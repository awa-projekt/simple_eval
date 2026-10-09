"""Resolving `module:attr` entrypoints for targets and environments."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from simple_eval.errors import SimpleEvalError


@dataclass
class Entrypoint:
    """A callable ready to be called per case, and what it reported about itself."""

    call: Callable[..., Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    # A class's optional `close()`, sync or async, called once after the last case.
    close: Callable[[], Any] | None = None

    async def aclose(self) -> None:
        if self.close is None:
            return
        result = self.close()
        if inspect.isawaitable(result):
            await result

    @property
    def is_async(self) -> bool:
        # An instance of a class target is async when its class defines `async __call__`.
        return inspect.iscoroutinefunction(self.call) or inspect.iscoroutinefunction(
            type(self.call).__call__
        )


def bind(entrypoint: str, args: dict[str, Any]) -> Entrypoint:
    """A class is built once from `args`; a function gets `args` on every call."""
    obj = resolve(entrypoint)
    if isinstance(obj, type):
        try:
            instance = obj(**args)
        except Exception as error:
            raise SimpleEvalError(
                f"Cannot build '{entrypoint}' from {args}: {type(error).__name__}: {error}"
            ) from error
        hook = getattr(instance, "provenance", None)
        close = getattr(instance, "close", None)
        return Entrypoint(
            call=instance,
            provenance=dict(hook()) if callable(hook) else {},
            close=close if callable(close) else None,
        )
    if not callable(obj):
        raise SimpleEvalError(f"'{entrypoint}' does not resolve to a callable.")
    return Entrypoint(call=partial(obj, **args) if args else obj)


def resolve(entrypoint: str) -> Any:
    if ":" not in entrypoint:
        raise SimpleEvalError(f"Entrypoint '{entrypoint}' must be 'module:attribute'.")
    module_name, attr = entrypoint.split(":", maxsplit=1)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        raise SimpleEvalError(
            f"Cannot import '{module_name}' for '{entrypoint}': {error}. "
            "Is its directory listed under `pythonpath` in the spec?"
        ) from error
    obj = getattr(module, attr, None)
    if obj is None:
        raise SimpleEvalError(f"'{module_name}' has no attribute '{attr}'.")
    return obj


def extend_sys_path(paths: list[Path]) -> None:
    """Put the spec's `pythonpath` entries first on sys.path, once each."""
    for path in reversed(paths):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    importlib.invalidate_caches()
