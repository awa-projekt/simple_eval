from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from simple_eval.models import CaseInput, TargetResult
from simple_eval.provenance import TargetProvenance

TaskFn = Callable[[CaseInput, Any], Awaitable[TargetResult]]


@dataclass
class BoundTarget:
    """A live system under evaluation, plus what it took to stand it up."""

    name: str
    call: TaskFn
    provenance: TargetProvenance = field(default_factory=TargetProvenance)


class TargetAdapter(Protocol):
    def bind(self) -> AbstractAsyncContextManager[BoundTarget]: ...


def as_result(value: Any) -> TargetResult:
    return value if isinstance(value, TargetResult) else TargetResult(output=value)
