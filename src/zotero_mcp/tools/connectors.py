# Copyright 2026 Vinay

"""The ChatGPT deep-research connector contract.

OpenAI's connector specification requires two tools named exactly ``search``
and ``fetch``, returning a fixed JSON shape. Those names are far too generic to
expose by default: over stdio they sit alongside every other MCP server the
user has installed, and a model asked to "search" has no way to know which one
means their Zotero library.

So they live in the ``chatgpt-connector`` toolset, off unless asked for, and
are thin adapters over the real tools rather than a second implementation.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.mapping import zotero_uri
from zotero_mcp.tools import _common as c


@mcp.tool(
    name="search",
    description=(
        "Search the connected Zotero research library. Returns JSON records with an "
        "id, a title and a URL for each match."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"chatgpt-connector"},
)
@c.tool
def connector_search(
    query: Annotated[str, Field(description="What to look for in the library.")],
) -> str:
    """Connector-shaped search."""
    from zotero_mcp.index import query as index_query

    limit = 10
    hits = index_query.search(query, limit=limit)
    if hits is None:
        page = c.backend().get_items(ItemQuery(query=query, limit=limit))
        hits = page.items

    library = c.backend().library_ref()
    results = []
    for item in hits:
        key = item.get("key") or ""
        data = item.get("data") or {}
        results.append(
            {
                "id": key,
                "title": data.get("title") or "Untitled",
                "url": zotero_uri(key, library) if key else "",
            }
        )
    return json.dumps({"results": results}, separators=(",", ":"))


@mcp.tool(
    name="fetch",
    description=(
        "Fetch one item from the connected Zotero library by id, returning its "
        "metadata and available text as JSON."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"chatgpt-connector"},
)
@c.tool
def connector_fetch(
    id: Annotated[str, Field(description="The item id returned by search.")],
) -> str:
    """Connector-shaped fetch."""
    from zotero_mcp.content.reader import read_content
    from zotero_mcp.errors import ZoteroMcpError
    from zotero_mcp.render import render_item_detail

    key = c.resolve_item_key(id)
    item = c.require_item(key)

    from zotero_mcp.mapping import to_item_detail

    detail = to_item_detail(item, library=c.backend().library_ref())
    text = render_item_detail(detail)

    try:
        chunk = read_content(c.backend(), key, limits=c.limits())
        text = f"{text}\n\n{chunk.text}"
    except ZoteroMcpError:
        # No readable attachment is normal, and the metadata alone is still a
        # useful answer to a fetch.
        pass

    return json.dumps(
        {
            "id": key,
            "title": detail.title,
            "text": text,
            "url": detail.zotero_uri or "",
            "metadata": {
                "itemType": detail.item_type,
                "date": detail.date or "",
                "doi": detail.doi or "",
            },
        },
        separators=(",", ":"),
    )


__all__: list[str] = ["connector_fetch", "connector_search"]
