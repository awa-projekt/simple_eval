from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic_evals.reporting import EvaluationReport

from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseMetadata, CaseRef, CaseResult, ResultRow
from simple_eval.provenance import Provenance
from simple_eval.spec import EvalSpec

TypedReport = EvaluationReport[CaseRef, Any, CaseMetadata]
_REPORT_ADAPTER: TypeAdapter[TypedReport] = TypeAdapter(TypedReport)

MANIFEST = "manifest.json"
OUTPUTS = "outputs.jsonl"
EVALUATION = "evaluation.json"
RESULTS = "results.jsonl"
LOGS = "logs"
ENVS = "envs"


class RunManifest(BaseModel):
    run_id: str
    spec: EvalSpec
    # Where the spec's relative paths resolved, so `evaluate` imports the same graders.
    base_dir: str
    pythonpath: list[str] = []
    targets: list[str]
    provenance: Provenance
    started_at: datetime
    finished_at: datetime | None = None
    case_count: int
    trials: int
    error_count: int = 0


@dataclass(frozen=True)
class RunArtifact:
    """The on-disk handoff between the runner, evaluation, and analysis phases."""

    dir: Path

    @classmethod
    def create(cls, artifacts_dir: Path, run_id: str) -> RunArtifact:
        artifact = cls(artifacts_dir / run_id)
        (artifact.dir / LOGS).mkdir(parents=True, exist_ok=True)
        return artifact

    @classmethod
    def open(cls, path: str | Path) -> RunArtifact:
        artifact = cls(Path(path))
        if not artifact.manifest_path.is_file():
            raise SimpleEvalError(f"{artifact.dir} is not a run artifact.")
        return artifact

    @property
    def run_id(self) -> str:
        return self.dir.name

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST

    @property
    def outputs_path(self) -> Path:
        return self.dir / OUTPUTS

    @property
    def evaluation_path(self) -> Path:
        return self.dir / EVALUATION

    @property
    def results_path(self) -> Path:
        return self.dir / RESULTS

    @property
    def logs_dir(self) -> Path:
        return self.dir / LOGS

    @property
    def envs_dir(self) -> Path:
        """Scratch directories of the environments, one per execution."""
        return self.dir / ENVS

    def write_manifest(self, manifest: RunManifest) -> None:
        self.manifest_path.write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json(self.manifest_path.read_text())

    def append_output(self, result: CaseResult) -> None:
        with self.outputs_path.open("a", encoding="utf-8") as file:
            file.write(result.model_dump_json() + "\n")

    def read_outputs(self) -> list[CaseResult]:
        if not self.outputs_path.is_file():
            return []
        return [
            CaseResult.model_validate_json(line)
            for line in self.outputs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_evaluation(self, report: TypedReport) -> None:
        self.evaluation_path.write_bytes(_REPORT_ADAPTER.dump_json(report, indent=2))

    def read_evaluation(self) -> TypedReport:
        if not self.evaluation_path.is_file():
            raise SimpleEvalError(
                f"{self.dir} has no evaluation; run 'simple-eval evaluate {self.dir}' first."
            )
        return _REPORT_ADAPTER.validate_json(self.evaluation_path.read_bytes())

    def write_results(self, rows: list[ResultRow]) -> None:
        self.results_path.write_text(
            "".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8"
        )

    def read_results(self) -> list[ResultRow]:
        if not self.results_path.is_file():
            raise SimpleEvalError(
                f"{self.dir} has no results; run 'simple-eval evaluate {self.dir}' first."
            )
        return [
            ResultRow.model_validate_json(line)
            for line in self.results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
