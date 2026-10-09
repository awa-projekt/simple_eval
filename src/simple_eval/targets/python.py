from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from simple_eval import entrypoints
from simple_eval.models import CaseInput, TargetResult
from simple_eval.provenance import TargetProvenance
from simple_eval.spec import PythonTarget
from simple_eval.targets.base import BoundTarget, as_result


class PythonAdapter:
    def __init__(self, target: PythonTarget) -> None:
        self._target = target

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[BoundTarget]:
        target = self._target
        bound = entrypoints.bind(target.entrypoint, target.args)
        timeout = target.request_timeout_s

        async def call(case: CaseInput, env: Any) -> TargetResult:
            pending = (
                bound.call(case, env)
                if bound.is_async
                else asyncio.to_thread(bound.call, case, env)
            )
            try:
                return as_result(await asyncio.wait_for(pending, timeout))
            except TimeoutError:
                raise TimeoutError(f"{target.name} took longer than {timeout}s") from None

        try:
            yield BoundTarget(
                name=target.name,
                call=call,
                provenance=TargetProvenance(kind="python", details=bound.provenance),
            )
        finally:
            await bound.aclose()
