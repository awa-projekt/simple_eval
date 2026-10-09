from __future__ import annotations

from pathlib import Path

from simple_eval.artifacts import RunArtifact
from simple_eval.spec import ComposeTarget, PythonTarget, Target
from simple_eval.targets.base import TargetAdapter
from simple_eval.targets.compose import ComposeAdapter
from simple_eval.targets.python import PythonAdapter

__all__ = ["TargetAdapter", "adapter_for"]


def adapter_for(
    target: Target, *, base_dir: Path, artifact: RunArtifact
) -> TargetAdapter:
    match target:
        case PythonTarget():
            return PythonAdapter(target)
        case ComposeTarget():
            return ComposeAdapter(
                target,
                base_dir=base_dir,
                project=f"simple-eval-{artifact.run_id}-{target.name}".lower(),
                logs_dir=artifact.logs_dir / target.name,
            )
