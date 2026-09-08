# Copyright 2026 Vinay

"""Process-wide state: the resolved configuration and the live backend.

Tools need three things at call time, all decided at startup: the config, the
backend, and whether structured output is wanted. Passing them through every
signature would be noise, and reaching for globals from inside each tool is
what made the predecessors untestable. One accessor, one setter, one reset.

Library switching replaces the whole :class:`Runtime`, so a switched library is
not a flag some code paths honour and others forget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from zotero_mcp.backends.base import LibraryBackend
from zotero_mcp.config import LibrarySettings, LimitSettings, ZoteroConfig, load_config
from zotero_mcp.errors import InternalError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    """Everything a tool needs that was decided before the tool was called."""

    config: ZoteroConfig
    backend: LibraryBackend

    @property
    def limits(self) -> LimitSettings:
        return self.config.limits

    @property
    def structured(self) -> bool:
        return self.config.surface.structured_output

    def with_backend(self, backend: LibraryBackend, library: LibrarySettings) -> Runtime:
        """A copy bound to a different library."""
        return replace(self, backend=backend, config=replace(self.config, library=library))


_runtime: Runtime | None = None

#: Recorded when startup could not build a backend, and re-raised by the first
#: tool that needs one. Keeping the original error means the caller is told
#: "Zotero is not running, start it" rather than a generic "not initialised".
_startup_error: Exception | None = None


def get_runtime() -> Runtime:
    """The active runtime.

    Raises:
        The error that prevented startup, if there was one, so the caller gets
        the actionable message rather than a symptom of it.
        InternalError: No runtime and no recorded startup failure, which can
            only mean a tool ran before the lifespan handler completed.
    """
    if _runtime is not None:
        return _runtime
    if _startup_error is not None:
        raise _startup_error
    raise InternalError(
        "The Zotero backend has not been initialised.",
        hint="This is a bug: a tool ran before server startup completed.",
    )


def set_startup_error(error: Exception | None) -> None:
    """Record why startup could not build a backend."""
    global _startup_error
    _startup_error = error


def startup_error() -> Exception | None:
    return _startup_error


def set_runtime(runtime: Runtime) -> None:
    global _runtime, _startup_error
    _runtime = runtime
    _startup_error = None


def reset_runtime() -> None:
    """Drop the active runtime. For tests and for shutdown."""
    global _runtime, _startup_error
    _runtime = None
    _startup_error = None


def initialise(
    config: ZoteroConfig | None = None, *, overrides: dict[str, Any] | None = None
) -> Runtime:
    """Load configuration, build the backend, and install the runtime."""
    from zotero_mcp.backends.factory import build_backend

    resolved = config or load_config(overrides=overrides)
    runtime = Runtime(config=resolved, backend=build_backend(resolved))
    set_runtime(runtime)
    logger.info("Zotero backend ready: %s", runtime.backend.name)
    return runtime


__all__ = [
    "Runtime",
    "get_runtime",
    "initialise",
    "reset_runtime",
    "set_runtime",
    "set_startup_error",
    "startup_error",
]
