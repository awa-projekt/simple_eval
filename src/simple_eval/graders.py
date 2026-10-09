from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorOutput

from simple_eval.models import EvalContext


class Named:
    """Reports under its `evaluation_name`, which pydantic-evals 2 no longer reads itself."""

    def get_default_evaluation_name(self) -> str:
        return self.evaluation_name


def as_text(output) -> str:
    """String graders compare text; a structured output is compared as its JSON."""
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)


@dataclass(repr=False)
class ExactMatchGrader(Named, Evaluator):
    case_sensitive: bool = False
    strip_whitespace: bool = True
    evaluation_name: str = "exact_match"

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        if ctx.expected_output is None:
            return EvaluationReason(value=False, reason="Case has no expected_output.")

        expected, actual = ctx.expected_output, as_text(ctx.output)
        if self.strip_whitespace:
            expected, actual = expected.strip(), actual.strip()
        if not self.case_sensitive:
            expected, actual = expected.lower(), actual.lower()

        passed = expected == actual
        return EvaluationReason(
            value=passed,
            reason="Exact match."
            if passed
            else f"Expected {ctx.expected_output!r}, got {ctx.output!r}.",
        )


@dataclass(repr=False)
class ContainsExpectedGrader(Named, Evaluator):
    case_sensitive: bool = False
    evaluation_name: str = "contains_expected"

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        if ctx.expected_output is None:
            return EvaluationReason(value=False, reason="Case has no expected_output.")

        expected, actual = ctx.expected_output, as_text(ctx.output)
        if not self.case_sensitive:
            expected, actual = expected.lower(), actual.lower()

        passed = expected in actual
        return EvaluationReason(
            value=passed,
            reason=f"{'Found' if passed else 'Missing'} {ctx.expected_output!r} in output.",
        )


@dataclass(repr=False)
class RegexGrader(Named, Evaluator):
    pattern: str
    flags: int = 0
    evaluation_name: str = "regex"

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        found = re.search(self.pattern, as_text(ctx.output), flags=self.flags) is not None
        return EvaluationReason(
            value=found,
            reason=f"Pattern {self.pattern!r} {'matched' if found else 'did not match'}.",
        )


@dataclass(repr=False)
class PythonGrader(Named, Evaluator):
    """Custom grader over the full evaluator context. Not expressible in YAML."""

    fn: Callable[[EvalContext], EvaluatorOutput]
    evaluation_name: str = "python"

    def evaluate(self, ctx: EvalContext) -> EvaluatorOutput:
        return self.fn(ctx)


class RubricJudgeOutput(BaseModel):
    passed: bool = Field(description="Whether the output passes the rubric.")
    score: float = Field(ge=0.0, le=1.0, description="A normalized rubric score.")
    rationale: str = Field(description="Short explanation of the decision.")


@dataclass(repr=False)
class RubricJudgeGrader(Evaluator):
    model: str
    rubric: str
    evaluation_name: str = "rubric_judge"
    system_prompt: str = (
        "You are a strict evaluation judge. Use the rubric exactly. "
        "If evidence is insufficient, fail the case and explain why."
    )

    async def evaluate(self, ctx: EvalContext) -> dict[str, EvaluationReason]:
        from pydantic_ai import Agent

        case_data = {
            "case_id": ctx.inputs.case_id,
            "prompt": ctx.inputs.prompt,
            "metadata": ctx.metadata,
            "expected_output": ctx.expected_output,
            "output": ctx.output,
            "attributes": ctx.attributes,
            "metrics": ctx.metrics,
        }
        judge = Agent(
            model=self.model,
            system_prompt=self.system_prompt,
            output_type=RubricJudgeOutput,
        )
        response = await judge.run(
            "Evaluate the agent output using the rubric.\n\n"
            f"Rubric:\n{self.rubric}\n\n"
            "Case and execution data:\n"
            f"{json.dumps(case_data, indent=2, default=str)}"
        )
        data = response.output
        return {
            f"{self.evaluation_name}_score": EvaluationReason(
                value=data.score, reason=data.rationale
            ),
            f"{self.evaluation_name}_pass": EvaluationReason(
                value=data.passed, reason=data.rationale
            ),
        }
