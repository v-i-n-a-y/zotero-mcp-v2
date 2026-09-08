# Copyright 2026 Vinay

"""Adding, changing and removing items.

Two rules run through every tool here.

**Preview by default.** Destructive operations take ``dry_run`` and it starts
at True. The result says which it was, so a preview can never be mistaken for
a completed write. That ambiguity is the one that costs a user real data.

**Send the version.** Every update carries the version it read, which Zotero
turns into ``If-Unmodified-Since-Version``. A concurrent edit in the desktop
client then produces a conflict the caller can act on, instead of silently
overwriting whatever the user just typed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp import schema
from zotero_mcp.app import mcp
from zotero_mcp.errors import InvalidInput, NotFound, Unsupported
from zotero_mcp.identifiers import IdentifierKind, parse_identifier
from zotero_mcp.mapping import citation_key_of
from zotero_mcp.models import Change, WriteResult
from zotero_mcp.render import render_write_result
from zotero_mcp.tools import _common as c

logger = logging.getLogger(__name__)

#: Fields a caller may set directly. Everything else Zotero manages.
_MANAGED_FIELDS = frozenset({"key", "version", "dateAdded", "dateModified", "itemType"})


def _existing_by_identifier(kind: IdentifierKind, value: str) -> str | None:
    """An item already in the library carrying this identifier.

    Checked before every import. Re-adding a paper the user already has is the
    most common way an assistant silently creates duplicates.
    """
    from zotero_mcp.backends.base import ItemQuery

    page = c.backend().get_items(
        ItemQuery(query=value, qmode="everything", limit=10, item_type=None)
    )
    for item in page.items:
        data = item.get("data") or {}
        if kind is IdentifierKind.DOI and (data.get("DOI") or "").lower() == value.lower():
            return item.get("key")
        if kind is IdentifierKind.ISBN and value in (data.get("ISBN") or ""):
            return item.get("key")
        if kind is IdentifierKind.ARXIV and value in (
            f"{data.get('archiveID', '')} {data.get('url', '')} {data.get('extra', '')}"
        ):
            return item.get("key")
    return None


@mcp.tool(
    name="zotero_add_item",
    description=(
        "Add an item to the library from a DOI, arXiv id, ISBN, PMID, URL or local "
        "file, fetching metadata automatically. Checks for an existing copy first. "
        "Optionally attaches a legally free open-access PDF."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    tags={"write", "core"},
)
@c.tool
def zotero_add_item(
    identifier: Annotated[
        str | None,
        Field(
            description=(
                "A DOI, arXiv id, ISBN, PMID, PMCID or URL. The kind is detected, so "
                "there is no need to say which."
            )
        ),
    ] = None,
    file_path: Annotated[
        str | None,
        Field(description="Absolute path to a local file to import as an attachment."),
    ] = None,
    fields: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Item fields, for creating an item by hand or overriding fetched "
                "metadata. Generic names like 'title' are routed to the item type's "
                "real field, so a case gets caseName."
            )
        ),
    ] = None,
    item_type: Annotated[
        str | None,
        Field(description="Zotero item type. Required when creating by hand."),
    ] = None,
    collections: Annotated[
        list[str] | str | None,
        Field(description="Collection keys to file the new item under."),
    ] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply.")] = None,
    attach_pdf: Annotated[
        bool,
        Field(
            description=(
                "Look for an open-access PDF (Unpaywall, arXiv, PMC, Semantic Scholar) "
                "and attach it. Never touches paywalled content."
            )
        ),
    ] = True,
    dry_run: Annotated[
        bool,
        Field(description="Preview the item that would be created without creating it."),
    ] = False,
) -> Any:
    """Add an item."""
    if not identifier and not file_path and not fields:
        raise InvalidInput(
            "Nothing to add.",
            hint="Pass an identifier (DOI, arXiv id, URL), a file_path, or fields.",
        )

    settings = c.runtime().config.network
    data: dict[str, Any] = {}
    source = "supplied fields"
    arxiv_id: str | None = None

    if identifier:
        from zotero_mcp.external import metadata

        parsed = parse_identifier(identifier)
        if parsed.kind is IdentifierKind.ZOTERO_KEY:
            raise InvalidInput(
                f"{identifier} is already a Zotero item key.",
                hint="It is in the library; use zotero_get_item to look at it.",
            )

        existing = _existing_by_identifier(parsed.kind, parsed.value)
        if existing:
            return c.respond(
                render_write_result(
                    WriteResult(
                        action="add_item",
                        dry_run=True,
                        unchanged=[existing],
                        created_key=existing,
                        message=(
                            f"This library already has {parsed.kind.value} "
                            f"`{parsed.value}` as item `{existing}`. Nothing was added."
                        ),
                    )
                ),
                WriteResult(
                    action="add_item", dry_run=True, unchanged=[existing], created_key=existing
                ),
            )

        data = metadata.fetch(parsed, settings)
        source = parsed.kind.value
        if parsed.kind is IdentifierKind.ARXIV:
            arxiv_id = parsed.value

    if fields:
        merged_type = item_type or fields.get("itemType") or data.get("itemType") or "document"
        if not schema.is_item_type(merged_type):
            raise InvalidInput(
                f"{merged_type!r} is not a Zotero item type.",
                hint=f"Try one of: {', '.join(schema.creatable_item_types()[:12])}...",
            )
        overrides, unplaceable = schema.resolve_fields(
            merged_type, {k: v for k, v in fields.items() if k not in {"itemType"}}
        )
        if unplaceable:
            raise InvalidInput(
                f"A {merged_type} has no field for: {', '.join(unplaceable)}.",
                hint=_field_hint(merged_type, unplaceable[0]),
            )
        data = {**data, **overrides, "itemType": merged_type}

    if item_type and not fields:
        data["itemType"] = item_type
    data.setdefault("itemType", "document")

    if tag_list := c.normalise_list(tags):
        data["tags"] = [{"tag": tag} for tag in tag_list]
    if collection_keys := c.normalise_list(collections):
        data["collections"] = collection_keys

    if dry_run:
        preview = WriteResult(
            action="add_item",
            dry_run=True,
            changes=[Change(field=k, after=_short(v)) for k, v in sorted(data.items())],
            message=f"Metadata resolved from {source}.",
        )
        return c.respond(render_write_result(preview), preview)

    outcome = c.backend().create_items([data])
    if outcome.failed:
        raise InvalidInput(
            f"Zotero rejected the new item: {'; '.join(outcome.failed.values())}",
            details={"item_type": data.get("itemType")},
        )
    key = next(iter(outcome.succeeded), None)
    if not key:
        raise InvalidInput("Zotero accepted the request but returned no item key.")

    notes: list[str] = [f"Metadata resolved from {source}."]

    if file_path:
        notes.append(_attach_local_file(key, file_path))
    elif attach_pdf:
        notes.append(_attach_open_access(key, data, arxiv_id, settings))

    result = WriteResult(
        action="add_item",
        succeeded=[key],
        created_key=key,
        changes=[Change(field=k, after=_short(v)) for k, v in sorted(data.items())],
        message=" ".join(notes),
    )
    return c.respond(render_write_result(result), result)


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _field_hint(item_type: str, field: str) -> str:
    suggestions = schema.suggest_field(item_type, field)
    if suggestions:
        return f"Did you mean {', '.join(suggestions)}?"
    return f"Valid fields for {item_type}: {', '.join(sorted(schema.valid_fields(item_type))[:15])}"


def _attach_local_file(item_key: str, file_path: str) -> str:
    """Attach a file from disk, refusing anything that is not plainly a file."""
    path = Path(file_path)
    if not path.is_absolute():
        raise InvalidInput("file_path must be absolute.", hint=f"Got {file_path!r}.")
    if path.is_symlink():
        # A symlink can point anywhere, including outside anything the user
        # meant to share, and it is never what an attachment should be.
        raise InvalidInput("Refusing to attach a symlink.", hint=str(path))
    resolved = path.resolve()
    if not resolved.is_file():
        raise NotFound(f"No file at {resolved}.")

    try:
        outcome = c.backend().attach_file(item_key, resolved, title=resolved.name)
    except Unsupported as exc:
        return f"Could not attach the file: {exc.message}"
    if outcome.failed:
        return f"Could not attach {resolved.name}: {'; '.join(outcome.failed.values())}"
    return f"Attached {resolved.name}."


def _attach_open_access(
    item_key: str, data: dict[str, Any], arxiv_id: str | None, settings: Any
) -> str:
    """Find and attach an open-access PDF, reporting honestly when there is none."""
    from zotero_mcp.external import http, openaccess

    doi = data.get("DOI")
    if not doi and not arxiv_id:
        return "No DOI or arXiv id, so no open-access lookup was attempted."
    if not settings.allow_external_services:
        # find_pdf swallows a failing source, so without this check a disabled
        # lookup would be reported as "nothing found", which is not the same
        # thing and sends the user looking for a PDF that was never sought.
        return (
            "Open-access lookups are disabled, so none was attempted. "
            "Set ZOTERO_MCP_ALLOW_EXTERNAL=1 to enable them."
        )

    try:
        found = openaccess.find_pdf(doi=doi, arxiv_id=arxiv_id, settings=settings)
    except Unsupported as exc:
        return exc.message
    if not found:
        return "No open-access PDF was found for this item."

    url, source = found
    payload = http.download(url, settings=settings)
    if not payload or not payload.startswith(b"%PDF"):
        # A landing page served as HTML is the usual failure, and attaching it
        # as a .pdf would produce an unreadable attachment.
        return f"{source} offered a PDF but it could not be downloaded; the link is in the item."

    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="zotero_mcp_oa_"))
    target = directory / f"{item_key}.pdf"
    target.write_bytes(payload)
    try:
        outcome = c.backend().attach_file(item_key, target, title=f"Full Text PDF ({source})")
        if outcome.failed:
            return f"Found a PDF via {source} but Zotero rejected the upload."
        return f"Attached an open-access PDF from {source}."
    finally:
        from zotero_mcp.content.reader import cleanup_download

        cleanup_download(target)


@mcp.tool(
    name="zotero_update_item",
    description=(
        "Change an existing item: any metadata field, its tags, its collections, or "
        "its item type. Previews the exact before/after by default."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    tags={"write", "core"},
)
@c.tool
def zotero_update_item(
    item_key: Annotated[str, Field(description="The item to change.")],
    fields: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Fields to set. Generic names are routed to the item type's real "
                "field. Pass an empty string to clear a field."
            )
        ),
    ] = None,
    add_tags: Annotated[list[str] | str | None, Field(description="Tags to add.")] = None,
    remove_tags: Annotated[list[str] | str | None, Field(description="Tags to remove.")] = None,
    add_collections: Annotated[
        list[str] | str | None, Field(description="Collection keys to file it under.")
    ] = None,
    remove_collections: Annotated[
        list[str] | str | None, Field(description="Collection keys to remove it from.")
    ] = None,
    change_item_type: Annotated[
        str | None,
        Field(
            description=(
                "Convert to another item type. Fields the new type cannot hold are "
                "reported rather than silently dropped."
            )
        ),
    ] = None,
    dry_run: Annotated[
        bool, Field(description="Show what would change without applying it.")
    ] = True,
) -> Any:
    """Update an item."""
    key = c.resolve_item_key(item_key)
    item = c.require_item(key)
    data = dict(item.get("data") or {})
    version = item.get("version") or data.get("version") or 0
    item_type = change_item_type or data.get("itemType", "document")

    if change_item_type and not schema.is_item_type(change_item_type):
        raise InvalidInput(
            f"{change_item_type!r} is not a Zotero item type.",
            hint=f"Try one of: {', '.join(schema.creatable_item_types()[:12])}...",
        )

    changes: list[Change] = []
    patch: dict[str, Any] = {}

    if change_item_type and change_item_type != data.get("itemType"):
        patch["itemType"] = change_item_type
        changes.append(
            Change(field="itemType", before=data.get("itemType"), after=change_item_type)
        )
        # Carry across only what the new type can actually hold, and say what
        # is being left behind rather than dropping it silently.
        lost = [
            name
            for name in data
            if name not in _MANAGED_FIELDS
            and name not in {"tags", "collections", "relations"}
            and data.get(name)
            and schema.resolve_field(change_item_type, schema.base_field(data["itemType"], name))
            is None
        ]
        for name in lost:
            changes.append(Change(field=f"{name} (dropped)", before=data[name], after=None))

    if fields:
        resolved, unplaceable = schema.resolve_fields(
            item_type, {k: v for k, v in fields.items() if k not in _MANAGED_FIELDS}
        )
        if unplaceable:
            raise InvalidInput(
                f"A {item_type} has no field for: {', '.join(unplaceable)}.",
                hint=_field_hint(item_type, unplaceable[0]),
            )
        for name, value in resolved.items():
            before = data.get(name)
            if str(before or "") != str(value or ""):
                patch[name] = value
                changes.append(Change(field=name, before=before, after=value))

    current_tags = [t.get("tag") for t in data.get("tags") or [] if isinstance(t, dict)]
    wanted_tags = list(current_tags)
    for tag in c.normalise_list(add_tags):
        if tag not in wanted_tags:
            wanted_tags.append(tag)
    for tag in c.normalise_list(remove_tags):
        if tag in wanted_tags:
            wanted_tags.remove(tag)
    if wanted_tags != current_tags:
        patch["tags"] = [{"tag": tag} for tag in wanted_tags]
        changes.append(
            Change(field="tags", before=", ".join(current_tags), after=", ".join(wanted_tags))
        )

    current_collections = list(data.get("collections") or [])
    wanted_collections = list(current_collections)
    for collection in c.normalise_list(add_collections):
        if collection not in wanted_collections:
            wanted_collections.append(collection)
    for collection in c.normalise_list(remove_collections):
        if collection in wanted_collections:
            wanted_collections.remove(collection)
    if wanted_collections != current_collections:
        patch["collections"] = wanted_collections
        changes.append(
            Change(
                field="collections",
                before=", ".join(current_collections),
                after=", ".join(wanted_collections),
            )
        )

    if not patch:
        result = WriteResult(
            action="update_item",
            unchanged=[key],
            version=version,
            message="Nothing to change: the item already has these values.",
        )
        return c.respond(render_write_result(result), result)

    if dry_run:
        result = WriteResult(
            action="update_item", dry_run=True, succeeded=[key], changes=changes, version=version
        )
        return c.respond(render_write_result(result), result)

    outcome = c.backend().update_item(key, patch, version=version)
    result = c.outcome_to_result(outcome, action="update_item", changes=changes)
    return c.respond(render_write_result(result), result)


@mcp.tool(
    name="zotero_manage_items",
    description=(
        "Move items to the trash, restore them, delete them permanently, or empty the "
        "trash. Previews by default; pass dry_run=False to apply."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    tags={"write", "core"},
)
@c.tool
def zotero_manage_items(
    action: Annotated[
        Literal["trash", "restore", "delete", "empty_trash", "list_trash", "copy"],
        Field(
            description=(
                "'trash' is reversible and is almost always what you want. 'delete' "
                "is permanent and cannot be undone."
            )
        ),
    ],
    item_keys: Annotated[
        list[str] | str | None,
        Field(description="Items to act on. Not needed for empty_trash or list_trash."),
    ] = None,
    dry_run: Annotated[bool, Field(description="Preview without changing anything.")] = True,
    limit: Annotated[int | str | None, Field(description="Page size for list_trash.")] = None,
    target_library_id: Annotated[
        str | None, Field(description="For action='copy': the library to copy into.")
    ] = None,
    target_library_type: Annotated[
        Literal["user", "group"] | None,
        Field(description="For action='copy': whether the target is a group library."),
    ] = None,
    target_collection_key: Annotated[
        str | None, Field(description="For action='copy': file the copies under this collection.")
    ] = None,
) -> Any:
    """Trash, restore, delete or copy items."""
    if action == "list_trash":
        from zotero_mcp.backends.base import RawPage
        from zotero_mcp.render import render_result_page

        size = c.page_size(limit)
        raw: RawPage = c.backend().get_trash(offset=0, limit=size)
        page = c.build_result_page(raw, fingerprint={"view": "trash"}, size=size)
        return c.respond(render_result_page(page, heading="Trash"), page)

    if action == "empty_trash":
        if dry_run:
            raw = c.backend().get_trash(offset=0, limit=1)
            count = raw.total if raw.total is not None else len(raw.items)
            result = WriteResult(
                action="empty_trash",
                dry_run=True,
                message=(
                    f"Would permanently delete {count} item(s) in the trash. This cannot be undone."
                ),
            )
            return c.respond(render_write_result(result), result)
        outcome = c.backend().empty_trash()
        result = c.outcome_to_result(outcome, action="empty_trash")
        return c.respond(render_write_result(result), result)

    keys = [c.resolve_item_key(k) for k in c.normalise_list(item_keys)]
    if not keys:
        raise InvalidInput(f"No items given to {action}.")

    if action == "copy":
        return _copy_items(
            keys, target_library_id, target_library_type, target_collection_key, dry_run
        )

    c.guard_destructive(dry_run, action=action, affected=len(keys))
    versions = c.item_versions(keys)

    if dry_run:
        summaries = [c.summarise(c.require_item(key)) for key in keys[:25]]
        lines = [f"- `{s.key}` {s.title} ({s.year or 'n.d.'})" for s in summaries]
        warning = "\n\n**This is permanent and cannot be undone.**" if action == "delete" else ""
        result = WriteResult(
            action=action,
            dry_run=True,
            succeeded=keys,
            message="\n".join(lines) + warning,
        )
        return c.respond(render_write_result(result), result)

    backend = c.backend()
    if action == "trash":
        outcome = backend.trash_items(versions)
    elif action == "restore":
        outcome = backend.restore_items(versions)
    else:
        outcome = backend.delete_items(versions)

    result = c.outcome_to_result(outcome, action=action)
    return c.respond(render_write_result(result), result)


def _copy_items(
    keys: list[str],
    library_id: str | None,
    library_type: str | None,
    collection_key: str | None,
    dry_run: bool,
) -> Any:
    """Copy items into another library, as new items.

    Zotero has no cross-library move: the copies are genuinely new items with
    their own keys, and the originals are left alone. Child notes and
    attachments are not carried across, and that is said plainly rather than
    left for the user to discover.
    """
    if not library_id:
        raise InvalidInput(
            "action='copy' needs a target_library_id.",
            hint="Use zotero_library(action='list') to see the options.",
        )

    from dataclasses import replace as dataclass_replace

    from zotero_mcp.backends.factory import build_backend
    from zotero_mcp.config import LibraryType

    active = c.runtime()
    # The backend knows which library is actually open; the configuration may
    # never have named one, as with the local API.
    if str(library_id) == str(active.backend.library_ref().library_id):
        raise InvalidInput(
            "The target library is the one already open.",
            hint="Use zotero_collections(action='add_items') to file items instead.",
        )

    known = {library.library_id: library for library in active.backend.list_libraries()}
    chosen = known.get(str(library_id))
    resolved_type = library_type or (chosen.library_type if chosen else "group")

    payloads: list[dict[str, Any]] = []
    titles: list[str] = []
    for key in keys:
        data = dict(c.require_item(key).get("data") or {})
        titles.append(data.get("title") or key)
        for managed in ("key", "version", "dateAdded", "dateModified", "relations"):
            data.pop(managed, None)
        data["collections"] = [collection_key] if collection_key else []
        payloads.append(data)

    if dry_run:
        target_name = chosen.name if chosen else library_id
        result = WriteResult(
            action="copy_items",
            dry_run=True,
            succeeded=keys,
            message=(
                f"Would copy {len(payloads)} item(s) into **{target_name}** "
                f"(`{library_id}`, {resolved_type}): {', '.join(titles[:10])}. "
                "The copies are new items with new keys; the originals are untouched. "
                "Child notes and attachments are not carried across."
            ),
        )
        return c.respond(render_write_result(result), result)

    target_library = dataclass_replace(
        active.config.library,
        library_id=str(library_id),
        library_type=LibraryType(resolved_type),
    )
    target = build_backend(dataclass_replace(active.config, library=target_library))
    if not target.ping():
        raise NotFound(
            f"Library {library_id} could not be reached with the current credentials.",
            hint="Check the id, and that your API key has write access to that group.",
        )

    outcome = target.create_items(payloads)
    result = c.outcome_to_result(
        outcome,
        action="copy_items",
        message=(
            f"Copied {len(outcome.succeeded)} item(s) into `{library_id}`. "
            "Child notes and attachments were not carried across."
        ),
    )
    return c.respond(render_write_result(result), result)


__all__ = ["citation_key_of", "zotero_add_item", "zotero_manage_items", "zotero_update_item"]
