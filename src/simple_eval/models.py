from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_evals.evaluators import EvaluatorContext

CaseMetadata = dict[str, Any]


@dataclass(frozen=True)
class CaseInput:
    """What the runner hands to a target for a single execution."""

    case_id: str
    trial: int
    prompt: str
    metadata: CaseMetadata
    run_id: str = ""

    def template_context(self) -> dict[str, Any]:
        return {
            "input": self.prompt,
            "case_id": self.case_id,
            "trial": self.trial,
            "run_id": self.run_id,
            "metadata": self.metadata,
        }


@dataclass
class TargetResult:
    """What a target returns: the graded `output`, and `attributes` recorded beside it.

    `output` is any JSON value and is what graders see. `attributes` (traces, token
    usage, intermediate state) are stored in outputs.jsonl for analysis and debugging
    but never graded. A target may also return a bare value, taken as the output.
    """

    output: Any
    attributes: dict[str, Any] = field(default_factory=dict)


class CaseRef(BaseModel):
    """Evaluation-phase inputs: identifies which recorded execution to grade."""

    target: str
    case_id: str
    trial: int
    prompt: str

    def __str__(self) -> str:
        return self.prompt

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.target, self.case_id, self.trial)


class CaseResult(BaseModel):
    """One recorded execution of one target on one case."""

    target: str
    case_id: str
    trial: int
    prompt: str
    expected_output: str | None
    metadata: CaseMetadata
    output: Any = None
    attributes: dict[str, Any] = {}
    error: str | None
    duration_s: float
    started_at: datetime

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.target, self.case_id, self.trial)

    def to_ref(self) -> CaseRef:
        return CaseRef(
            target=self.target, case_id=self.case_id, trial=self.trial, prompt=self.prompt
        )


ResultKind = Literal["assertion", "score", "label", "grader_error", "execution_error"]


class ResultRow(BaseModel):
    """One graded value in long format; every aggregate is a group-by of these.

    A grader that judges several items of one output names its results
    `<metric>.<item>`, e.g. `value.article_type`; `metric` and `item` are that name
    split at the first dot. A name without a dot is its own metric, with no item.
    """

    target: str
    case_id: str
    trial: int
    kind: ResultKind
    grader: str | None = None
    name: str | None = None
    metric: str | None = None
    item: str | None = None
    value: bool | int | float | str | None = None
    reason: str | None = None


EvalContext = EvaluatorContext[CaseRef, Any, CaseMetadata]
