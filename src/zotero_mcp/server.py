"""Construct the FastMCP application.

Kept separate from the CLI so a test — or an embedding host — can build the
server object and inspect its registered tools without spawning a process or
opening stdio.
"""

from __future__ import annotations

import logging

from zotero_mcp import __version__
from zotero_mcp.backends import make_backend
from zotero_mcp.config import ZoteroConfig, load_config
from zotero_mcp.tools import register_tools

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """\
Read access to a Zotero library. Use `search_library` to find items (each hit
carries the `key` needed for follow-ups), `get_item` for one item's full
record, `list_collections` and `collection_items` to browse, `list_tags` for
tags, `library_stats` for an overview, and `server_health` to check the
library is reachable. Every list is paged: pass the returned `next_cursor` back
unchanged to get the next page.
"""


def build_server(config: ZoteroConfig | None = None):
    """Build and return the configured FastMCP server (not yet running)."""
    from fastmcp import FastMCP

    config = config or load_config()
    backend = make_backend(config)

    mcp = FastMCP(name="zotero", version=__version__, instructions=_INSTRUCTIONS)
    register_tools(mcp, config, backend)
    logger.info(
        "zotero-mcp %s ready: backend=%s library=%s/%s",
        __version__,
        "local" if backend.local else "web",
        backend.library_type,
        backend.library_id,
    )
    return mcp


__all__ = ["build_server"]
