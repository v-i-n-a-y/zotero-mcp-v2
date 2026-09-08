# Copyright 2026 Vinay

"""Library-level operations: browsing, statistics, switching, health.

Replaces ``zotero_list_libraries``, ``zotero_switch_library``,
``zotero_validate_library_switch``, ``zotero_get_library_stats``,
``zotero_get_recent``, ``zotero_get_items_by_type``,
``zotero_get_items_without_collection``, ``zotero_list_feeds``,
``zotero_get_feed_items`` and ``zotero_get_item_versions`` with one tool and an
action, plus a separate health tool because "is this thing working" should
never be buried in a menu.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp import schema
from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.models import HealthReport, LibraryStats
from zotero_mcp.render import render_health, render_result_page, render_stats
from zotero_mcp.tools import _common as c

logger = logging.getLogger(__name__)


@mcp.tool(
    name="zotero_library",
    description=(
        "Browse and inspect the library as a whole: recent additions, items of a "
        "given type, uncollected items, statistics, available libraries, and "
        "switching between them."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    tags={"library", "core"},
)
@c.tool
def zotero_library(
    action: Annotated[
        Literal[
            "recent",
            "stats",
            "list",
            "switch",
            "by_type",
            "uncollected",
            "saved_searches",
            "feeds",
            "feed_items",
            "versions",
        ],
        Field(description="What to do. 'recent' is the usual way to see what is new."),
    ] = "recent",
    item_type: Annotated[str | None, Field(description="Item type, for action='by_type'.")] = None,
    library_id: Annotated[
        str | None, Field(description="Library to switch to, from action='list'.")
    ] = None,
    library_type: Annotated[
        Literal["user", "group"] | None, Field(description="Whether that library is a group.")
    ] = None,
    feed_id: Annotated[
        int | str | None,
        Field(description="Feed library id, for action='feed_items'. From action='feeds'."),
    ] = None,
    since_version: Annotated[
        int | None,
        Field(description="For action='versions': only items changed after this version."),
    ] = None,
    limit: Annotated[int | str | None, Field(description="Page size.")] = None,
    cursor: Annotated[str | None, Field(description="Continue a previous listing.")] = None,
) -> Any:
    """Library-level operations."""
    backend = c.backend()

    if action == "list":
        libraries = backend.list_libraries()
        active = backend.library_ref()
        lines = ["# Libraries", ""]
        for library in libraries:
            marker = " **(active)**" if library.library_id == active.library_id else ""
            lines.append(
                f"- **{library.name or library.library_id}** "
                f"(`{library.library_id}`, {library.library_type}){marker}"
            )
        lines += ["", "*Switch with zotero_library(action='switch', library_id=...).*"]
        return c.respond(
            "\n".join(lines),
            {
                "libraries": [lib.model_dump(mode="json", exclude_none=True) for lib in libraries],
                "active": active.model_dump(mode="json", exclude_none=True),
            },
        )

    if action == "switch":
        return _switch(library_id, library_type)

    if action == "stats":
        return _stats()

    if action == "feeds":
        return _feeds()

    if action == "feed_items":
        return _feed_items(feed_id, c.page_size(limit))

    if action == "versions":
        versions = backend.get_item_versions(since=since_version)
        heading = (
            f"# Item versions since {since_version}"
            if since_version is not None
            else "# Item versions"
        )
        lines = [heading, "", f"{len(versions)} item(s).", ""]
        lines += [f"- `{key}` v{version}" for key, version in sorted(versions.items())[:200]]
        return c.respond("\n".join(lines), {"versions": versions, "since": since_version})

    if action == "saved_searches":
        searches = backend.get_saved_searches()
        lines = ["# Saved searches", ""]
        for search in searches:
            data = search.get("data") or {}
            conditions = data.get("conditions") or []
            lines.append(
                f"- **{data.get('name', 'Untitled')}** (`{search.get('key', '')}`) "
                f"- {len(conditions)} condition(s)"
            )
        if not searches:
            lines.append("None saved in this library.")
        return c.respond("\n".join(lines), {"saved_searches": searches})

    size = c.page_size(limit)

    if action == "recent":
        spec = ItemQuery(sort="dateAdded", direction="desc", top_level_only=True, limit=size)
        heading = "Recently added"
    elif action == "by_type":
        if not item_type:
            raise InvalidInput(
                "action='by_type' needs an item_type.",
                hint=f"For example: {', '.join(schema.creatable_item_types()[:8])}",
            )
        if not schema.is_item_type(item_type):
            raise InvalidInput(
                f"{item_type!r} is not a Zotero item type.",
                hint=f"Did you mean one of: {', '.join(schema.suggest_field('journalArticle', item_type) or schema.creatable_item_types()[:8])}?",
            )
        spec = ItemQuery(item_type=item_type, sort="dateAdded", direction="desc", limit=size)
        heading = f"Items of type {item_type}"
    else:
        return _uncollected(size, cursor)

    fingerprint = {**spec.as_fingerprint(), "action": action}
    offset = c.offset_from(cursor, fingerprint)
    raw = backend.get_items(ItemQuery(**{**spec.as_fingerprint(), "offset": offset, "limit": size}))
    page = c.build_result_page(raw, fingerprint=fingerprint, size=size)
    return c.respond(render_result_page(page, heading=heading), page)


def _feeds() -> Any:
    """RSS subscriptions, which live only in the desktop client's database."""
    from zotero_mcp.backends import localdb

    feeds = localdb.get_feeds(c.runtime().config)
    if not feeds:
        return c.respond(
            "# RSS feeds\n\nNo feeds are subscribed in this Zotero installation.",
            {"feeds": []},
        )

    lines = ["# RSS feeds", ""]
    for feed in feeds:
        error = f" (last error: {feed['lastCheckError']})" if feed.get("lastCheckError") else ""
        lines += [
            f"### {feed.get('name') or 'Untitled feed'}",
            f"- **Feed id:** `{feed['libraryID']}`",
            f"- **URL:** {feed.get('url') or '(none)'}",
            f"- **Items:** {feed.get('itemCount', 0)}",
            f"- **Last checked:** {feed.get('lastCheck') or 'never'}{error}",
            "",
        ]
    lines.append("*Read items with zotero_library(action='feed_items', feed_id=...).*")
    return c.respond("\n".join(lines), {"feeds": feeds})


def _feed_items(feed_id: int | str | None, size: int) -> Any:
    """One feed's items, newest first."""
    from zotero_mcp.backends import localdb

    if feed_id is None:
        raise InvalidInput(
            "action='feed_items' needs a feed_id.",
            hint="Use zotero_library(action='feeds') to list them.",
        )
    try:
        numeric = int(feed_id)
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{feed_id!r} is not a feed id.", hint="Feed ids are numbers.") from exc

    config = c.runtime().config
    known = {feed["libraryID"] for feed in localdb.get_feeds(config)}
    if numeric not in known:
        raise NotFound(
            f"No feed with id {numeric}.",
            hint=(
                f"Known feed ids: {', '.join(str(i) for i in sorted(known)) or 'none'}."
                if known
                else "This Zotero has no feed subscriptions."
            ),
        )

    items = localdb.get_feed_items(config, numeric, limit=size)
    lines = ["# Feed items", ""]
    for item in items:
        unread = "" if item.get("readTime") else " **(unread)**"
        lines += [
            f"### {item.get('title') or 'Untitled'}{unread}",
            f"- **Authors:** {item.get('creators') or 'unknown'}",
            f"- **Added:** {item.get('dateAdded') or 'unknown'}",
        ]
        if url := item.get("url"):
            lines.append(f"- **URL:** {url}")
        if abstract := item.get("abstract"):
            lines.append(f"\n{abstract[:400]}")
        lines.append("")
    if not items:
        lines.append("This feed has no items.")
    return c.respond("\n".join(lines), {"feed_id": numeric, "items": items})


def _uncollected(size: int, cursor: str | None) -> Any:
    """Top-level items filed in no collection.

    Zotero has no server-side filter for this, so it is computed by scanning
    top-level items. Bounded to a few pages: the point is to surface a backlog
    worth tidying, not to enumerate an untidy library exhaustively.
    """
    fingerprint = {"view": "uncollected"}
    offset = c.offset_from(cursor, fingerprint)

    from zotero_mcp.backends.base import RawPage

    collected: list[dict[str, Any]] = []
    scanned = 0
    page_offset = 0
    while len(collected) < offset + size and scanned < 2_000:
        raw = c.backend().get_items(ItemQuery(top_level_only=True, offset=page_offset, limit=100))
        if not raw.items:
            break
        scanned += len(raw.items)
        page_offset += len(raw.items)
        collected.extend(
            item for item in raw.items if not (item.get("data") or {}).get("collections")
        )
        if raw.total is not None and page_offset >= raw.total:
            break

    window = collected[offset : offset + size]
    page = c.build_result_page(
        RawPage(items=window, total=len(collected) if scanned < 2_000 else None, offset=offset),
        fingerprint=fingerprint,
        size=size,
    )
    heading = "Items in no collection"
    markdown = render_result_page(page, heading=heading)
    if scanned >= 2_000:
        markdown += f"\n\n*Scanned the {scanned} most recent top-level items.*"
    return c.respond(markdown, page)


def _switch(library_id: str | None, library_type: str | None) -> Any:
    """Point the server at a different library, after checking it is readable.

    Validated before switching, because a failed switch that still changes
    state leaves every subsequent tool call failing for a reason the caller
    cannot see.
    """
    if not library_id:
        raise InvalidInput(
            "Switching needs a library_id.",
            hint="Use zotero_library(action='list') to see the options.",
        )

    from dataclasses import replace as dataclass_replace

    from zotero_mcp.backends.factory import build_backend
    from zotero_mcp.config import LibraryType
    from zotero_mcp.runtime import get_runtime, set_runtime

    active = get_runtime()
    known = {lib.library_id: lib for lib in active.backend.list_libraries()}
    chosen = known.get(str(library_id))
    resolved_type = library_type or (chosen.library_type if chosen else "user")

    candidate_library = dataclass_replace(
        active.config.library,
        library_id=str(library_id),
        library_type=LibraryType(resolved_type),
    )
    candidate_config = dataclass_replace(active.config, library=candidate_library)

    backend = build_backend(candidate_config)
    if not backend.ping():
        raise NotFound(
            f"Library {library_id} could not be read with the current credentials.",
            hint="Check the id, and that your API key has access to that group.",
        )

    set_runtime(active.with_backend(backend, candidate_library))
    name = chosen.name if chosen else library_id
    return c.respond(
        f"# Switched library\n\nNow reading **{name}** (`{library_id}`, {resolved_type}).",
        {"library_id": str(library_id), "library_type": resolved_type, "name": name},
    )


def _stats() -> Any:
    """Counts across the library.

    Every figure comes from a count query rather than by enumerating items, so
    this stays a handful of cheap requests on a library of any size.
    """
    backend = c.backend()
    library = backend.library_ref()

    def count(spec: ItemQuery) -> int:
        page = backend.get_items(spec)
        return page.total if page.total is not None else len(page.items)

    total = backend.count_items()
    if total is None:
        total = count(ItemQuery(item_type=None, limit=1))

    by_type: dict[str, int] = {}
    # Only the types worth reporting: enumerating all forty costs forty
    # requests to tell the user they have no hearings.
    for name in (
        "journalArticle",
        "book",
        "bookSection",
        "conferencePaper",
        "preprint",
        "thesis",
        "report",
        "webpage",
        "dataset",
    ):
        found = count(ItemQuery(item_type=name, limit=1))
        if found:
            by_type[name] = found

    collections = backend.get_all_collections()
    tags = backend.get_tags(limit=1)

    stats = LibraryStats(
        library=library,
        total_items=total,
        by_item_type=by_type,
        collection_count=len(collections),
        tag_count=tags.total if tags.total is not None else len(tags.items),
        attachment_count=count(ItemQuery(item_type="attachment", limit=1)),
        note_count=count(ItemQuery(item_type="note", limit=1)),
    )
    return c.respond(render_stats(stats), stats)


@mcp.tool(
    name="zotero_health",
    description=(
        "Report whether the server can actually reach Zotero, which backend it is "
        "using, and which optional features are installed. Start here when something "
        "is not working."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"admin", "core"},
)
@c.tool
def zotero_health() -> Any:
    """Diagnose the server."""
    from zotero_mcp.content.extract import available_extractors
    from zotero_mcp.index import query as index_query
    from zotero_mcp.runtime import get_runtime, startup_error

    warnings: list[str] = []

    failure = startup_error()
    if failure is not None:
        report = HealthReport(
            backend="none",
            reachable=False,
            schema_version=schema.schema_version(),
            warnings=[getattr(failure, "message", str(failure))],
        )
        hint = getattr(failure, "hint", None)
        if hint:
            report.warnings.append(hint)
        # Deliberately not routed through c.respond: that reaches for the
        # runtime, which is exactly what failed. The tool whose job is to
        # explain a broken startup must not need a working one.
        from zotero_mcp.config import LimitSettings
        from zotero_mcp.results import reply

        return reply(render_health(report), report, limit=LimitSettings().max_response_chars)

    active = get_runtime()
    backend = active.backend
    reachable = backend.ping()
    if not reachable:
        warnings.append(
            "Zotero is not responding. In local mode the desktop application must be "
            "running with its local API enabled in Settings -> Advanced."
        )

    if hasattr(backend, "ping_writer") and not backend.ping_writer():
        warnings.append("The Web API half of hybrid mode is not responding; writes will fail.")

    index_state = index_query.status()
    features = available_extractors()
    features["semantic index"] = index_state["state"] == "ready"
    features["external lookups"] = active.config.network.allow_external_services

    if not active.config.network.contact_email:
        warnings.append(
            "No contact email configured, so Unpaywall open-access lookups are skipped. "
            "Set ZOTERO_MCP_CONTACT_EMAIL to enable them."
        )

    report = HealthReport(
        backend=backend.name,
        reachable=reachable,
        library=backend.library_ref() if reachable else None,
        schema_version=schema.schema_version(),
        semantic_index=index_state["state"],
        indexed_items=index_state.get("count"),
        optional_features=features,
        warnings=warnings,
    )
    return c.respond(render_health(report), report)


__all__ = ["zotero_health", "zotero_library"]
