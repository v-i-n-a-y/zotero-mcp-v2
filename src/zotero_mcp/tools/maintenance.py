# Copyright 2026 Vinay

"""Exporting, deduplicating and indexing.

These are the tools a researcher reaches for occasionally rather than in every
session, which is why they are grouped and why two of them are behind optional
toolsets. Grouping them keeps the default surface small without hiding them.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.errors import InvalidInput, NotFound, Unsupported
from zotero_mcp.mapping import to_item_ref, year_of
from zotero_mcp.models import DuplicateGroup, WriteResult
from zotero_mcp.render import render_duplicates, render_write_result
from zotero_mcp.tools import _common as c

logger = logging.getLogger(__name__)

#: How many items may be exported in one call. Bibliographies are the one
#: place a large response is the point, but a whole library still is not.
_EXPORT_LIMIT = 200

_TITLE_NOISE = re.compile(r"[^a-z0-9 ]+")


@mcp.tool(
    name="zotero_export",
    description=(
        "Export items as BibTeX, CSL-JSON or RIS, ready to paste into a manuscript "
        "or import elsewhere. Uses Better BibTeX keys when that plugin is running."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"export", "core"},
)
@c.tool
def zotero_export(
    item_keys: Annotated[
        list[str] | str | None,
        Field(description="Items to export. Omit to export a whole collection instead."),
    ] = None,
    collection_key: Annotated[
        str | None, Field(description="Export every item in this collection.")
    ] = None,
    format: Annotated[
        Literal["bibtex", "csl-json", "ris"],
        Field(description="Output format. BibTeX for LaTeX and Overleaf."),
    ] = "bibtex",
    use_better_bibtex: Annotated[
        bool,
        Field(
            description=(
                "Prefer the Better BibTeX plugin when it is running, so keys match "
                "the ones already used in your manuscripts."
            )
        ),
    ] = True,
) -> Any:
    """Export a bibliography."""
    import json

    from zotero_mcp.external import bibtex as bib

    keys = [c.resolve_item_key(k) for k in c.normalise_list(item_keys)]

    if collection_key:
        raw = c.backend().get_items(
            ItemQuery(collection_key=collection_key, limit=_EXPORT_LIMIT, item_type="-attachment")
        )
        keys.extend(item.get("key") for item in raw.items if item.get("key"))

    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys:
        raise InvalidInput("Nothing to export.", hint="Pass item_keys, or a collection_key.")
    if len(keys) > _EXPORT_LIMIT:
        raise InvalidInput(
            f"{len(keys)} items is more than one export can return.",
            hint=f"Export at most {_EXPORT_LIMIT} at a time.",
        )

    if format == "bibtex" and use_better_bibtex and (exported := bib.from_better_bibtex(keys)):
        return c.respond(
            f"```bibtex\n{exported}\n```",
            {
                "format": "bibtex",
                "source": "Better BibTeX",
                "count": len(keys),
                "content": exported,
            },
            limit=c.limits().max_content_chars,
        )

    items = [c.require_item(key) for key in keys]

    if format == "bibtex":
        content = bib.to_bibtex(items)
        fenced = f"```bibtex\n{content}\n```"
    elif format == "ris":
        content = bib.to_ris(items)
        fenced = f"```\n{content}\n```"
    else:
        records = bib.to_csl_json(items)
        content = json.dumps(records, indent=2, ensure_ascii=False)
        fenced = f"```json\n{content}\n```"

    return c.respond(
        fenced,
        {"format": format, "source": "generated", "count": len(items), "content": content},
        limit=c.limits().max_content_chars,
    )


def _normalise_title(title: str) -> str:
    """A title reduced to what two records of the same work would share."""
    return " ".join(_TITLE_NOISE.sub(" ", (title or "").lower()).split())


def _first_author(data: dict[str, Any]) -> str:
    for creator in data.get("creators") or []:
        surname = creator.get("lastName") or creator.get("name")
        if surname:
            return surname.lower()
    return ""


@mcp.tool(
    name="zotero_duplicates",
    description=(
        "Find probable duplicate items and optionally merge them. Matching is by DOI, "
        "then by normalised title with author and year. Merging previews by default."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    tags={"duplicates"},
)
@c.tool
def zotero_duplicates(
    action: Annotated[Literal["find", "merge"], Field(description="What to do.")] = "find",
    collection_key: Annotated[
        str | None, Field(description="Restrict the scan to one collection.")
    ] = None,
    item_keys: Annotated[
        list[str] | str | None,
        Field(description="For merge: the group to merge. The first key is kept."),
    ] = None,
    scan_limit: Annotated[
        int | str | None,
        Field(description="How many items to scan when finding. Defaults to 500."),
    ] = None,
    dry_run: Annotated[bool, Field(description="Preview a merge without applying it.")] = True,
) -> Any:
    """Find or merge duplicates."""
    if action == "merge":
        return _merge(c.normalise_list(item_keys), dry_run)

    scan = c.page_size(scan_limit or 500) if scan_limit else 500
    collected: list[dict[str, Any]] = []
    offset = 0
    while len(collected) < scan:
        raw = c.backend().get_items(
            ItemQuery(
                collection_key=collection_key,
                item_type="-attachment",
                offset=offset,
                limit=min(100, scan - len(collected)),
            )
        )
        if not raw.items:
            break
        collected.extend(raw.items)
        offset += len(raw.items)
        if raw.total is not None and offset >= raw.total:
            break

    by_doi: dict[str, list[dict]] = {}
    by_title: dict[tuple[str, str, str], list[dict]] = {}
    for item in collected:
        data = item.get("data") or {}
        if data.get("itemType") in {"note", "attachment", "annotation"}:
            continue
        if doi := (data.get("DOI") or "").lower().strip():
            by_doi.setdefault(doi, []).append(item)
        title = _normalise_title(data.get("title", ""))
        if len(title) > 12:
            by_title.setdefault(
                (title, _first_author(data), year_of(data.get("date")) or ""), []
            ).append(item)

    groups: list[DuplicateGroup] = []
    seen: set[str] = set()

    for doi, items in by_doi.items():
        if len(items) < 2:
            continue
        seen.update(i.get("key") for i in items)
        groups.append(
            DuplicateGroup(
                items=[to_item_ref(i) for i in items],
                reason=f"identical DOI ({doi})",
                confidence=0.99,
                master_key=_pick_master(items),
            )
        )

    for (_title, author, year), items in by_title.items():
        if len(items) < 2 or any(i.get("key") in seen for i in items):
            continue
        # Title alone is not enough: conference and journal versions of a paper
        # legitimately share one. Requiring an author and a year makes the
        # match specific enough to act on.
        confidence = 0.9 if author and year else 0.6
        groups.append(
            DuplicateGroup(
                items=[to_item_ref(i) for i in items],
                reason="same title, author and year" if author and year else "same title",
                confidence=confidence,
                master_key=_pick_master(items),
            )
        )

    groups.sort(key=lambda g: -g.confidence)
    markdown = render_duplicates(groups, heading="Possible duplicates")
    markdown += f"\n\n*Scanned {len(collected)} items.*"
    if groups:
        markdown += (
            "\n\n*Merge a group with "
            "zotero_duplicates(action='merge', item_keys=[...], dry_run=False). "
            "The first key is kept.*"
        )
    return c.respond(
        markdown,
        {
            "groups": [g.model_dump(mode="json", exclude_none=True) for g in groups],
            "scanned": len(collected),
        },
    )


def _pick_master(items: list[dict[str, Any]]) -> str:
    """The copy worth keeping: the one carrying the most information."""

    def richness(item: dict[str, Any]) -> tuple[int, int]:
        data = item.get("data") or {}
        populated = sum(1 for value in data.values() if value not in (None, "", [], {}))
        children = (item.get("meta") or {}).get("numChildren", 0)
        return (children, populated)

    return max(items, key=richness).get("key", "")


def _merge(keys: list[str], dry_run: bool) -> Any:
    """Merge duplicates into the first key, preserving what the others carry.

    Zotero's Web API has no merge endpoint, so this is done explicitly: union
    the tags and collections onto the master, record the others as related, and
    trash them. Trash rather than delete, because a wrong merge must be
    recoverable from the Zotero UI.
    """
    resolved = [c.resolve_item_key(k) for k in keys]
    if len(resolved) < 2:
        raise InvalidInput(
            "A merge needs at least two item keys.",
            hint="The first is kept; the rest are merged into it and trashed.",
        )

    master_key, *others = resolved
    master = c.require_item(master_key)
    master_data = master.get("data") or {}

    tags = {t.get("tag") for t in master_data.get("tags") or [] if isinstance(t, dict)}
    collections = set(master_data.get("collections") or [])
    absorbed: list[str] = []

    for key in others:
        item = c.require_item(key)
        data = item.get("data") or {}
        tags.update(t.get("tag") for t in data.get("tags") or [] if isinstance(t, dict))
        collections.update(data.get("collections") or [])
        absorbed.append(key)

    patch = {
        "tags": [{"tag": tag} for tag in sorted(t for t in tags if t)],
        "collections": sorted(collections),
    }

    if dry_run:
        result = WriteResult(
            action="merge_duplicates",
            dry_run=True,
            succeeded=[master_key],
            message=(
                f"Would keep `{master_key}` ({master_data.get('title', 'Untitled')}), "
                f"union its tags and collections with {len(absorbed)} other item(s), "
                f"then move {', '.join(f'`{k}`' for k in absorbed)} to the trash. "
                "Trashed items can be restored from Zotero."
            ),
        )
        return c.respond(render_write_result(result), result)

    c.backend().update_item(master_key, patch, version=master.get("version", 0))
    versions = c.item_versions(absorbed)
    outcome = c.backend().trash_items(versions)

    result = WriteResult(
        action="merge_duplicates",
        succeeded=[master_key, *outcome.succeeded],
        failed=dict(outcome.failed),
        message=f"Kept `{master_key}`; moved {len(outcome.succeeded)} duplicate(s) to the trash.",
    )
    return c.respond(render_write_result(result), result)


@mcp.tool(
    name="zotero_index",
    description=(
        "Inspect or refresh the semantic search index. Building is normally done from "
        "the command line ('zotero-mcp index build'); this reports status and can "
        "trigger an incremental update."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    tags={"search-admin"},
)
@c.tool
def zotero_index(
    action: Annotated[
        Literal["status", "update"],
        Field(description="'status' is cheap. 'update' indexes items added since last time."),
    ] = "status",
    limit: Annotated[
        int | str | None,
        Field(description="Most items to index in one update. Defaults to 200."),
    ] = None,
) -> Any:
    """Inspect or update the semantic index."""
    from zotero_mcp.index import query as index_query

    if action == "status":
        state = index_query.status()
        lines = [
            "# Semantic index",
            "",
            f"- **State:** {state['state']}",
            f"- **Detail:** {state.get('detail', '')}",
        ]
        if state.get("count") is not None:
            lines.append(f"- **Indexed items:** {state['count']:,}")
        if state["state"] != "ready":
            lines += ["", "Build it with `zotero-mcp index build` on the command line."]
        return c.respond("\n".join(lines), state)

    from zotero_mcp.index.builder import update_index

    try:
        stats = update_index(c.runtime(), limit=int(limit) if limit else 200)
    except Unsupported as exc:
        raise exc

    lines = [
        "# Index updated",
        "",
        f"- **Items indexed:** {stats['indexed']:,}",
        f"- **Chunks written:** {stats['chunks']:,}",
        f"- **Skipped (already current):** {stats['skipped']:,}",
    ]
    if stats.get("errors"):
        lines += ["", f"*{len(stats['errors'])} item(s) could not be indexed.*"]
    return c.respond("\n".join(lines), stats)


__all__ = ["NotFound", "zotero_duplicates", "zotero_export", "zotero_index"]
