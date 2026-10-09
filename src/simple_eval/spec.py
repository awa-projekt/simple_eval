from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseMetadata


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CsvSource(_Strict):
    kind: Literal["csv"] = "csv"
    path: Path
    task_id_col: str = "task_id"
    input_col: str = "input"
    expected_col: str = "expected_output"


class JsonlSource(_Strict):
    kind: Literal["jsonl"] = "jsonl"
    path: Path
    task_id_key: str = "task_id"
    input_key: str = "input"
    expected_key: str = "expected_output"


class NativeSource(_Strict):
    kind: Literal["pydantic_evals"] = "pydantic_evals"
    path: Path


DataSource = Annotated[
    CsvSource | JsonlSource | NativeSource, Field(discriminator="kind")
]


class Endpoint(_Strict):
    """The single entrypoint of the system under evaluation.

    The host port is discovered at runtime so runs never collide on fixed ports.
    """

    service: str
    port: int
    path: str = "/"
    method: Literal["GET", "POST"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    output_pointer: str = ""
    expect_status: int = 200


class HealthCheck(_Strict):
    path: str = "/health"
    expect_status: int = 200
    timeout_s: float = 60.0
    interval_s: float = 0.5


class ComposeTarget(_Strict):
    kind: Literal["compose"] = "compose"
    name: str
    file: Path = Path("docker-compose.eval.yaml")
    profiles: list[str] = Field(default_factory=list)
    endpoint: Endpoint
    health: HealthCheck | None = None
    up_timeout_s: float = 180.0
    request_timeout_s: float = 60.0


class PythonTarget(_Strict):
    """A Python callable, or a class built once from `args` and then called per case.

    A function is called as `fn(case, env, **args)`. A class is instantiated as
    `cls(**args)` before the first case, and the instance is called as
    `instance(case, env)`; its optional `provenance()` lands in the manifest and its
    optional `close()` runs once after the last case.
    """

    kind: Literal["python"] = "python"
    name: str
    entrypoint: str
    args: dict[str, Any] = Field(default_factory=dict)
    request_timeout_s: float = 60.0


Target = Annotated[ComposeTarget | PythonTarget, Field(discriminator="kind")]


class EnvironmentSpec(_Strict):
    """What every execution gets a fresh copy of: an async context manager.

    A function is called as `fn(case, workdir, **args)`; a class is instantiated as
    `cls(**args)` once and the instance called as `instance(case, workdir)`. Either
    returns an async context manager whose value is handed to the target as `env`.
    Its optional `provenance()` lands in the manifest, its optional `close()` runs once
    after the last case.
    """

    entrypoint: str
    args: dict[str, Any] = Field(default_factory=dict)
    setup_timeout_s: float = 120.0
    keep_workdir: Literal["failed", "always", "never"] = "failed"


class GraderSpec(_Strict):
    uses: str
    args: dict[str, Any] = Field(default_factory=dict)
    when: CaseMetadata = Field(default_factory=dict)

    def applies_to(self, metadata: CaseMetadata) -> bool:
        return all(str(metadata.get(k)) == str(v) for k, v in self.when.items())


class RunConfig(_Strict):
    trials: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=4, ge=1)


class EvalSpec(_Strict):
    version: Literal[1] = 1
    name: str
    targets: list[Target] = Field(min_length=1)
    environment: EnvironmentSpec | None = None
    dataset: DataSource
    graders: list[GraderSpec] = Field(
        default_factory=lambda: [GraderSpec(uses="exact_match")]
    )
    run: RunConfig = Field(default_factory=RunConfig)
    artifacts_dir: Path = Path("runs")
    env_passthrough: list[str] = Field(default_factory=list)
    # Prepended to sys.path, relative to the spec, so entrypoints in the project import.
    pythonpath: list[Path] = Field(default_factory=list)
    # The code whose uncommitted changes the manifest records, relative to the spec.
    diff_paths: list[Path] = Field(default_factory=lambda: [Path(".")])

    @model_validator(mode="after")
    def _unique_target_names(self) -> EvalSpec:
        names = [target.name for target in self.targets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"target names must be unique: {', '.join(duplicates)}")
        return self

    def target(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        raise SimpleEvalError(
            f"No target '{name}'. The spec has: {', '.join(t.name for t in self.targets)}."
        )


def load_spec(path: str | Path) -> tuple[EvalSpec, Path, bytes]:
    """Return the spec, the directory its relative paths resolve against, and its bytes."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise SimpleEvalError(f"Spec file not found: {resolved}")
    raw = resolved.read_bytes()
    try:
        spec = EvalSpec.model_validate(yaml.safe_load(raw))
    except ValidationError as error:
        raise SimpleEvalError(
            f"{resolved} is not a valid eval spec:\n{error}"
        ) from error
    return spec, resolved.parent, raw


def spec_json_schema() -> dict[str, Any]:
    return EvalSpec.model_json_schema()
