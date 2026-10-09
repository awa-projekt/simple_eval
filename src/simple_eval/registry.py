from __future__ import annotations

import importlib

from pydantic_evals.evaluators import (
    Contains,
    Equals,
    EqualsExpected,
    Evaluator,
    IsInstance,
    LLMJudge,
)

from simple_eval.errors import SimpleEvalError
from simple_eval.graders import (
    ContainsExpectedGrader,
    ExactMatchGrader,
    RegexGrader,
    RubricJudgeGrader,
)
from simple_eval.spec import GraderSpec

BUILTIN: dict[str, type[Evaluator]] = {
    "exact_match": ExactMatchGrader,
    "contains_expected": ContainsExpectedGrader,
    "regex": RegexGrader,
    "rubric_judge": RubricJudgeGrader,
    "equals": Equals,
    "equals_expected": EqualsExpected,
    "contains": Contains,
    "is_instance": IsInstance,
    "llm_judge": LLMJudge,
}


def build(spec: GraderSpec) -> Evaluator:
    return _resolve(spec.uses)(**spec.args)


def _resolve(uses: str) -> type[Evaluator]:
    if uses in BUILTIN:
        return BUILTIN[uses]
    if ":" not in uses:
        raise SimpleEvalError(
            f"Unknown grader '{uses}'. Use one of {sorted(BUILTIN)} "
            "or a dotted path like 'my_pkg.graders:MyGrader'."
        )
    module_name, class_name = uses.split(":", maxsplit=1)
    grader = getattr(importlib.import_module(module_name), class_name, None)
    if not isinstance(grader, type) or not issubclass(grader, Evaluator):
        raise SimpleEvalError(f"'{uses}' is not an Evaluator subclass.")
    return grader
