from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_evals import Case, Dataset, set_eval_attribute

from simple_eval import entrypoints, registry
from simple_eval.artifacts import RunArtifact, TypedReport
from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseMetadata, CaseRef, CaseResult, ResultKind, ResultRow
from simple_eval.spec import GraderSpec


class ReplayError(RuntimeError):
    """Raised in place of a failure the runner already recorded."""


def evaluate(
    artifact: RunArtifact, *, graders: list[GraderSpec] | None = None
) -> TypedReport:
    """Phase 2: grade recorded outputs. Never touches the system under evaluation.

    Writes the native report to evaluation.json and the same results, one row per
    graded value, to results.jsonl.
    """
    manifest = artifact.read_manifest()
    entrypoints.extend_sys_path([Path(path) for path in manifest.pythonpath])
    results = artifact.read_outputs()
    if not results:
        raise SimpleEvalError(f"{artifact.dir} has no recorded outputs.")

    grader_specs = graders if graders is not None else manifest.spec.graders
    dataset = _build_dataset(
        results, grader_specs, trials=manifest.trials, name=manifest.spec.name
    )
    by_key = {result.key: result for result in results}

    def replay(ref: CaseRef) -> Any:
        result = by_key[ref.key]
        set_eval_attribute("target", result.target)
        set_eval_attribute("trial", result.trial)
        set_eval_attribute("runner_duration_s", result.duration_s)
        if result.error is not None:
            raise ReplayError(result.error)
        return result.output

    report = dataset.evaluate_sync(
        replay,
        name=manifest.run_id,
        max_concurrency=manifest.spec.run.max_concurrency,
        metadata={
            "run_id": manifest.run_id,
            "spec_name": manifest.spec.name,
            "graders": [spec.model_dump(mode="json") for spec in grader_specs],
        },
    )
    artifact.write_evaluation(report)
    artifact.write_results(flatten(report))
    return report


def case_name(result: CaseResult, trials: int) -> str:
    name = f"{result.target}/{result.case_id}"
    return name if trials == 1 else f"{name}#{result.trial}"


def _build_dataset(
    results: list[CaseResult], grader_specs: list[GraderSpec], *, trials: int, name: str
) -> Dataset[CaseRef, Any, CaseMetadata]:
    graders = [(spec, registry.build(spec)) for spec in grader_specs]
    cases = [
        Case(
            name=case_name(result, trials),
            inputs=result.to_ref(),
            expected_output=result.expected_output,
            metadata=result.metadata,
            evaluators=[
                grader for spec, grader in graders if spec.applies_to(result.metadata)
            ],
        )
        for result in results
    ]
    return Dataset[CaseRef, Any, CaseMetadata](name=name, cases=cases)


def flatten(report: TypedReport) -> list[ResultRow]:
    """Every assertion, score and label of the report, one row each, plus the failures."""
    rows: list[ResultRow] = []
    for case in report.cases:
        ref = case.inputs
        graded: tuple[tuple[ResultKind, dict], ...] = (
            ("assertion", case.assertions),
            ("score", case.scores),
            ("label", case.labels),
        )
        for kind, results in graded:
            for name, result in results.items():
                rows.append(_row(ref, kind, name, result.value, result.reason, result.source.name))
        for failure in case.evaluator_failures:
            rows.append(
                _row(ref, "grader_error", failure.name, failure.error_message, None,
                     failure.source.name)
            )
    for failure in report.failures:
        rows.append(_row(failure.inputs, "execution_error", None, failure.error_message))
    return rows


def _row(
    ref: CaseRef,
    kind: ResultKind,
    name: str | None,
    value: Any,
    reason: str | None = None,
    grader: str | None = None,
) -> ResultRow:
    metric, _, item = (name or "").partition(".")
    return ResultRow(
        target=ref.target,
        case_id=ref.case_id,
        trial=ref.trial,
        kind=kind,
        grader=grader,
        name=name,
        metric=metric or None,
        item=item or None,
        value=value,
        reason=reason,
    )
