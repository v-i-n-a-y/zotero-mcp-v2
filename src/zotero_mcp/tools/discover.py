# Copyright 2026 Vinay

"""Corpus-level questions, built on the semantic index.

These answer things a per-item tool cannot: what else in the library is like
this, and where the library is thin on a topic. Both need the index, and both
say so plainly when it is missing rather than degrading into a keyword search
that would answer a different question.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.errors import NotFound
from zotero_mcp.render import render_result_page
from zotero_mcp.tools import _common as c

_NO_INDEX = (
    "This needs the semantic index, which has not been built for this library. "
    "Run 'zotero-mcp index build' first."
)


@mcp.tool(
    name="zotero_find_related",
    description=(
        "Find items in the library similar to a given one, by meaning rather than "
        "by shared words. Useful for 'what else have I got on this'."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"discovery"},
)
@c.tool
def zotero_find_related(
    item_key: Annotated[str, Field(description="The item to find neighbours for.")],
    limit: Annotated[int | str | None, Field(description="How many neighbours to return.")] = None,
    collection_key: Annotated[
        str | None, Field(description="Restrict the search to one collection.")
    ] = None,
) -> Any:
    """Find similar items."""
    from zotero_mcp.index import query as index_query

    key = c.resolve_item_key(item_key)
    item = c.require_item(key)
    data = item.get("data") or {}

    from zotero_mcp.index.builder import document_for

    seed = document_for(item)
    if not seed:
        raise NotFound(f"Item {key} has no title or abstract to compare against.")

    size = c.page_size(limit)
    # Over-fetch by one, because the seed item is its own nearest neighbour.
    hits = index_query.search(seed, limit=size + 1, collection_key=collection_key)
    if hits is None:
        raise NotFound(_NO_INDEX)

    hits = [hit for hit in hits if hit.get("key") != key][:size]

    from zotero_mcp.backends.base import RawPage

    page = c.build_result_page(
        RawPage(items=hits, total=len(hits), offset=0),
        fingerprint={"view": "related", "key": key},
        size=size,
    )
    for summary, raw in zip(page.items, hits, strict=False):
        summary.score = raw.get("_score")
        summary.matched_text = raw.get("_matched_text")

    heading = f"Similar to: {data.get('title', key)}"
    return c.respond(render_result_page(page, heading=heading), page)


@mcp.tool(
    name="zotero_coverage",
    description=(
        "Assess how well the library covers a topic: the closest items, how strong "
        "the match is, and whether there is a gap worth filling."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"discovery"},
)
@c.tool
def zotero_coverage(
    topic: Annotated[str, Field(description="The topic or research question to assess.")],
    limit: Annotated[int | str | None, Field(description="How many items to consider.")] = None,
) -> Any:
    """Assess topic coverage."""
    from zotero_mcp.index import query as index_query

    size = c.page_size(limit or 15)
    hits = index_query.search(topic, limit=size)
    if hits is None:
        raise NotFound(_NO_INDEX)

    scores = [hit.get("_score") or 0.0 for hit in hits]
    best = max(scores, default=0.0)
    strong = sum(1 for score in scores if score >= 0.5)

    # Thresholds are advisory and stated as such: cosine similarity has no
    # absolute meaning, and presenting a number as a verdict would be false
    # precision.
    if not hits or best < 0.3:
        verdict = "**Thin.** Nothing in the library matches this topic closely."
    elif strong >= 5:
        verdict = f"**Well covered.** {strong} items match this topic strongly."
    elif strong >= 1:
        verdict = f"**Partial.** {strong} item(s) match closely; the rest are peripheral."
    else:
        verdict = "**Weak.** Some items are related, but none matches closely."

    from zotero_mcp.backends.base import RawPage

    page = c.build_result_page(
        RawPage(items=hits, total=len(hits), offset=0),
        fingerprint={"view": "coverage", "topic": topic},
        size=size,
    )
    for summary, raw in zip(page.items, hits, strict=False):
        summary.score = raw.get("_score")
        summary.matched_text = raw.get("_matched_text")

    markdown = (
        f"# Coverage: {topic}\n\n{verdict}\n\n"
        f"*Best similarity {best:.2f}; {strong} of {len(hits)} above 0.5. "
        "Similarity scores are relative, not absolute.*\n\n"
        + render_result_page(page, heading="Closest items").split("\n", 1)[-1]
    )
    return c.respond(
        markdown,
        {
            "topic": topic,
            "verdict": verdict,
            "best_score": round(best, 4),
            "strong_matches": strong,
            "items": [i.model_dump(mode="json", exclude_none=True) for i in page.items],
        },
    )


__all__ = ["zotero_coverage", "zotero_find_related"]
