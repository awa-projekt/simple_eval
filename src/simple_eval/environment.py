"""A fresh environment per execution, so stateful systems can run cases concurrently.

The spec's `environment` names an async context manager. Every execution enters its
own copy with its own scratch directory, hands the yielded value to the target as
`env`, and leaves it once the target returns. Concurrency is `run.max_concurrency`
environments alive at once.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from simple_eval import entrypoints
from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseInput
from simple_eval.spec import EnvironmentSpec


class EnvironmentSetupError(RuntimeError):
    """The environment could not be entered, so the target never ran."""


class Environments:
    """Opens one environment per execution; a no-op when the spec declares none."""

    def __init__(self, spec: EnvironmentSpec | None) -> None:
        self.spec = spec
        self._entrypoint = entrypoints.bind(spec.entrypoint, spec.args) if spec else None

    @property
    def provenance(self) -> dict[str, Any] | None:
        if self.spec is None or self._entrypoint is None:
            return None
        return {
            "entrypoint": self.spec.entrypoint,
            "args": self.spec.args,
            **self._entrypoint.provenance,
        }

    @asynccontextmanager
    async def open(self, case: CaseInput, workdir: Path) -> AsyncIterator[Any]:
        if self.spec is None or self._entrypoint is None:
            yield None
            return

        workdir.mkdir(parents=True, exist_ok=True)
        manager = self._entrypoint.call(case, workdir)
        if not isinstance(manager, AbstractAsyncContextManager):
            raise SimpleEvalError(
                f"Environment '{self.spec.entrypoint}' must return an async context "
                f"manager, got {type(manager).__name__}."
            )
        try:
            env = await asyncio.wait_for(manager.__aenter__(), self.spec.setup_timeout_s)
        except TimeoutError:
            raise EnvironmentSetupError(
                f"environment setup took longer than {self.spec.setup_timeout_s}s"
            ) from None
        except Exception as error:
            raise EnvironmentSetupError(
                f"environment setup failed: {type(error).__name__}: {error}"
            ) from error

        try:
            yield env
        except BaseException as error:
            if not await manager.__aexit__(type(error), error, error.__traceback__):
                raise
        else:
            await manager.__aexit__(None, None, None)

    async def close(self) -> None:
        """Once after the last execution: the class's optional `close()`."""
        if self._entrypoint is not None:
            await self._entrypoint.aclose()

    def finish(self, workdir: Path, *, failed: bool) -> None:
        """Remove the scratch directory unless the spec keeps it."""
        if self.spec is None or not workdir.exists():
            return
        keep = self.spec.keep_workdir
        if keep == "always" or (keep == "failed" and failed):
            return
        shutil.rmtree(workdir, ignore_errors=True)


def workdir_name(case_id: str, trial: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", case_id)
    return f"{safe}-{trial}"
