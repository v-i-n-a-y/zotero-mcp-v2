# Copyright 2026 Vinay

"""Importing this package registers every tool with the FastMCP app.

Registration is a side effect of import, which is FastMCP's own idiom.

Scite and the ChatGPT connector are imported unconditionally even though both
are off by default: :mod:`zotero_mcp.toolsets` disables tools by name after
registration, so they have to exist for it to find them.
"""

from zotero_mcp.tools import (
    connectors,
    discover,
    items,
    library,
    maintenance,
    notes,
    organize,
    scite,
    search,
    write,
)

__all__ = [
    "connectors",
    "discover",
    "items",
    "library",
    "maintenance",
    "notes",
    "organize",
    "scite",
    "search",
    "write",
]
