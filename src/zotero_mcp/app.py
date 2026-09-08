# Copyright 2026 Vinay

"""The FastMCP application and its lifecycle.

Startup does three things and reports honestly about each: resolve
configuration, build a backend, and refresh Zotero's schema in the background.

None of them is allowed to prevent the server from starting. A server that
refuses to boot because Zotero happens to be closed, or because a schema
refresh timed out, is strictly worse than one that boots, answers
``zotero_health`` accurately, and starts working the moment the problem is
fixed. The startup failure is recorded and raised by the first tool that
actually needs the backend, with the hint that says how to fix it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read, search and manage a Zotero research library.

Start with `zotero_search`; it accepts a plain question and picks a strategy.
Every result carries an 8-character item key, which is what the other tools
take. Use `zotero_read` to read a paper's text by page range rather than
whole. Writes preview by default: pass dry_run=False to apply them.
"""


def _configure_logging() -> None:
    """Log to stderr only.

    An MCP server on stdio speaks JSON-RPC over stdout; anything else written
    there corrupts the protocol stream and the client disconnects with a
    parse error that names nothing useful.
    """
    level = os.environ.get("ZOTERO_MCP_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Bring the backend up, and take background work down cleanly."""
    from zotero_mcp import runtime as runtime_module
    from zotero_mcp import schema

    _configure_logging()

    startup_error: Exception | None = None
    try:
        runtime_module.initialise()
    except Exception as exc:  # noqa: BLE001 (recorded and re-raised at first use)
        startup_error = exc
        runtime_module.set_startup_error(exc)
        logger.warning("Zotero backend unavailable at startup: %s", exc)

    # The vendored schema is always correct enough to serve from, so a refresh
    # is an optimisation and never blocks readiness.
    refresh_task = asyncio.create_task(asyncio.to_thread(schema.refresh))

    try:
        yield {"startup_error": startup_error}
    finally:
        if not refresh_task.done():
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        runtime_module.reset_runtime()


mcp: FastMCP = FastMCP(
    name="Zotero",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)


__all__ = ["INSTRUCTIONS", "mcp"]
