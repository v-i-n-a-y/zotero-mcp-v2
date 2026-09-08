# Copyright 2026 Vinay

"""Scite.ai citation intelligence.

Scite classifies citations as supporting, contrasting or merely mentioning,
and flags retractions. That is genuinely different information from a citation
count, and it is the reason to keep this integration.

It is an optional toolset because it calls a third-party service on the user's
behalf. Nothing here needs a Scite account; the endpoints used are public.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.errors import InvalidInput
from zotero_mcp.tools import _common as c

logger = logging.getLogger(__name__)

SCITE_TALLIES = "https://api.scite.ai/tallies"


def _tallies(dois: list[str], settings: Any) -> dict[str, dict[str, Any]]:
    """Citation tallies for a batch of DOIs, keyed by DOI."""
    from zotero_mcp.external import http

    if not dois:
        return {}
    payload = http.get(SCITE_TALLIES, settings=settings, params={"dois": ",".join(dois[:100])})
    if not isinstance(payload, dict):
        return {}
    tallies = payload.get("tallies")
    if isinstance(tallies, list):
        return {t.get("doi", "").lower(): t for t in tallies if t.get("doi")}
    if isinstance(tallies, dict):
        return {k.lower(): v for k, v in tallies.items()}
    return {}


def _render(doi: str, tally: dict[str, Any]) -> str:
    supporting = tally.get("supporting", 0)
    contrasting = tally.get("contrasting", 0)
    mentioning = tally.get("mentioning", 0)
    total = tally.get("total", supporting + contrasting + mentioning)
    line = (
        f"- `{doi}`: **{total}** citing statements "
        f"({supporting} supporting, {contrasting} contrasting, {mentioning} mentioning)"
    )
    if tally.get("retracted"):
        line += "  \n  **RETRACTED**"
    return line


@mcp.tool(
    name="scite_enrich_item",
    description=(
        "Citation tallies for one item from Scite.ai: how many later papers support, "
        "contrast with, or merely mention it. Needs the item to have a DOI."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
    tags={"scite"},
)
@c.tool
def scite_enrich_item(
    item_key: Annotated[str, Field(description="The item to look up.")],
) -> Any:
    """Citation tallies for an item."""
    key = c.resolve_item_key(item_key)
    item = c.require_item(key)
    data = item.get("data") or {}
    doi = (data.get("DOI") or "").strip()
    if not doi:
        raise InvalidInput(
            f"Item {key} has no DOI, and Scite looks things up by DOI.",
            hint="Add a DOI to the item first.",
        )

    tallies = _tallies([doi], c.runtime().config.network)
    tally = tallies.get(doi.lower())
    if not tally:
        return c.respond(
            f"# Citation tallies\n\nScite has no data for `{doi}`.",
            {"doi": doi, "tally": None},
        )

    markdown = (
        f"# Citation tallies: {data.get('title', key)}\n\n{_render(doi, tally)}\n\n"
        "*Source: scite.ai. Supporting and contrasting counts come from classified "
        "citation statements, not from citation counts.*"
    )
    return c.respond(markdown, {"doi": doi, "tally": tally})


@mcp.tool(
    name="scite_enrich_search",
    description=(
        "Run a library search and annotate every result with its Scite citation "
        "tallies, so the most supported work is visible at a glance."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
    tags={"scite"},
)
@c.tool
def scite_enrich_search(
    query: Annotated[str, Field(description="What to search the library for.")],
    limit: Annotated[int | str | None, Field(description="How many results to enrich.")] = None,
) -> Any:
    """Search, then annotate with citation tallies."""
    size = c.page_size(limit or 10)
    page = c.backend().get_items(ItemQuery(query=query, limit=size))
    items = page.items

    by_doi = {
        (item.get("data") or {}).get("DOI", "").strip().lower(): item
        for item in items
        if (item.get("data") or {}).get("DOI")
    }
    tallies = _tallies(list(by_doi), c.runtime().config.network)

    lines = [f"# Cited support: {query}", ""]
    enriched = []
    for item in items:
        data = item.get("data") or {}
        doi = (data.get("DOI") or "").strip().lower()
        tally = tallies.get(doi)
        title = data.get("title", "Untitled")
        lines.append(f"**{title}** (`{item.get('key')}`)")
        lines.append(_render(doi, tally) if tally else "- no Scite data")
        lines.append("")
        enriched.append({"key": item.get("key"), "doi": doi or None, "tally": tally})

    if not items:
        lines.append("No matching items.")
    lines.append("*Source: scite.ai.*")
    return c.respond("\n".join(lines), {"query": query, "results": enriched})


@mcp.tool(
    name="scite_check_retractions",
    description=(
        "Scan the library for items Scite reports as retracted or corrected. Worth "
        "running before submitting a manuscript."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": True},
    tags={"scite"},
)
@c.tool
def scite_check_retractions(
    collection_key: Annotated[
        str | None, Field(description="Restrict the scan to one collection.")
    ] = None,
    scan_limit: Annotated[
        int | str | None, Field(description="How many items to check. Defaults to 200.")
    ] = None,
) -> Any:
    """Check for retracted work."""
    ceiling = int(scan_limit) if scan_limit else 200
    settings = c.runtime().config.network

    dois: dict[str, dict[str, Any]] = {}
    offset = 0
    while len(dois) < ceiling:
        raw = c.backend().get_items(
            ItemQuery(
                collection_key=collection_key, item_type="-attachment", offset=offset, limit=100
            )
        )
        if not raw.items:
            break
        offset += len(raw.items)
        for item in raw.items:
            doi = ((item.get("data") or {}).get("DOI") or "").strip().lower()
            if doi:
                dois[doi] = item
        if raw.total is not None and offset >= raw.total:
            break

    flagged = []
    doi_list = list(dois)
    # Scite's endpoint takes a batch; a library-sized scan is a handful of
    # requests rather than one per item.
    for start in range(0, len(doi_list), 100):
        batch = doi_list[start : start + 100]
        for doi, tally in _tallies(batch, settings).items():
            if tally.get("retracted") or tally.get("editorialNotice"):
                item = dois.get(doi) or {}
                flagged.append(
                    {
                        "key": item.get("key"),
                        "doi": doi,
                        "title": (item.get("data") or {}).get("title", "Untitled"),
                        "retracted": bool(tally.get("retracted")),
                    }
                )

    lines = ["# Retraction check", "", f"*Checked {len(dois)} items with DOIs.*", ""]
    if flagged:
        lines.append(f"**{len(flagged)} item(s) flagged:**")
        lines += [
            f"- `{f['key']}` {f['title']}  \n  `{f['doi']}` "
            f"{'**retracted**' if f['retracted'] else 'editorial notice'}"
            for f in flagged
        ]
    else:
        lines.append("Nothing flagged.")
    lines += ["", "*Source: scite.ai. Verify anything flagged against the publisher.*"]

    return c.respond("\n".join(lines), {"checked": len(dois), "flagged": flagged})


__all__ = ["scite_check_retractions", "scite_enrich_item", "scite_enrich_search"]
