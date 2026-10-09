from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic_evals.evaluators import EvaluationReason, Evaluator

from simple_eval import analysis, evaluation
from simple_eval.artifacts import RunArtifact
from simple_eval.errors import SimpleEvalError
from simple_eval.models import EvalContext, TargetResult
from simple_eval.registry import build
from simple_eval.runner import execute
from simple_eval.spec import (
    CsvSource,
    EnvironmentSpec,
    EvalSpec,
    GraderSpec,
    PythonTarget,
    RunConfig,
)

CSV = (
    "id,category,question,expected_answer\n"
    "c1,math,What is 2 + 2?,4\n"
    "c2,math,What is 3 + 3?,6\n"
    "c3,prose,Name a colour.,blue\n"
)

CALLS: list[str] = []
EVENTS: list[tuple[str, str]] = []
LIVE: set[str] = set()
PEAK: list[int] = [0]


def answers(case, env) -> str:
    CALLS.append(case.prompt)
    if case.prompt == "What is 2 + 2?":
        return "4"
    if case.prompt == "Name a colour.":
        return "The colour is blue."
    return "seven"


def explodes(case, env) -> str:
    raise RuntimeError(f"target down for {case.prompt}")


async def echo_env(case, env) -> dict:
    await asyncio.sleep(0.01)
    return {"case": case.case_id, "env": env}


def structured(case, env) -> TargetResult:
    return TargetResult(
        output={"answer": answers(case, env), "words": len(case.prompt.split())},
        attributes={"trace": ["thought", "answer"]},
    )


def unserialisable(case, env) -> object:
    return object()


class Counter:
    """A class target: built once from its args, called per case."""

    built = 0
    closed = 0

    def __init__(self, offset: int) -> None:
        Counter.built += 1
        self.offset = offset

    async def __call__(self, case, env) -> int:
        return len(case.case_id) + self.offset

    def provenance(self) -> dict:
        return {"offset": self.offset}

    async def close(self) -> None:
        Counter.closed += 1


@asynccontextmanager
async def scratch(case, workdir: Path, fail_for: str | None = None):
    """An environment: a scratch file per execution, torn down afterwards."""
    if case.case_id == fail_for:
        raise RuntimeError(f"no environment for {case.case_id}")
    key = f"{case.case_id}#{case.trial}@{workdir.parent.name}"
    (workdir / "state.txt").write_text(case.case_id, encoding="utf-8")
    EVENTS.append(("enter", key))
    LIVE.add(key)
    PEAK[0] = max(PEAK[0], len(LIVE))
    try:
        yield {"workdir": str(workdir), "state": (workdir / "state.txt").read_text()}
    finally:
        LIVE.discard(key)
        EVENTS.append(("exit", key))


@dataclass(repr=False)
class PerKey(Evaluator):
    """Judges each key of a structured output on its own: `present.<key>`."""

    def evaluate(self, ctx: EvalContext) -> dict[str, EvaluationReason | str]:
        out: dict[str, EvaluationReason | str] = {}
        for key, value in ctx.output.items():
            out[f"present.{key}"] = EvaluationReason(value=value is not None, reason=key)
            out[f"kind.{key}"] = type(value).__name__
        return out


def _spec(
    tmp: Path,
    targets: list[PythonTarget],
    graders: list[GraderSpec],
    trials: int = 1,
    environment: EnvironmentSpec | None = None,
    **extra,
) -> EvalSpec:
    (tmp / "data.csv").write_text(CSV, encoding="utf-8")
    return EvalSpec(
        name="test",
        targets=targets,
        environment=environment,
        dataset=CsvSource(
            path=Path("data.csv"),
            task_id_col="id",
            input_col="question",
            expected_col="expected_answer",
        ),
        graders=graders,
        run=RunConfig(trials=trials, max_concurrency=2),
        **extra,
    )


def _target(entrypoint: str, name: str = "t", **args) -> PythonTarget:
    return PythonTarget(name=name, entrypoint=f"{__name__}:{entrypoint}", args=args)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        CALLS.clear()
        EVENTS.clear()
        LIVE.clear()
        PEAK[0] = 0
        Counter.built = 0
        Counter.closed = 0
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, spec: EvalSpec, **options) -> RunArtifact:
        return asyncio.run(
            execute(spec, base_dir=self.tmp, spec_bytes=b"spec", run_id="testrun", **options)
        )

    def test_grading_is_decoupled_from_execution(self) -> None:
        artifact = self._run(self._spec_exact())
        self.assertEqual(len(CALLS), 3)

        strict = evaluation.evaluate(artifact)
        lenient = evaluation.evaluate(
            artifact, graders=[GraderSpec(uses="contains_expected")]
        )

        self.assertEqual(len(CALLS), 3, "re-grading must not re-run the target")
        self.assertEqual(_passes(strict), {"t/c1"})
        self.assertEqual(_passes(lenient), {"t/c1", "t/c3"})

    def test_grader_when_scopes_to_matching_metadata(self) -> None:
        artifact = self._run(
            self._spec_exact(
                graders=[
                    GraderSpec(uses="exact_match"),
                    GraderSpec(
                        uses="regex",
                        args={"pattern": r"^The colour"},
                        when={"category": "prose"},
                    ),
                ]
            )
        )
        report = evaluation.evaluate(artifact)
        by_name = {case.name: set(case.assertions) for case in report.cases}

        self.assertEqual(by_name["t/c1"], {"exact_match"})
        self.assertEqual(by_name["t/c3"], {"exact_match", "regex"})

    def test_target_errors_are_recorded_not_raised(self) -> None:
        artifact = self._run(self._spec_exact(entrypoint="explodes"))
        manifest = artifact.read_manifest()

        self.assertEqual(manifest.error_count, 3)
        report = evaluation.evaluate(artifact)
        self.assertEqual(len(report.cases), 0)
        self.assertEqual(len(report.failures), 3)
        self.assertIn("target down", report.failures[0].error_message)
        rows = artifact.read_results()
        self.assertEqual({row.kind for row in rows}, {"execution_error"})
        self.assertEqual(sorted(row.case_id for row in rows), ["c1", "c2", "c3"])

    def test_analysis_groups_and_flags_flaky_cases(self) -> None:
        artifact = self._run(self._spec_exact(trials=2))
        evaluation.evaluate(artifact)
        summary = analysis.summarize(artifact, group_by="category")

        self.assertEqual(summary.targets["t"].executions, 6)
        self.assertAlmostEqual(
            summary.groups["t"]["math"].assertion_rates["exact_match"], 0.5
        )
        self.assertEqual(summary.groups["t"]["prose"].assertion_rates["exact_match"], 0.0)
        self.assertEqual(summary.flaky_cases, [])

    def test_provenance_flags_unpinned_runs(self) -> None:
        artifact = self._run(self._spec_exact())
        provenance = artifact.read_manifest().provenance

        self.assertFalse(provenance.portable)
        self.assertIn("not a git repository", provenance.unportable_reasons())

    def test_every_execution_gets_its_own_environment(self) -> None:
        spec = _spec(
            self.tmp,
            [_target("echo_env", "a"), _target("echo_env", "b")],
            [GraderSpec(uses="exact_match")],
            environment=EnvironmentSpec(entrypoint=f"{__name__}:scratch"),
        )
        artifact = self._run(spec)

        outputs = artifact.read_outputs()
        self.assertEqual(len(outputs), 6)
        self.assertEqual(len([e for e in EVENTS if e[0] == "enter"]), 6)
        self.assertEqual(len({key for _, key in EVENTS}), 6, "one environment each")
        self.assertEqual(LIVE, set(), "every environment was torn down")
        self.assertLessEqual(PEAK[0], 2, "no more environments than max_concurrency")
        for result in outputs:
            self.assertIsNone(result.error)
            self.assertEqual(result.output["env"]["state"], result.case_id)
            self.assertFalse(Path(result.output["env"]["workdir"]).exists(),
                             "a successful case's workdir is removed")

    def test_a_failed_setup_is_recorded_and_its_workdir_kept(self) -> None:
        spec = _spec(
            self.tmp,
            [_target("answers")],
            [GraderSpec(uses="exact_match")],
            environment=EnvironmentSpec(
                entrypoint=f"{__name__}:scratch", args={"fail_for": "c2"}
            ),
        )
        artifact = self._run(spec)

        errors = {r.case_id: r.error for r in artifact.read_outputs() if r.error}
        self.assertEqual(list(errors), ["c2"])
        self.assertIn("environment setup failed", errors["c2"])
        self.assertEqual(len(CALLS), 2, "the target never ran without its environment")
        self.assertTrue((artifact.envs_dir / "t" / "c2-0").is_dir())
        self.assertFalse((artifact.envs_dir / "t" / "c1-0").exists())

    def test_the_environment_is_torn_down_when_the_target_fails(self) -> None:
        spec = _spec(
            self.tmp,
            [_target("explodes")],
            [GraderSpec(uses="exact_match")],
            environment=EnvironmentSpec(entrypoint=f"{__name__}:scratch"),
        )
        artifact = self._run(spec)

        self.assertEqual(artifact.read_manifest().error_count, 3)
        self.assertEqual(LIVE, set())
        self.assertEqual(len([e for e in EVENTS if e[0] == "exit"]), 3)
        self.assertTrue((artifact.envs_dir / "t" / "c1-0" / "state.txt").is_file(),
                        "a failed case keeps its workdir for debugging")

    def test_structured_outputs_are_graded_per_item_and_attributes_recorded(self) -> None:
        spec = _spec(self.tmp, [_target("structured")],
                     [GraderSpec(uses=f"{__name__}:PerKey")])
        artifact = self._run(spec)
        evaluation.evaluate(artifact)

        recorded = {r.case_id: r for r in artifact.read_outputs()}
        self.assertEqual(recorded["c1"].output, {"answer": "4", "words": 5})
        self.assertEqual(recorded["c1"].attributes, {"trace": ["thought", "answer"]})

        rows = [r for r in artifact.read_results() if r.case_id == "c1"]
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["present.answer"].kind, "assertion")
        self.assertEqual(by_name["present.answer"].metric, "present")
        self.assertEqual(by_name["present.answer"].item, "answer")
        self.assertEqual(by_name["kind.words"].kind, "label")
        self.assertEqual(by_name["kind.words"].value, "int")
        self.assertEqual(by_name["kind.words"].grader, "PerKey")

        summary = analysis.summarize(artifact)
        self.assertEqual(summary.targets["t"].metric_rates, {"present": 1.0})

    def test_a_class_target_is_built_once_from_its_args(self) -> None:
        spec = _spec(self.tmp, [_target("Counter", offset=10)],
                     [GraderSpec(uses="exact_match")])
        artifact = self._run(spec)

        self.assertEqual(Counter.built, 1)
        self.assertEqual(Counter.closed, 1, "close() runs once, after the last case")
        self.assertEqual({r.output for r in artifact.read_outputs()}, {12})
        details = artifact.read_manifest().provenance.targets["t"].details
        self.assertEqual(details, {"offset": 10})

    def test_several_targets_run_the_same_cases(self) -> None:
        spec = _spec(
            self.tmp,
            [_target("answers", "good"), _target("explodes", "broken")],
            [GraderSpec(uses="exact_match")],
        )
        artifact = self._run(spec)
        evaluation.evaluate(artifact)
        summary = analysis.summarize(artifact)

        self.assertEqual(artifact.read_manifest().targets, ["good", "broken"])
        self.assertEqual(summary.targets["good"].executions, 3)
        self.assertEqual(summary.targets["broken"].failures, 3)
        deltas = analysis.compare(summary, summary)
        self.assertEqual({d.target for d in deltas}, {"good", "broken"})

    def test_a_target_that_cannot_be_built_stops_the_run_before_any_case(self) -> None:
        spec = _spec(self.tmp, [_target("Counter", offset="x", unknown=1)],
                     [GraderSpec(uses="exact_match")])
        with self.assertRaises(SimpleEvalError) as caught:
            self._run(spec)

        self.assertIn("Cannot build", str(caught.exception))
        self.assertFalse((self.tmp / "runs" / "testrun").exists(), "no half-made run is left")

    def test_duplicate_target_names_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _spec(self.tmp, [_target("answers"), _target("explodes")],
                  [GraderSpec(uses="exact_match")])

    def test_pythonpath_makes_project_entrypoints_importable(self) -> None:
        project = self.tmp / "project"
        project.mkdir()
        (project / "local_agent.py").write_text(
            "def run(case, env):\n    return case.case_id.upper()\n", encoding="utf-8"
        )
        spec = _spec(
            self.tmp,
            [PythonTarget(name="local", entrypoint="local_agent:run")],
            [GraderSpec(uses="exact_match")],
            pythonpath=[Path("project")],
        )
        artifact = self._run(spec)

        self.assertEqual({r.output for r in artifact.read_outputs()}, {"C1", "C2", "C3"})
        self.assertEqual(artifact.read_manifest().pythonpath, [str(project.resolve())])

    def test_cases_can_be_picked_by_id_or_capped(self) -> None:
        by_id = self._run(self._spec_exact(), case_ids=["c3", "c1"])
        self.assertEqual(sorted(r.case_id for r in by_id.read_outputs()), ["c1", "c3"])

        capped = asyncio.run(execute(self._spec_exact(), base_dir=self.tmp,
                                     spec_bytes=b"spec", run_id="capped", limit=2))
        self.assertEqual(capped.read_manifest().case_count, 2)

        with self.assertRaises(SimpleEvalError):
            self._run(self._spec_exact(), case_ids=["nope"])

    def test_an_unserialisable_output_is_a_case_error(self) -> None:
        artifact = self._run(self._spec_exact(entrypoint="unserialisable"))

        errors = [r.error for r in artifact.read_outputs()]
        self.assertTrue(all("not JSON-serialisable" in (e or "") for e in errors))

    def test_the_manifest_is_readable_while_the_run_is_in_flight(self) -> None:
        artifact = self._run(self._spec_exact())
        manifest = json.loads(artifact.manifest_path.read_text())

        self.assertEqual(manifest["targets"], ["t"])
        self.assertIsNotNone(manifest["finished_at"])
        self.assertEqual(manifest["base_dir"], str(self.tmp.resolve()))

    def _spec_exact(
        self,
        entrypoint: str = "answers",
        graders: list[GraderSpec] | None = None,
        trials: int = 1,
    ) -> EvalSpec:
        return _spec(
            self.tmp,
            [_target(entrypoint)],
            graders or [GraderSpec(uses="exact_match")],
            trials=trials,
        )


class RegistryTests(unittest.TestCase):
    def test_unknown_grader_lists_available_names(self) -> None:
        with self.assertRaises(SimpleEvalError) as caught:
            build(GraderSpec(uses="nope"))
        self.assertIn("exact_match", str(caught.exception))

    def test_dotted_path_must_be_an_evaluator(self) -> None:
        with self.assertRaises(SimpleEvalError):
            build(GraderSpec(uses="pathlib:Path"))


def _passes(report) -> set[str]:
    return {
        case.name
        for case in report.cases
        if case.assertions and all(a.value for a in case.assertions.values())
    }


if __name__ == "__main__":
    unittest.main()
