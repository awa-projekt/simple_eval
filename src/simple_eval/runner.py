from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

from pydantic_core import to_jsonable_python

from simple_eval import entrypoints, registry
from simple_eval.artifacts import RunArtifact, RunManifest
from simple_eval.datasets import LoadedCase, fingerprint, load_cases
from simple_eval.environment import Environments, workdir_name
from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseInput, CaseResult
from simple_eval.provenance import capture
from simple_eval.spec import EvalSpec
from simple_eval.targets import adapter_for
from simple_eval.targets.base import BoundTarget

# Called after every execution with (result, finished so far, total).
OnResult = Callable[[CaseResult, int, int], None]


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def resolve(base_dir: Path, path: Path) -> Path:
    return path if path.is_absolute() else (base_dir / path).resolve()


def select_cases(
    cases: list[LoadedCase], *, case_ids: list[str] | None, limit: int | None
) -> list[LoadedCase]:
    if case_ids:
        known = {case.case_id for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in known]
        if missing:
            raise SimpleEvalError(f"Dataset has no case(s) {', '.join(missing)}.")
        wanted = set(case_ids)
        cases = [case for case in cases if case.case_id in wanted]
    return cases[:limit] if limit else cases


async def execute(
    spec: EvalSpec,
    *,
    base_dir: Path,
    spec_bytes: bytes,
    run_id: str | None = None,
    case_ids: list[str] | None = None,
    limit: int | None = None,
    target_names: list[str] | None = None,
    on_result: OnResult | None = None,
) -> RunArtifact:
    """Phase 1: stand up the system under evaluation and record raw outputs."""
    pythonpath = [resolve(base_dir, path) for path in spec.pythonpath]
    entrypoints.extend_sys_path(pythonpath)

    for grader in spec.graders:
        registry.build(grader)
    targets = [spec.target(name) for name in target_names] if target_names else spec.targets
    environments = Environments(spec.environment)

    cases = select_cases(load_cases(spec.dataset, base_dir), case_ids=case_ids, limit=limit)
    if not cases:
        raise SimpleEvalError("Dataset contains no cases.")

    artifact = RunArtifact.create(resolve(base_dir, spec.artifacts_dir), run_id or new_run_id())
    try:
        return await _run(
            spec, artifact, cases, targets, environments,
            base_dir=base_dir, spec_bytes=spec_bytes, pythonpath=pythonpath,
            on_result=on_result,
        )
    except BaseException:
        # A run that never got to its first case leaves nothing behind.
        if not artifact.manifest_path.exists():
            shutil.rmtree(artifact.dir, ignore_errors=True)
        raise


async def _run(
    spec: EvalSpec,
    artifact: RunArtifact,
    cases: list[LoadedCase],
    targets: list,
    environments: Environments,
    *,
    base_dir: Path,
    spec_bytes: bytes,
    pythonpath: list[Path],
    on_result: OnResult | None,
) -> RunArtifact:
    provenance = await capture(
        repo_dir=base_dir,
        spec_bytes=spec_bytes,
        dataset_sha256=fingerprint(cases),
        dataset_path=resolve(base_dir, spec.dataset.path),
        env_passthrough=spec.env_passthrough,
        diff_paths=[resolve(base_dir, path) for path in spec.diff_paths],
    )
    provenance.environment = environments.provenance

    async with AsyncExitStack() as stack:
        stack.push_async_callback(environments.close)
        bound = []
        for target in targets:
            adapter = adapter_for(target, base_dir=base_dir, artifact=artifact)
            bound.append(await stack.enter_async_context(adapter.bind()))
        provenance.targets = {b.name: b.provenance for b in bound}

        manifest = RunManifest(
            run_id=artifact.run_id,
            spec=spec,
            base_dir=str(base_dir.resolve()),
            pythonpath=[str(path) for path in pythonpath],
            targets=[b.name for b in bound],
            provenance=provenance,
            started_at=datetime.now(UTC),
            case_count=len(cases),
            trials=spec.run.trials,
        )
        # Written before the first case, so an interrupted run is still readable.
        artifact.write_manifest(manifest)
        results = await _execute_all(
            bound,
            cases,
            environments,
            trials=spec.run.trials,
            max_concurrency=spec.run.max_concurrency,
            artifact=artifact,
            on_result=on_result,
        )

    manifest.finished_at = datetime.now(UTC)
    manifest.error_count = sum(1 for result in results if result.error)
    artifact.write_manifest(manifest)
    return artifact


async def _execute_all(
    targets: list[BoundTarget],
    cases: list[LoadedCase],
    environments: Environments,
    *,
    trials: int,
    max_concurrency: int,
    artifact: RunArtifact,
    on_result: OnResult | None,
) -> list[CaseResult]:
    semaphore = asyncio.Semaphore(max_concurrency)
    write_lock = asyncio.Lock()
    jobs = [
        (target, case, trial) for target in targets for case in cases for trial in range(trials)
    ]
    finished = 0

    async def run_one(target: BoundTarget, case: LoadedCase, trial: int) -> CaseResult:
        nonlocal finished
        async with semaphore:
            result = await _execute_case(
                target, case, trial, environments, artifact=artifact
            )
        async with write_lock:
            artifact.append_output(result)
            finished += 1
            if on_result is not None:
                on_result(result, finished, len(jobs))
        return result

    return list(await asyncio.gather(*(run_one(*job) for job in jobs)))


async def _execute_case(
    target: BoundTarget,
    case: LoadedCase,
    trial: int,
    environments: Environments,
    *,
    artifact: RunArtifact,
) -> CaseResult:
    started_at = datetime.now(UTC)
    clock = time.perf_counter()
    case_input = CaseInput(
        case_id=case.case_id,
        trial=trial,
        prompt=case.prompt,
        metadata=case.metadata,
        run_id=artifact.run_id,
    )
    workdir = artifact.envs_dir / target.name / workdir_name(case.case_id, trial)
    output = None
    attributes: dict = {}
    error: str | None = None
    try:
        async with environments.open(case_input, workdir) as env:
            result = await target.call(case_input, env)
        output = _jsonable(result.output, "output")
        attributes = _jsonable(result.attributes, "attributes")
    except Exception as exception:  # noqa: BLE001 - recorded as a case failure
        error = f"{type(exception).__name__}: {exception}"
    finally:
        environments.finish(workdir, failed=error is not None)

    return CaseResult(
        target=target.name,
        case_id=case.case_id,
        trial=trial,
        prompt=case.prompt,
        expected_output=case.expected_output,
        metadata=case.metadata,
        output=output,
        attributes=attributes,
        error=error,
        duration_s=time.perf_counter() - clock,
        started_at=started_at,
    )


def _jsonable(value, what: str):
    try:
        return to_jsonable_python(value)
    except Exception as error:
        raise TypeError(f"the target's {what} is not JSON-serialisable: {error}") from error
