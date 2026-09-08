# Copyright 2026 Vinay

"""MCP resources: library content a client can attach as context directly.

Resources are the half of MCP the predecessors never implemented. They let a
host attach a collection listing or a single item as context without the model
spending a tool call and a round trip to get it, which is exactly right for
material the user already knows they want in the conversation.

Everything here is read-only and cheap. Anything that would need paging or a
budget belongs in a tool, where the caller can control it.
"""

from __future__ import annotations

import logging

from zotero_mcp.app import mcp

logger = logging.getLogger(__name__)

#: Resources cannot paginate, so a listing has to be bounded at the source.
_MAX_ROWS = 500


@mcp.resource(
    "zotero://collections",
    name="Zotero collections",
    description="Every collection in the active library, with its key and path.",
    mime_type="text/markdown",
)
def collections_resource() -> str:
    """The collection hierarchy as markdown."""
    from zotero_mcp.mapping import collection_paths, to_collection_ref
    from zotero_mcp.models import CollectionPage
    from zotero_mcp.render import render_collections
    from zotero_mcp.runtime import get_runtime

    try:
        raw = get_runtime().backend.get_all_collections()
    except Exception as exc:  # noqa: BLE001 (a resource must render, not raise)
        return f"# Zotero collections\n\nCould not load collections: {exc}"

    paths = collection_paths(raw)
    refs = [to_collection_ref(item, path=paths.get(item.get("key") or "")) for item in raw]
    refs.sort(key=lambda r: (r.path or r.name).lower())
    page = CollectionPage(collections=refs[:_MAX_ROWS], total=len(refs), returned=len(refs))
    return render_collections(page, heading="Zotero collections")


@mcp.resource(
    "zotero://items/{item_key}",
    name="Zotero item",
    description="Full metadata for one item, by its 8-character key.",
    mime_type="text/markdown",
)
def item_resource(item_key: str) -> str:
    """One item's metadata as markdown."""
    from zotero_mcp.mapping import to_item_detail
    from zotero_mcp.render import render_item_detail
    from zotero_mcp.runtime import get_runtime

    try:
        backend = get_runtime().backend
        item = backend.get_item(item_key)
        if item is None:
            return f"# Zotero item\n\nNo item with key `{item_key}`."
        children = backend.get_children(item_key)
        detail = to_item_detail(item, library=backend.library_ref(), children=children)
        return render_item_detail(detail)
    except Exception as exc:  # noqa: BLE001
        return f"# Zotero item\n\nCould not load `{item_key}`: {exc}"


@mcp.resource(
    "zotero://collections/{collection_key}/items",
    name="Zotero collection items",
    description="The items in one collection.",
    mime_type="text/markdown",
)
def collection_items_resource(collection_key: str) -> str:
    """A collection's items as markdown."""
    from zotero_mcp.backends.base import ItemQuery
    from zotero_mcp.mapping import to_item_summary
    from zotero_mcp.models import ResultPage
    from zotero_mcp.render import render_result_page
    from zotero_mcp.runtime import get_runtime

    try:
        active = get_runtime()
        raw = active.backend.get_items(
            ItemQuery(collection_key=collection_key, limit=100, item_type="-attachment")
        )
        summaries = [
            to_item_summary(
                item,
                abstract_chars=active.limits.abstract_preview_chars,
                library=active.backend.library_ref(),
            )
            for item in raw.items
        ]
        page = ResultPage(items=summaries, total=raw.total, returned=len(summaries))
        return render_result_page(page, heading="Collection items")
    except Exception as exc:  # noqa: BLE001
        return f"# Collection items\n\nCould not load `{collection_key}`: {exc}"


@mcp.resource(
    "zotero://tags",
    name="Zotero tags",
    description="Tags in the active library, with item counts.",
    mime_type="text/markdown",
)
def tags_resource() -> str:
    """The library's tags as markdown."""
    from zotero_mcp.mapping import to_tag_count
    from zotero_mcp.render import render_tags
    from zotero_mcp.runtime import get_runtime

    try:
        raw = get_runtime().backend.get_tags(limit=_MAX_ROWS)
        return render_tags([to_tag_count(item) for item in raw.items], heading="Zotero tags")
    except Exception as exc:  # noqa: BLE001
        return f"# Zotero tags\n\nCould not load tags: {exc}"


__all__ = [
    "collection_items_resource",
    "collections_resource",
    "item_resource",
    "tags_resource",
]
