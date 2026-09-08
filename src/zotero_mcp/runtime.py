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


def get_runtime() -> Runtime:
    """The active runtime.

    Raises:
        InternalError: Called before startup completed. Always a bug: tools
            cannot run before the lifespan handler has installed a runtime.
    """
    if _runtime is None:
        raise InternalError(
            "The Zotero backend has not been initialised.",
            hint="This is a bug: a tool ran before server startup completed.",
        )
    return _runtime


def set_runtime(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def reset_runtime() -> None:
    """Drop the active runtime. For tests and for shutdown."""
    global _runtime
    _runtime = None


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


__all__ = ["Runtime", "get_runtime", "initialise", "reset_runtime", "set_runtime"]
