# Copyright 2026 Vinay

"""Collections and tags.

Two tools replace nine. The predecessor shipped ``zotero_get_collections``,
``zotero_get_collection_items``, ``zotero_search_collections``,
``zotero_create_collection``, ``zotero_rename_collection``,
``zotero_delete_collection``, ``zotero_manage_collections``,
``zotero_get_tags`` and ``zotero_batch_update_tags``, one of which was already
an attempt at consolidation that never replaced the others.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.mapping import collection_paths, to_collection_ref, to_tag_count
from zotero_mcp.models import Change, CollectionPage, WriteResult
from zotero_mcp.render import (
    render_collections,
    render_result_page,
    render_tags,
    render_write_result,
)
from zotero_mcp.tools import _common as c


def _all_collections_with_paths() -> tuple[list[dict[str, Any]], dict[str, str]]:
    raw = c.backend().get_all_collections()
    return raw, collection_paths(raw)


@mcp.tool(
    name="zotero_collections",
    description=(
        "Browse, search and manage collections: list the hierarchy, list a "
        "collection's items, create, rename, delete, or move items in and out. "
        "Every collection is returned with its key."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    tags={"organize", "core"},
)
@c.tool
def zotero_collections(
    action: Annotated[
        Literal[
            "list", "items", "search", "create", "rename", "delete", "add_items", "remove_items"
        ],
        Field(description="What to do. 'list' returns the whole hierarchy with paths."),
    ] = "list",
    collection_key: Annotated[str | None, Field(description="The collection to act on.")] = None,
    name: Annotated[
        str | None, Field(description="Name, for create and rename; search text for search.")
    ] = None,
    parent_key: Annotated[
        str | None, Field(description="Parent collection, when creating a subcollection.")
    ] = None,
    item_keys: Annotated[
        list[str] | str | None, Field(description="Items to add or remove.")
    ] = None,
    limit: Annotated[int | str | None, Field(description="Page size.")] = None,
    cursor: Annotated[str | None, Field(description="Continue a previous listing.")] = None,
    dry_run: Annotated[bool, Field(description="Preview a change without applying it.")] = True,
) -> Any:
    """Work with collections."""
    backend = c.backend()

    if action in {"list", "search"}:
        raw, paths = _all_collections_with_paths()
        refs = [to_collection_ref(item, path=paths.get(item.get("key") or "")) for item in raw]
        if action == "search":
            needle = (name or "").strip().lower()
            if not needle:
                raise InvalidInput("Searching collections needs a name to look for.")
            refs = [r for r in refs if needle in r.name.lower() or needle in (r.path or "").lower()]
        refs.sort(key=lambda r: (r.path or r.name).lower())

        size = c.page_size(limit)
        fingerprint = {"view": "collections", "search": name}
        offset = c.offset_from(cursor, fingerprint)
        window = refs[offset : offset + size]
        from zotero_mcp.paging import encode_cursor

        page = CollectionPage(
            collections=window,
            total=len(refs),
            offset=offset,
            returned=len(window),
            next_cursor=(
                encode_cursor(offset + len(window), fingerprint)
                if offset + len(window) < len(refs)
                else None
            ),
        )
        heading = f"Collections matching '{name}'" if action == "search" else "Collections"
        return c.respond(render_collections(page, heading=heading), page)

    if action == "items":
        if not collection_key:
            raise InvalidInput("collection_key is required to list a collection's items.")
        if backend.get_collection(collection_key) is None:
            raise NotFound(
                f"No collection with key {collection_key}.",
                hint="Use zotero_collections(action='search') to find it.",
            )
        size = c.page_size(limit)
        spec = ItemQuery(collection_key=collection_key, limit=size)
        fingerprint = spec.as_fingerprint()
        offset = c.offset_from(cursor, fingerprint)
        raw = backend.get_items(ItemQuery(**{**fingerprint, "offset": offset, "limit": size}))
        page = c.build_result_page(raw, fingerprint=fingerprint, size=size)
        return c.respond(render_result_page(page, heading="Collection items"), page)

    if action == "create":
        if not name:
            raise InvalidInput("A new collection needs a name.")
        if dry_run:
            where = f" under `{parent_key}`" if parent_key else " at the top level"
            result = WriteResult(
                action="create_collection", dry_run=True, message=f"Would create '{name}'{where}."
            )
            return c.respond(render_write_result(result), result)
        outcome = backend.create_collection(name, parent_key=parent_key)
        result = c.outcome_to_result(outcome, action="create_collection")
        return c.respond(render_write_result(result), result)

    if not collection_key:
        raise InvalidInput(f"collection_key is required to {action.replace('_', ' ')}.")
    collection = backend.get_collection(collection_key)
    if collection is None:
        raise NotFound(f"No collection with key {collection_key}.")
    version = collection.get("version") or (collection.get("data") or {}).get("version") or 0
    current_name = (collection.get("data") or {}).get("name", "")

    if action == "rename":
        if not name:
            raise InvalidInput("A rename needs a new name.")
        changes = [Change(field="name", before=current_name, after=name)]
        if dry_run:
            result = WriteResult(
                action="rename_collection",
                dry_run=True,
                succeeded=[collection_key],
                changes=changes,
            )
            return c.respond(render_write_result(result), result)
        outcome = backend.update_collection(collection_key, {"name": name}, version=version)
        result = c.outcome_to_result(outcome, action="rename_collection", changes=changes)
        return c.respond(render_write_result(result), result)

    if action == "delete":
        if dry_run:
            raw = backend.get_items(ItemQuery(collection_key=collection_key, limit=1))
            count = raw.total if raw.total is not None else len(raw.items)
            result = WriteResult(
                action="delete_collection",
                dry_run=True,
                succeeded=[collection_key],
                message=(
                    f"Would delete the collection '{current_name}'. Its {count} item(s) "
                    "stay in the library; only the collection itself is removed."
                ),
            )
            return c.respond(render_write_result(result), result)
        outcome = backend.delete_collection(collection_key, version=version)
        result = c.outcome_to_result(outcome, action="delete_collection")
        return c.respond(render_write_result(result), result)

    keys = [c.resolve_item_key(k) for k in c.normalise_list(item_keys)]
    if not keys:
        raise InvalidInput(f"No items given to {action.replace('_', ' ')}.")
    verb = "add_items_to_collection" if action == "add_items" else "remove_items_from_collection"
    if dry_run:
        result = WriteResult(
            action=verb,
            dry_run=True,
            succeeded=keys,
            message=f"Collection '{current_name}' (`{collection_key}`).",
        )
        return c.respond(render_write_result(result), result)

    outcome = (
        backend.add_to_collection(collection_key, keys)
        if action == "add_items"
        else backend.remove_from_collection(collection_key, keys)
    )
    result = c.outcome_to_result(outcome, action=verb)
    return c.respond(render_write_result(result), result)


@mcp.tool(
    name="zotero_tags",
    description=(
        "List or search the library's tags, and add or remove tags across many items "
        "at once. Batch changes preview by default."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    tags={"organize", "core"},
)
@c.tool
def zotero_tags(
    action: Annotated[
        Literal["list", "add", "remove", "rename"],
        Field(description="What to do."),
    ] = "list",
    filter_text: Annotated[
        str | None, Field(description="Only tags containing this text, when listing.")
    ] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to add or remove.")] = None,
    new_name: Annotated[str | None, Field(description="Replacement name, for rename.")] = None,
    item_keys: Annotated[
        list[str] | str | None,
        Field(description="Items to change. Omit on rename to change every item carrying the tag."),
    ] = None,
    limit: Annotated[int | str | None, Field(description="Page size for listing.")] = None,
    cursor: Annotated[str | None, Field(description="Continue a previous listing.")] = None,
    dry_run: Annotated[bool, Field(description="Preview without applying.")] = True,
) -> Any:
    """Work with tags."""
    backend = c.backend()

    if action == "list":
        size = c.page_size(limit)
        fingerprint = {"view": "tags", "filter": filter_text}
        offset = c.offset_from(cursor, fingerprint)
        raw = backend.get_tags(filter_text=filter_text, offset=offset, limit=size)
        counts = [to_tag_count(item) for item in raw.items]
        heading = f"Tags matching '{filter_text}'" if filter_text else "Tags"
        payload = {
            "tags": [t.model_dump(mode="json", exclude_none=True) for t in counts],
            "total": raw.total,
            "offset": offset,
            "returned": len(counts),
        }
        return c.respond(render_tags(counts, heading=heading), payload)

    tag_list = c.normalise_list(tags)
    if not tag_list:
        raise InvalidInput(f"No tags given to {action}.")

    if action == "rename":
        if not new_name:
            raise InvalidInput("A rename needs new_name.")
        if len(tag_list) != 1:
            raise InvalidInput("Rename one tag at a time.", hint=f"Got {len(tag_list)}.")
        old = tag_list[0]
        keys = [c.resolve_item_key(k) for k in c.normalise_list(item_keys)]
        if not keys:
            page = backend.get_items(ItemQuery(tags=(old,), item_type=None, limit=100))
            keys = [item.get("key") for item in page.items if item.get("key")]
        return _apply_tag_change(
            keys, add=[new_name], remove=[old], action="rename_tag", dry_run=dry_run
        )

    keys = [c.resolve_item_key(k) for k in c.normalise_list(item_keys)]
    if not keys:
        raise InvalidInput(f"No items given to {action} tags on.")
    return _apply_tag_change(
        keys,
        add=tag_list if action == "add" else [],
        remove=tag_list if action == "remove" else [],
        action=f"{action}_tags",
        dry_run=dry_run,
    )


def _apply_tag_change(
    keys: list[str], *, add: list[str], remove: list[str], action: str, dry_run: bool
) -> Any:
    """Apply a tag change item by item, so one rejection does not lose the rest."""
    c.guard_destructive(dry_run, action=action, affected=len(keys))

    changes: list[Change] = []
    to_write: list[tuple[str, list[str], int]] = []

    for key in keys:
        item = c.require_item(key)
        data = item.get("data") or {}
        current = [t.get("tag") for t in data.get("tags") or [] if isinstance(t, dict)]
        wanted = list(current)
        for tag in add:
            if tag not in wanted:
                wanted.append(tag)
        for tag in remove:
            if tag in wanted:
                wanted.remove(tag)
        if wanted != current:
            to_write.append((key, wanted, item.get("version", 0)))
            if len(changes) < 25:
                changes.append(
                    Change(field=key, before=", ".join(current), after=", ".join(wanted))
                )

    if not to_write:
        result = WriteResult(
            action=action, unchanged=keys, message="Every item already has these tags."
        )
        return c.respond(render_write_result(result), result)

    if dry_run:
        result = WriteResult(
            action=action, dry_run=True, succeeded=[key for key, _, _ in to_write], changes=changes
        )
        return c.respond(render_write_result(result), result)

    succeeded: list[str] = []
    failed: dict[str, str] = {}
    for key, wanted, version in to_write:
        try:
            c.backend().update_item(
                key, {"tags": [{"tag": tag} for tag in wanted]}, version=version
            )
            succeeded.append(key)
        except Exception as exc:  # noqa: BLE001 (one rejection must not lose the batch)
            failed[key] = str(exc)

    result = WriteResult(action=action, succeeded=succeeded, failed=failed, changes=changes)
    return c.respond(render_write_result(result), result)


__all__ = ["zotero_collections", "zotero_tags"]
