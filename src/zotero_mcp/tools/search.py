# Copyright 2026 Vinay

"""Finding things in a Zotero library.

One tool replaces six. The servers this supersedes shipped
``zotero_search_items``, ``zotero_advanced_search``, ``zotero_search_by_tag``,
``zotero_search_by_citation_key``, ``zotero_semantic_search`` and a ChatGPT
connector variant, and then spent 524 characters of tool description telling
the model which to use and how not to phrase a query. That is a routing
problem solved in prose, which is to say not solved.

Here the routing is code. ``mode="auto"`` runs a cascade, each step cheaper and
narrower than the next, under a wall-clock budget, and the result reports which
step actually matched. An empty result carries the strategies that were tried
and a concrete suggestion, because "no results" from a substring matcher is
almost always a fixable query rather than an absent paper.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.identifiers import is_zotero_key
from zotero_mcp.models import ResultPage, SearchDiagnostics
from zotero_mcp.render import render_result_page
from zotero_mcp.tools import _common as c

logger = logging.getLogger(__name__)

SearchMode = Literal["auto", "metadata", "fulltext", "semantic", "tag", "citation_key"]

_YEAR = re.compile(r"^(1[89]\d{2}|20\d{2})$")
_CITEKEY = re.compile(r"^@?([a-z][a-z0-9_.:-]{2,})$", re.IGNORECASE)

#: Diacritic and punctuation variants generated for a query. Zotero's search is
#: substring matching over stored strings, so "Muller" does not find "Müller"
#: and "Cladder-Micus" does not find "Cladder Micus".
_TRANSLITERATIONS = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "á": "a",
    "à": "a",
    "â": "a",
    "å": "a",
    "é": "e",
    "è": "e",
    "ê": "e",
    "ë": "e",
    "í": "i",
    "ì": "i",
    "î": "i",
    "ï": "i",
    "ó": "o",
    "ò": "o",
    "ô": "o",
    "ø": "o",
    "ú": "u",
    "ù": "u",
    "û": "u",
    "ç": "c",
    "ñ": "n",
}


def query_variants(query: str) -> list[str]:
    """Spellings of *query* worth trying, most faithful first.

    Deduplicated and order-preserving, so the original is always attempted
    before anything derived from it.
    """
    variants = [query]

    folded = query
    for source, target in _TRANSLITERATIONS.items():
        folded = folded.replace(source, target).replace(source.upper(), target.upper())
    variants.append(folded)

    try:
        from unidecode import unidecode

        variants.append(unidecode(query))
    except ImportError:  # pragma: no cover - unidecode is a hard dependency
        pass

    if "-" in query:
        variants.append(query.replace("-", " "))
    if "'" in query or "’" in query:
        variants.append(query.replace("'", "").replace("’", ""))

    seen: set[str] = set()
    ordered = []
    for variant in variants:
        cleaned = variant.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            ordered.append(cleaned)
    return ordered


class _Budget:
    """A wall-clock allowance shared across the steps of a cascade."""

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds
        self.exhausted = False

    def spent(self) -> bool:
        if time.monotonic() > self._deadline:
            self.exhausted = True
        return self.exhausted


def _search_variants(spec: ItemQuery, budget: _Budget) -> tuple[list[dict], int | None, list[str]]:
    """Run one query in each spelling, merging results and keeping order."""
    if not spec.query:
        page = c.backend().get_items(spec)
        return page.items, page.total, []

    merged: list[dict] = []
    seen: set[str] = set()
    total: int | None = None
    tried: list[str] = []

    for variant in query_variants(spec.query):
        if budget.spent():
            break
        tried.append(variant)
        page = c.backend().get_items(
            ItemQuery(
                **{
                    **spec.as_fingerprint(),
                    "query": variant,
                    "offset": spec.offset,
                    "limit": spec.limit,
                }
            )
        )
        # The total only means anything for the query that produced it, so it
        # is kept from the first spelling that returned rows.
        if total is None and page.total is not None and page.items:
            total = page.total
        for item in page.items:
            key = item.get("key") or ""
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
        if merged:
            break

    return merged[: spec.limit], total, tried


def _simplify(query: str) -> list[tuple[str, str]]:
    """Progressively looser rewrites of a failed query, with labels.

    Zotero's search is substring matching, so every extra word makes a query
    *stricter*. A model that adds topic words to a title is making things
    worse, and the fix is mechanical: drop back to the parts that are likely
    to appear verbatim in a title or creator field.
    """
    words = query.split()
    attempts: list[tuple[str, str]] = []
    if len(words) <= 1:
        return attempts

    year = next((w for w in words if _YEAR.match(w)), None)
    names = [w for w in words if not _YEAR.match(w) and len(w) > 2]

    if names and year:
        attempts.append((f"{names[0]} {year}", "author and year"))
    if names:
        attempts.append((names[0], "first significant word"))
    longest = max(names, key=len) if names else None
    if longest and longest != (names[0] if names else None):
        attempts.append((longest, "longest word"))
    return attempts


def _semantic(query: str, limit: int, collection_key: str | None) -> list[dict[str, Any]] | None:
    """Semantic hits, or None when no usable index exists.

    None rather than an empty list, so the cascade can distinguish "the index
    said nothing matches" from "there is no index", and report accordingly.
    """
    from zotero_mcp.index import query as index_query

    return index_query.search(query, limit=limit, collection_key=collection_key)


def _run_cascade(
    spec: ItemQuery, mode: SearchMode
) -> tuple[list[dict], int | None, SearchDiagnostics]:
    """Search, escalating through strategies until something matches."""
    budget = _Budget(c.limits().search_timeout_seconds)
    started = time.monotonic()
    attempts: list[str] = []
    query = (spec.query or "").strip()

    def finish(items, total, strategy, suggestion=None):
        return (
            items,
            total,
            SearchDiagnostics(
                strategy=strategy,
                attempts=attempts,
                query_variants=query_variants(query) if query else [],
                elapsed_seconds=round(time.monotonic() - started, 2),
                timed_out=budget.exhausted,
                suggestion=suggestion,
            ),
        )

    # A bare item key is not a search; it is a lookup that happens to arrive
    # through the search tool because that is what the model reached for.
    if mode == "auto" and is_zotero_key(query):
        item = c.backend().get_item(query.upper())
        attempts.append("item key lookup")
        if item:
            return finish([item], 1, "item key")

    if mode in {"auto", "citation_key"} and query and _CITEKEY.match(query):
        hits = _by_citation_key(query.lstrip("@"), spec)
        attempts.append(f"citation key ({len(hits)})")
        if hits:
            return finish(hits, len(hits), "citation key")
        if mode == "citation_key":
            return finish(
                [],
                0,
                "citation key",
                "No item carries that Better BibTeX key. Try a title or author instead.",
            )

    if mode in {"auto", "metadata"}:
        items, total, _ = _search_variants(spec, budget)
        attempts.append(f"title/creator/year ({len(items)})")
        if items:
            return finish(items, total, "title, creator or year")

    if mode == "metadata":
        return finish([], 0, "title, creator or year", _suggest(query))

    if mode in {"auto", "metadata"} and query and not budget.spent():
        for simplified, label in _simplify(query):
            if budget.spent():
                break
            items, total, _ = _search_variants(
                ItemQuery(
                    **{
                        **spec.as_fingerprint(),
                        "query": simplified,
                        "offset": 0,
                        "limit": spec.limit,
                    }
                ),
                budget,
            )
            attempts.append(f"{label} '{simplified}' ({len(items)})")
            if items:
                return finish(items, total, f"simplified to {label}")

    if mode in {"auto", "fulltext"} and query and not budget.spent():
        items, total, _ = _search_variants(
            ItemQuery(
                **{
                    **spec.as_fingerprint(),
                    "qmode": "everything",
                    "offset": spec.offset,
                    "limit": spec.limit,
                }
            ),
            budget,
        )
        attempts.append(f"full text ({len(items)})")
        if items:
            return finish(items, total, "full text")

    if mode == "fulltext":
        return finish([], 0, "full text", _suggest(query))

    if mode in {"auto", "semantic"} and query and not budget.spent():
        hits = _semantic(query, spec.limit, spec.collection_key)
        if hits is None:
            attempts.append("semantic (no index)")
            if mode == "semantic":
                raise NotFound(
                    "No semantic index has been built for this library.",
                    hint="Run 'zotero-mcp index build' first, or use mode='auto'.",
                )
        else:
            attempts.append(f"semantic ({len(hits)})")
            if hits:
                return finish(hits, len(hits), "semantic similarity")

    return finish([], 0, "none", _suggest(query))


def _suggest(query: str) -> str:
    """A concrete next step for an empty result.

    Substring matching fails predictably, so the advice can be specific rather
    than "try different keywords".
    """
    words = query.split()
    if len(words) > 3:
        return (
            f"This is substring matching, not web search, so every extra word narrows it. "
            f"Try just '{words[0]}' or an author surname."
        )
    if len(words) > 1:
        return "Try a single author surname, or mode='semantic' to match on meaning."
    return (
        "Try mode='fulltext' to search inside attachments, or mode='semantic' for topic matching."
    )


def _by_citation_key(key: str, spec: ItemQuery) -> list[dict[str, Any]]:
    """Items whose Better BibTeX citation key matches.

    Zotero cannot index the Extra field, so this is a scan over candidates the
    server-side search narrowed first. Matching is exact and case-insensitive
    to avoid 'smith2020' quietly returning 'smith2020a'.
    """
    from zotero_mcp.mapping import citation_key_of

    page = c.backend().get_items(
        ItemQuery(
            **{
                **spec.as_fingerprint(),
                "query": key,
                "qmode": "everything",
                "offset": 0,
                "limit": 50,
            }
        )
    )
    return [
        item
        for item in page.items
        if (citation_key_of(item.get("data") or {}) or "").lower() == key.lower()
    ]


@mcp.tool(
    name="zotero_search",
    description=(
        "Search the Zotero library and return matching items with their keys. "
        "Give it a natural query; it picks a strategy and reports which one matched. "
        "Every result carries an 8-character item key for use with the other tools."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"search", "core"},
)
@c.tool
def zotero_search(
    query: Annotated[
        str,
        Field(
            description=(
                "What to look for. Short is better: an author surname, or 'Author Year'. "
                "Metadata search is substring matching, so extra topic words make it "
                "stricter, not broader. For conceptual queries use mode='semantic'."
            )
        ),
    ] = "",
    mode: Annotated[
        SearchMode,
        Field(
            description=(
                "'auto' cascades from exact metadata to simplified queries to full text "
                "to semantic. Name a mode to skip straight to it."
            )
        ),
    ] = "auto",
    item_type: Annotated[
        str | None,
        Field(
            description=(
                "Zotero itemType filter. Supports negation and alternation: "
                "'journalArticle', '-attachment', 'book || bookSection'."
            )
        ),
    ] = "-attachment",
    tags: Annotated[
        list[str] | str | None,
        Field(description="Only items carrying all of these tags."),
    ] = None,
    collection_key: Annotated[
        str | None,
        Field(description="Restrict to one collection, by its 8-character key."),
    ] = None,
    since_version: Annotated[
        int | None,
        Field(description="Only items modified after this library version."),
    ] = None,
    sort: Annotated[
        Literal["dateAdded", "dateModified", "title", "creator", "date", "itemType"] | None,
        Field(description="Field to sort by. Defaults to Zotero's relevance order."),
    ] = None,
    direction: Annotated[
        Literal["asc", "desc"] | None, Field(description="Sort direction.")
    ] = None,
    limit: Annotated[
        int | str | None,
        Field(description="Results per page. Clamped to the configured maximum."),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="The next_cursor from a previous response, to get the next page."),
    ] = None,
) -> Any:
    """Search the library."""
    size = c.page_size(limit)
    tag_tuple = tuple(c.normalise_list(tags))

    if not query.strip() and not tag_tuple and not collection_key and since_version is None:
        raise InvalidInput(
            "A search needs a query, a tag, a collection or since_version.",
            hint="To browse instead, use zotero_library(action='recent').",
        )

    spec = ItemQuery(
        query=query.strip() or None,
        item_type=item_type,
        tags=tag_tuple,
        collection_key=collection_key,
        since=since_version,
        sort=sort,
        direction=direction,
        limit=size,
    )
    fingerprint = {**spec.as_fingerprint(), "mode": mode}
    offset = c.offset_from(cursor, fingerprint)

    if offset:
        # Paging re-runs the query that produced the cursor, not the cascade:
        # a later page must come from the same strategy as the first.
        raw = c.backend().get_items(
            ItemQuery(**{**spec.as_fingerprint(), "offset": offset, "limit": size})
        )
        items, total, diagnostics = raw.items, raw.total, None
    else:
        items, total, diagnostics = _run_cascade(spec, mode)

    from zotero_mcp.backends.base import RawPage

    page = c.build_result_page(
        RawPage(items=items, total=total, offset=offset),
        fingerprint=fingerprint,
        size=size,
        diagnostics=diagnostics,
    )
    heading = f"Search: {query}" if query else "Search"
    return c.respond(render_result_page(page, heading=heading), page)


__all__ = ["ResultPage", "query_variants", "zotero_search"]
