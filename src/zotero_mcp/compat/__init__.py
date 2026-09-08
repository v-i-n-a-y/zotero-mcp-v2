# Copyright 2026 Vinay

"""Every pre-1.0 tool name, as a thin alias over the consolidated surface.

Importing this module registers fifty-odd extra tools, which is exactly the
context-window cost the consolidation exists to avoid. That is why it is off
unless asked for, by ``ZOTERO_MCP_COMPAT=1`` or the ``compat`` toolset: the
point is that an existing installation keeps working unchanged while it
migrates, not that the old surface is a supported way to use this server.

Every alias is a call to the consolidated tool and nothing else. There is no
second implementation of anything here, so a fix to a real tool reaches its
aliases without being ported, which is the failure mode a compatibility layer
usually has.

Two names, ``zotero_update_item`` and ``zotero_get_annotations``, exist on both
surfaces with different signatures. They are re-registered here accepting
either shape, so a caller written against the old server and one written
against this one both work.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.errors import InvalidInput
from zotero_mcp.tools import _common as c
from zotero_mcp.tools import items as items_tools
from zotero_mcp.tools import library as library_tools
from zotero_mcp.tools import maintenance as maintenance_tools
from zotero_mcp.tools import notes as notes_tools
from zotero_mcp.tools import organize as organize_tools
from zotero_mcp.tools import search as search_tools
from zotero_mcp.tools import write as write_tools

logger = logging.getLogger(__name__)

#: Appended to every alias's description, so a model reading the tool list can
#: see which surface it is on and prefer the current one.
_LEGACY = " (Legacy name, kept for compatibility. Prefer {replacement}.)"


def _alias(name: str, description: str, replacement: str):
    """Register one legacy name over a consolidated tool."""

    def decorate(func):
        return mcp.tool(
            name=name,
            description=description + _LEGACY.format(replacement=replacement),
            annotations={"readOnlyHint": False, "openWorldHint": False},
            tags={"compat"},
        )(c.tool(func))

    return decorate


def _unregister(name: str) -> None:
    """Drop a consolidated registration so an alias can take its name."""
    for target in (getattr(mcp, "local_provider", None), mcp):
        remove = getattr(target, "remove_tool", None)
        if remove is None:
            continue
        try:
            remove(name)
        except Exception:  # noqa: BLE001 (not registered is the same as removed)
            logger.debug("compat: %s was not registered", name)
        return


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@_alias("zotero_search_items", "Search the Zotero library by title and creator.", "zotero_search")
def zotero_search_items(
    query: str,
    qmode: str = "titleCreatorYear",
    item_type: str | None = None,
    limit: int | str | None = None,
    tag: str | None = None,
    collection_key: str | None = None,
) -> Any:
    return search_tools.zotero_search(
        query=query,
        mode="fulltext" if qmode == "everything" else "metadata",
        item_type=item_type or "-attachment",
        tags=tag,
        collection_key=collection_key,
        limit=limit,
    )


@_alias("zotero_search_by_tag", "Find every item carrying a tag.", "zotero_search")
def zotero_search_by_tag(
    tag: str,
    item_type: str | None = None,
    limit: int | str | None = None,
    collection_key: str | None = None,
) -> Any:
    return search_tools.zotero_search(
        query="",
        tags=tag,
        item_type=item_type or "-attachment",
        collection_key=collection_key,
        limit=limit,
    )


@_alias(
    "zotero_search_by_citation_key",
    "Find an item by its Better BibTeX citation key.",
    "zotero_search(mode='citation_key')",
)
def zotero_search_by_citation_key(citekey: str) -> Any:
    return search_tools.zotero_search(query=citekey, mode="citation_key")


@_alias(
    "zotero_semantic_search",
    "Search the library by meaning rather than by shared words.",
    "zotero_search(mode='semantic')",
)
def zotero_semantic_search(
    query: str,
    limit: int | str | None = None,
    filters: dict[str, Any] | None = None,
) -> Any:
    filters = filters or {}
    return search_tools.zotero_search(
        query=query,
        mode="semantic",
        limit=limit,
        item_type=filters.get("item_type") or "-attachment",
        tags=filters.get("tag") or filters.get("tags"),
        collection_key=filters.get("collection_key"),
    )


#: Legacy advanced-search conditions that map onto a query this server can run.
_CONDITION_FIELDS = {
    "title": "query",
    "creator": "query",
    "quicksearch-titleCreatorYear": "query",
    "quicksearch-everything": "everything",
    "tag": "tag",
    "itemType": "item_type",
    "collection": "collection_key",
}


@_alias(
    "zotero_advanced_search",
    "Search with a list of Zotero saved-search conditions.",
    "zotero_search",
)
def zotero_advanced_search(
    conditions: list[dict[str, Any]],
    join_mode: str = "all",
    sort_by: str | None = None,
    sort_direction: str | None = None,
    limit: int | str | None = None,
) -> Any:
    if join_mode not in {"all", "any"}:
        raise InvalidInput(f"join_mode must be 'all' or 'any', not {join_mode!r}.")
    if join_mode == "any":
        # Zotero's own API cannot OR conditions server-side either, and
        # pretending to would silently return an AND of them.
        raise InvalidInput(
            "join_mode='any' is not supported.",
            hint="Run one zotero_search per condition and combine the results.",
        )

    terms: list[str] = []
    tags: list[str] = []
    item_type: str | None = None
    collection_key: str | None = None
    everything = False
    unsupported: list[str] = []

    for condition in conditions or []:
        field = str(condition.get("condition") or condition.get("field") or "")
        value = str(condition.get("value") or "")
        target = _CONDITION_FIELDS.get(field)
        if target is None or not value:
            if field:
                unsupported.append(field)
            continue
        if target in {"query", "everything"}:
            terms.append(value)
            everything = everything or target == "everything"
        elif target == "tag":
            tags.append(value)
        elif target == "item_type":
            item_type = value
        else:
            collection_key = value

    if unsupported:
        raise InvalidInput(
            f"Cannot search on: {', '.join(sorted(set(unsupported)))}.",
            hint=f"Supported conditions: {', '.join(sorted(_CONDITION_FIELDS))}.",
        )
    if not terms and not tags and not collection_key:
        raise InvalidInput("No usable conditions were given.")

    return search_tools.zotero_search(
        query=" ".join(terms),
        mode="fulltext" if everything else "metadata",
        item_type=item_type or "-attachment",
        tags=tags or None,
        collection_key=collection_key,
        sort=sort_by,
        direction=sort_direction,
        limit=limit,
    )


@_alias(
    "zotero_search_notes",
    "Search the text of every note in the library.",
    "zotero_search(item_type='note')",
)
def zotero_search_notes(
    query: str,
    limit: int | str | None = None,
    raw_html: bool = False,
) -> Any:
    return search_tools.zotero_search(query=query, mode="fulltext", item_type="note", limit=limit)


@_alias(
    "zotero_get_search_database_status",
    "Report on the semantic search index.",
    "zotero_index(action='status')",
)
def zotero_get_search_database_status() -> Any:
    return maintenance_tools.zotero_index(action="status")


@_alias(
    "zotero_update_search_database",
    "Index items added since the last update.",
    "zotero_index(action='update')",
)
def zotero_update_search_database(
    force_rebuild: bool = False, limit: int | str | None = None
) -> Any:
    if force_rebuild:
        raise InvalidInput(
            "A full rebuild is a command line operation.",
            hint="Run 'zotero-mcp index build' in a terminal.",
        )
    return maintenance_tools.zotero_index(action="update", limit=limit)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@_alias("zotero_get_item_metadata", "Full metadata for one item.", "zotero_get_item")
def zotero_get_item_metadata(
    item_key: str, include_abstract: bool = True, format: str = "markdown"
) -> Any:
    return items_tools.zotero_get_item(keys=item_key, include="children,collections")


@_alias("zotero_get_item_children", "An item's notes and attachments.", "zotero_get_item")
def zotero_get_item_children(item_key: str) -> Any:
    return items_tools.zotero_get_item(keys=item_key, include="children")


@_alias("zotero_get_items_children", "Several items' notes and attachments.", "zotero_get_item")
def zotero_get_items_children(item_keys: list[str] | str) -> Any:
    return items_tools.zotero_get_item(keys=item_keys, include="children")


@_alias("zotero_get_item_fulltext", "The text of an item's attachment.", "zotero_read")
def zotero_get_item_fulltext(item_key: str) -> Any:
    return items_tools.zotero_read(item_key=item_key)


@_alias(
    "zotero_get_pdf_outline",
    "A PDF's table of contents.",
    "zotero_read(what='outline')",
)
def zotero_get_pdf_outline(item_key: str) -> Any:
    return items_tools.zotero_read(item_key=item_key, what="outline")


@_alias("zotero_get_notes", "The notes attached to an item.", "zotero_get_annotations")
def zotero_get_notes(
    item_key: str,
    limit: int | str | None = None,
    truncate: bool = True,
    raw_html: bool = False,
) -> Any:
    return items_tools.zotero_get_annotations(item_key=item_key, kind="notes")


@_alias("zotero_get_recent", "Recently added items.", "zotero_library(action='recent')")
def zotero_get_recent(limit: int | str | None = None) -> Any:
    return library_tools.zotero_library(action="recent", limit=limit)


@_alias(
    "zotero_get_items_by_type",
    "Items of one Zotero item type.",
    "zotero_library(action='by_type')",
)
def zotero_get_items_by_type(item_type: str, limit: int | str | None = None) -> Any:
    return library_tools.zotero_library(action="by_type", item_type=item_type, limit=limit)


@_alias(
    "zotero_get_items_without_collection",
    "Items filed in no collection.",
    "zotero_library(action='uncollected')",
)
def zotero_get_items_without_collection(limit: int | str | None = None) -> Any:
    return library_tools.zotero_library(action="uncollected", limit=limit)


@_alias("zotero_get_library_stats", "Counts across the library.", "zotero_library(action='stats')")
def zotero_get_library_stats() -> Any:
    return library_tools.zotero_library(action="stats")


@_alias("zotero_list_libraries", "Libraries this key can read.", "zotero_library(action='list')")
def zotero_list_libraries() -> Any:
    return library_tools.zotero_library(action="list")


@_alias("zotero_switch_library", "Read a different library.", "zotero_library(action='switch')")
def zotero_switch_library(library_id: str, library_type: str = "group") -> Any:
    return library_tools.zotero_library(
        action="switch", library_id=library_id, library_type=library_type
    )


@_alias(
    "zotero_get_item_versions",
    "Current version numbers for the library's items.",
    "zotero_library(action='versions')",
)
def zotero_get_item_versions(item_keys: list[str] | str | None = None) -> Any:
    return library_tools.zotero_library(action="versions")


@_alias("zotero_list_feeds", "RSS subscriptions in this Zotero.", "zotero_library(action='feeds')")
def zotero_list_feeds() -> Any:
    return library_tools.zotero_library(action="feeds")


@_alias(
    "zotero_get_feed_items",
    "Items from one RSS feed.",
    "zotero_library(action='feed_items')",
)
def zotero_get_feed_items(library_id: int | str, limit: int | str | None = None) -> Any:
    return library_tools.zotero_library(action="feed_items", feed_id=library_id, limit=limit)


@_alias("zotero_get_trash", "Items currently in the trash.", "zotero_manage_items")
def zotero_get_trash(limit: int | str | None = None) -> Any:
    return write_tools.zotero_manage_items(action="list_trash", limit=limit)


@_alias("zotero_export_bibtex", "Export items as BibTeX.", "zotero_export")
def zotero_export_bibtex(
    collection_key: str | None = None, item_keys: list[str] | str | None = None
) -> Any:
    return maintenance_tools.zotero_export(
        item_keys=item_keys, collection_key=collection_key, format="bibtex"
    )


# ---------------------------------------------------------------------------
# Collections and tags
# ---------------------------------------------------------------------------


@_alias("zotero_get_collections", "Every collection, with its key.", "zotero_collections")
def zotero_get_collections(limit: int | str | None = None) -> Any:
    return organize_tools.zotero_collections(action="list", limit=limit)


@_alias("zotero_search_collections", "Find a collection by name.", "zotero_collections")
def zotero_search_collections(query: str) -> Any:
    return organize_tools.zotero_collections(action="search", name=query)


@_alias("zotero_get_collection_items", "The items in one collection.", "zotero_collections")
def zotero_get_collection_items(
    collection_key: str, detail: str = "summary", limit: int | str | None = None
) -> Any:
    return organize_tools.zotero_collections(
        action="items", collection_key=collection_key, limit=limit
    )


@_alias("zotero_create_collection", "Create a collection.", "zotero_collections")
def zotero_create_collection(name: str, parent_collection: str | None = None) -> Any:
    return organize_tools.zotero_collections(
        action="create", name=name, parent_key=parent_collection, dry_run=False
    )


@_alias("zotero_rename_collection", "Rename a collection.", "zotero_collections")
def zotero_rename_collection(collection_key: str, new_name: str) -> Any:
    return organize_tools.zotero_collections(
        action="rename", collection_key=collection_key, name=new_name, dry_run=False
    )


@_alias("zotero_delete_collection", "Delete a collection.", "zotero_collections")
def zotero_delete_collection(collection_key: str, confirm: bool = False) -> Any:
    return organize_tools.zotero_collections(
        action="delete", collection_key=collection_key, dry_run=not confirm
    )


@_alias(
    "zotero_manage_collections",
    "File items into collections, or take them out.",
    "zotero_collections",
)
def zotero_manage_collections(
    item_keys: list[str] | str,
    add_to: list[str] | str | None = None,
    remove_from: list[str] | str | None = None,
) -> Any:
    added = c.normalise_list(add_to)
    removed = c.normalise_list(remove_from)
    if not added and not removed:
        raise InvalidInput("Nothing to do: pass add_to or remove_from.")

    results = []
    for key in added:
        results.append(
            organize_tools.zotero_collections(
                action="add_items", collection_key=key, item_keys=item_keys, dry_run=False
            )
        )
    for key in removed:
        results.append(
            organize_tools.zotero_collections(
                action="remove_items", collection_key=key, item_keys=item_keys, dry_run=False
            )
        )
    return results[-1]


@_alias("zotero_get_tags", "The library's tags, with counts.", "zotero_tags")
def zotero_get_tags(limit: int | str | None = None) -> Any:
    return organize_tools.zotero_tags(action="list", limit=limit)


@_alias("zotero_batch_update_tags", "Add or remove tags across many items.", "zotero_tags")
def zotero_batch_update_tags(
    query: str | None = None,
    add_tags: list[str] | str | None = None,
    remove_tags: list[str] | str | None = None,
    tag: str | None = None,
    limit: int | str | None = None,
) -> Any:
    from zotero_mcp.backends.base import ItemQuery
    from zotero_mcp.tools._common import backend, page_size

    if not add_tags and not remove_tags:
        raise InvalidInput("Nothing to do: pass add_tags or remove_tags.")

    size = page_size(limit or 100)
    page = backend().get_items(
        ItemQuery(query=query or None, tags=tuple(c.normalise_list(tag)), limit=size)
    )
    keys = [item.get("key") for item in page.items if item.get("key")]
    if not keys:
        raise InvalidInput(
            "That query matched no items, so no tags were changed.",
            hint="Check the query with zotero_search first.",
        )

    if add_tags:
        result = organize_tools.zotero_tags(
            action="add", tags=add_tags, item_keys=keys, dry_run=False
        )
    if remove_tags:
        result = organize_tools.zotero_tags(
            action="remove", tags=remove_tags, item_keys=keys, dry_run=False
        )
    return result


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@_alias("zotero_add_by_doi", "Add an item to the library from a DOI.", "zotero_add_item")
def zotero_add_by_doi(
    doi: str,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
) -> Any:
    return write_tools.zotero_add_item(
        identifier=doi,
        collections=collections,
        tags=tags,
        attach_pdf=attach_mode != "none",
    )


@_alias("zotero_add_by_url", "Add an item to the library from a URL.", "zotero_add_item")
def zotero_add_by_url(
    url: str,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
) -> Any:
    return write_tools.zotero_add_item(
        identifier=url,
        collections=collections,
        tags=tags,
        attach_pdf=attach_mode != "none",
    )


@_alias("zotero_add_from_file", "Add an item from a local file.", "zotero_add_item")
def zotero_add_from_file(
    file_path: str,
    title: str | None = None,
    item_type: str = "document",
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
) -> Any:
    return write_tools.zotero_add_item(
        file_path=file_path,
        item_type=item_type,
        fields={"title": title} if title else None,
        collections=collections,
        tags=tags,
    )


@_alias("zotero_change_item_type", "Convert an item to another type.", "zotero_update_item")
def zotero_change_item_type(
    item_key: str,
    new_type: str,
    doi: str | None = None,
    publication_title: str | None = None,
    volume: str | None = None,
    issue: str | None = None,
    pages: str | None = None,
    date: str | None = None,
) -> Any:
    fields = {
        name: value
        for name, value in (
            ("DOI", doi),
            ("publicationTitle", publication_title),
            ("volume", volume),
            ("issue", issue),
            ("pages", pages),
            ("date", date),
        )
        if value is not None
    }
    return write_tools.zotero_update_item(
        item_key=item_key, change_item_type=new_type, fields=fields or None, dry_run=False
    )


@_alias("zotero_trash_items", "Move items to the trash.", "zotero_manage_items")
def zotero_trash_items(item_keys: list[str] | str) -> Any:
    return write_tools.zotero_manage_items(action="trash", item_keys=item_keys, dry_run=False)


@_alias("zotero_restore_from_trash", "Restore items from the trash.", "zotero_manage_items")
def zotero_restore_from_trash(item_keys: list[str] | str) -> Any:
    return write_tools.zotero_manage_items(action="restore", item_keys=item_keys, dry_run=False)


@_alias("zotero_delete_items", "Permanently delete items.", "zotero_manage_items")
def zotero_delete_items(item_keys: list[str] | str, confirm: bool = False) -> Any:
    return write_tools.zotero_manage_items(
        action="delete", item_keys=item_keys, dry_run=not confirm
    )


@_alias("zotero_empty_trash", "Permanently delete everything in the trash.", "zotero_manage_items")
def zotero_empty_trash(confirm: bool = False) -> Any:
    return write_tools.zotero_manage_items(action="empty_trash", dry_run=not confirm)


@_alias(
    "zotero_copy_items_to_library",
    "Copy items into another library.",
    "zotero_manage_items(action='copy')",
)
def zotero_copy_items_to_library(
    item_keys: list[str] | str,
    target_library_type: str = "group",
    target_library_id: str | None = None,
    target_collection_key: str | None = None,
) -> Any:
    return write_tools.zotero_manage_items(
        action="copy",
        item_keys=item_keys,
        target_library_id=target_library_id,
        target_library_type=target_library_type,
        target_collection_key=target_collection_key,
        dry_run=False,
    )


@_alias("zotero_find_duplicates", "Find probable duplicate items.", "zotero_duplicates")
def zotero_find_duplicates(
    method: str = "doi", collection_key: str | None = None, limit: int | str | None = None
) -> Any:
    return maintenance_tools.zotero_duplicates(
        action="find", collection_key=collection_key, scan_limit=limit
    )


@_alias("zotero_merge_duplicates", "Merge a group of duplicates.", "zotero_duplicates")
def zotero_merge_duplicates(
    keeper_key: str, duplicate_keys: list[str] | str, confirm: bool = False
) -> Any:
    return maintenance_tools.zotero_duplicates(
        action="merge",
        item_keys=[keeper_key, *c.normalise_list(duplicate_keys)],
        dry_run=not confirm,
    )


# ---------------------------------------------------------------------------
# Notes and annotations
# ---------------------------------------------------------------------------


@_alias("zotero_create_note", "Attach a note to an item.", "zotero_manage_note")
def zotero_create_note(
    item_key: str,
    note_title: str | None = None,
    note_text: str = "",
    tags: list[str] | str | None = None,
) -> Any:
    body = f"# {note_title}\n\n{note_text}" if note_title else note_text
    return notes_tools.zotero_manage_note(
        action="create", text=body, parent_item_key=item_key, tags=tags
    )


@_alias("zotero_update_note", "Change a note's text.", "zotero_manage_note")
def zotero_update_note(item_key: str, note_text: str, append: bool = False) -> Any:
    return notes_tools.zotero_manage_note(
        action="update", note_key=item_key, text=note_text, append=append
    )


@_alias("zotero_delete_note", "Delete a note.", "zotero_manage_note")
def zotero_delete_note(item_key: str) -> Any:
    return notes_tools.zotero_manage_note(action="delete", note_key=item_key)


@_alias(
    "zotero_create_annotation", "Add a highlight or comment to a PDF.", "zotero_manage_annotation"
)
def zotero_create_annotation(
    attachment_key: str,
    page: int,
    text: str | None = None,
    comment: str | None = None,
    color: str | None = None,
) -> Any:
    return notes_tools.zotero_manage_annotation(
        action="create",
        item_key=attachment_key,
        annotation_type="highlight" if text else "note",
        text=text,
        comment=comment,
        page=page,
        color=color,
    )


@_alias(
    "zotero_create_area_annotation",
    "Add an area selection to a PDF page.",
    "zotero_manage_annotation",
)
def zotero_create_area_annotation(
    attachment_key: str,
    page: int,
    x: float,
    y: float,
    width: float,
    height: float,
    comment: str | None = None,
    color: str | None = None,
) -> Any:
    return notes_tools.zotero_manage_annotation(
        action="create",
        item_key=attachment_key,
        annotation_type="image",
        comment=comment,
        page=page,
        rect=[x, y, x + width, y + height],
        color=color,
    )


# ---------------------------------------------------------------------------
# The two names that exist on both surfaces
# ---------------------------------------------------------------------------

_unregister("zotero_update_item")
_unregister("zotero_get_annotations")


@mcp.tool(
    name="zotero_update_item",
    description=(
        "Change an existing item: any metadata field, its tags, its collections, or "
        "its item type. Accepts either the current 'fields' argument or the "
        "individual pre-1.0 arguments."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    tags={"write", "core", "compat"},
)
@c.tool
def zotero_update_item(
    item_key: Annotated[str, Field(description="The item to change.")],
    fields: Annotated[dict[str, Any] | None, Field(description="Fields to set, by name.")] = None,
    title: Annotated[str | None, Field(description="Legacy: the item's title.")] = None,
    creators: Annotated[
        list[dict[str, Any]] | None, Field(description="Legacy: the full creator list.")
    ] = None,
    date: Annotated[str | None, Field(description="Legacy: the item's date.")] = None,
    publication_title: Annotated[
        str | None, Field(description="Legacy: the journal or book title.")
    ] = None,
    abstract: Annotated[str | None, Field(description="Legacy: the abstract.")] = None,
    doi: Annotated[str | None, Field(description="Legacy: the DOI.")] = None,
    url: Annotated[str | None, Field(description="Legacy: the URL.")] = None,
    extra: Annotated[str | None, Field(description="Legacy: the Extra field.")] = None,
    tags: Annotated[
        list[str] | str | None, Field(description="Legacy: replace the tag list entirely.")
    ] = None,
    add_tags: Annotated[list[str] | str | None, Field(description="Tags to add.")] = None,
    remove_tags: Annotated[list[str] | str | None, Field(description="Tags to remove.")] = None,
    collections: Annotated[
        list[str] | str | None, Field(description="Legacy: replace the collection list.")
    ] = None,
    collection_names: Annotated[
        list[str] | str | None, Field(description="Legacy: collections named rather than keyed.")
    ] = None,
    add_collections: Annotated[
        list[str] | str | None, Field(description="Collection keys to file it under.")
    ] = None,
    remove_collections: Annotated[
        list[str] | str | None, Field(description="Collection keys to remove it from.")
    ] = None,
    change_item_type: Annotated[
        str | None, Field(description="Convert to another item type.")
    ] = None,
    dry_run: Annotated[bool, Field(description="Show what would change.")] = True,
) -> Any:
    """Update an item, in either the current or the pre-1.0 argument shape."""
    merged = dict(fields or {})
    for name, value in (
        ("title", title),
        ("creators", creators),
        ("date", date),
        ("publicationTitle", publication_title),
        ("abstractNote", abstract),
        ("DOI", doi),
        ("url", url),
        ("extra", extra),
    ):
        if value is not None:
            merged[name] = value

    wanted_add = c.normalise_list(add_collections)
    wanted_remove = c.normalise_list(remove_collections)

    if collection_names:
        wanted_add.extend(_collection_keys_by_name(collection_names))
    if collections is not None:
        # The legacy argument replaced the list outright, so express that as
        # removing everything the item has and adding what was asked for.
        current = (c.require_item(c.resolve_item_key(item_key)).get("data") or {}).get(
            "collections"
        ) or []
        wanted_add.extend(c.normalise_list(collections))
        wanted_remove.extend(k for k in current if k not in wanted_add)

    wanted_add_tags = c.normalise_list(add_tags)
    wanted_remove_tags = c.normalise_list(remove_tags)
    if tags is not None:
        current_tags = [
            t.get("tag")
            for t in (c.require_item(c.resolve_item_key(item_key)).get("data") or {}).get("tags")
            or []
            if isinstance(t, dict)
        ]
        wanted_add_tags.extend(c.normalise_list(tags))
        wanted_remove_tags.extend(t for t in current_tags if t not in wanted_add_tags)

    return write_tools.zotero_update_item(
        item_key=item_key,
        fields=merged or None,
        add_tags=wanted_add_tags or None,
        remove_tags=wanted_remove_tags or None,
        add_collections=wanted_add or None,
        remove_collections=wanted_remove or None,
        change_item_type=change_item_type,
        dry_run=dry_run,
    )


def _collection_keys_by_name(names: list[str] | str) -> list[str]:
    """Resolve collection names to keys, failing loudly on an ambiguous one."""
    from zotero_mcp.mapping import collection_paths

    raw = c.backend().get_all_collections()
    paths = collection_paths(raw)
    keys: list[str] = []
    for name in c.normalise_list(names):
        matches = [
            collection.get("key")
            for collection in raw
            if (collection.get("data") or {}).get("name", "").lower() == name.lower()
        ]
        if not matches:
            raise InvalidInput(
                f"No collection named {name!r}.",
                hint="Use zotero_collections(action='search') to find its key.",
            )
        if len(matches) > 1:
            options = ", ".join(f"{paths.get(k, k)} (`{k}`)" for k in matches)
            raise InvalidInput(
                f"More than one collection is called {name!r}: {options}.",
                hint="Pass the key instead of the name.",
            )
        keys.append(matches[0])
    return keys


@mcp.tool(
    name="zotero_get_annotations",
    description=(
        "Highlights, comments and notes attached to an item. Annotations are grouped "
        "by page in reading order; every one carries its key so it can be edited."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"annotations", "core", "compat"},
)
@c.tool
def zotero_get_annotations(
    item_key: Annotated[str, Field(description="The item whose annotations to fetch.")],
    kind: Annotated[
        Literal["all", "annotations", "notes"],
        Field(description="Which to return."),
    ] = "all",
    query: Annotated[
        str | None, Field(description="Only annotations or notes containing this text.")
    ] = None,
    use_pdf_extraction: Annotated[
        bool | None,
        Field(description="Legacy and ignored: annotations are read from Zotero itself."),
    ] = None,
    limit: Annotated[
        int | str | None, Field(description="Legacy and ignored: all annotations are returned.")
    ] = None,
) -> Any:
    """Fetch annotations and notes, in either argument shape."""
    return items_tools.zotero_get_annotations(item_key=item_key, kind=kind, query=query)


#: Every legacy name this module registers, for the toolset table and tests.
LEGACY_TOOL_NAMES = frozenset(
    {
        "zotero_add_by_doi",
        "zotero_add_by_url",
        "zotero_add_from_file",
        "zotero_advanced_search",
        "zotero_batch_update_tags",
        "zotero_change_item_type",
        "zotero_copy_items_to_library",
        "zotero_create_annotation",
        "zotero_create_area_annotation",
        "zotero_create_collection",
        "zotero_create_note",
        "zotero_delete_collection",
        "zotero_delete_items",
        "zotero_delete_note",
        "zotero_empty_trash",
        "zotero_export_bibtex",
        "zotero_find_duplicates",
        "zotero_get_annotations",
        "zotero_get_collection_items",
        "zotero_get_collections",
        "zotero_get_feed_items",
        "zotero_get_item_children",
        "zotero_get_item_fulltext",
        "zotero_get_item_metadata",
        "zotero_get_item_versions",
        "zotero_get_items_by_type",
        "zotero_get_items_children",
        "zotero_get_items_without_collection",
        "zotero_get_library_stats",
        "zotero_get_notes",
        "zotero_get_pdf_outline",
        "zotero_get_recent",
        "zotero_get_search_database_status",
        "zotero_get_tags",
        "zotero_get_trash",
        "zotero_list_feeds",
        "zotero_list_libraries",
        "zotero_manage_collections",
        "zotero_merge_duplicates",
        "zotero_rename_collection",
        "zotero_restore_from_trash",
        "zotero_search_by_citation_key",
        "zotero_search_by_tag",
        "zotero_search_collections",
        "zotero_search_items",
        "zotero_search_notes",
        "zotero_semantic_search",
        "zotero_switch_library",
        "zotero_trash_items",
        "zotero_update_item",
        "zotero_update_note",
        "zotero_update_search_database",
    }
)

__all__ = ["LEGACY_TOOL_NAMES"]
