# Copyright 2026 Vinay

"""Reading items: metadata, content and annotations.

Three tools where the predecessors had nine. The consolidation is not
cosmetic. ``zotero_get_item_metadata``, ``zotero_get_item_children``,
``zotero_get_items_children``, ``zotero_get_item_versions`` and
``zotero_get_item_fulltext`` are all "tell me about this item", differing only
in which part, which is a parameter rather than five tool names competing for
the model's attention.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.content.reader import read_content, read_outline
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.mapping import (
    collection_paths,
    to_annotation,
    to_collection_ref,
    to_item_detail,
    to_note,
)
from zotero_mcp.models import Annotation, ItemDetail, Note
from zotero_mcp.render import (
    render_annotations,
    render_content_chunk,
    render_item_detail,
    render_notes,
    render_outline,
)
from zotero_mcp.tools import _common as c

#: Fetching an item's collections costs a full collection listing, so it is
#: only done when a reasonable number of items were asked for.
_COLLECTION_LOOKUP_LIMIT = 5


def _collections_for(item: dict[str, Any], paths: dict[str, str], lookup: dict[str, dict]):
    refs = []
    for key in (item.get("data") or {}).get("collections") or []:
        raw = lookup.get(key)
        if raw is not None:
            refs.append(to_collection_ref(raw, path=paths.get(key)))
    return refs


@mcp.tool(
    name="zotero_get_item",
    description=(
        "Full metadata for one or more Zotero items, by key. Includes creators, "
        "identifiers, tags, collections and a list of attachments. Does not return "
        "document text: use zotero_read for that."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"items", "core"},
)
@c.tool
def zotero_get_item(
    keys: Annotated[
        list[str] | str,
        Field(
            description=(
                "One or more item keys. Accepts a list, a comma-separated string, "
                "a zotero:// link, or a DOI already in the library."
            )
        ),
    ],
    include: Annotated[
        list[str] | str | None,
        Field(
            description=(
                "Extra detail to fetch: 'children' (attachments and note counts), "
                "'collections' (resolved names). Both cost additional requests."
            )
        ),
    ] = "children",
) -> Any:
    """Fetch item metadata."""
    wanted = c.normalise_list(keys)
    if not wanted:
        raise InvalidInput("No item keys were given.")
    if len(wanted) > 25:
        raise InvalidInput(
            f"{len(wanted)} items is too many for one call.",
            hint="Ask for at most 25; use zotero_search to page through more.",
        )

    extras = {part.lower() for part in c.normalise_list(include)}
    resolved = [c.resolve_item_key(key) for key in wanted]

    paths: dict[str, str] = {}
    lookup: dict[str, dict] = {}
    if "collections" in extras and len(resolved) <= _COLLECTION_LOOKUP_LIMIT:
        raw_collections = c.backend().get_all_collections()
        paths = collection_paths(raw_collections)
        lookup = {
            (col.get("key") or (col.get("data") or {}).get("key")): col for col in raw_collections
        }

    library = c.backend().library_ref()
    details: list[ItemDetail] = []
    for key in resolved:
        item = c.require_item(key)
        children = c.backend().get_children(key) if "children" in extras else None
        details.append(
            to_item_detail(
                item,
                library=library,
                children=children,
                collections=_collections_for(item, paths, lookup) if lookup else None,
            )
        )

    markdown = "\n\n---\n\n".join(render_item_detail(detail) for detail in details)
    if len(details) == 1:
        return c.respond(markdown, details[0])
    return c.respond(
        markdown, {"items": [d.model_dump(mode="json", exclude_none=True) for d in details]}
    )


@mcp.tool(
    name="zotero_read",
    description=(
        "Read the text of an item's attachment, by page range. Returns the opening "
        "pages by default and tells you the range to pass next, so a long paper is "
        "read in slices rather than all at once."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"content", "core"},
)
@c.tool
def zotero_read(
    item_key: Annotated[str, Field(description="The item (or attachment) to read.")],
    pages: Annotated[
        str | int | None,
        Field(
            description=(
                "1-based page range: '1-5', '3', or '1-5,12'. Omit for the opening "
                "pages. Only meaningful for PDFs."
            )
        ),
    ] = None,
    attachment_key: Annotated[
        str | None,
        Field(description="Read a specific attachment. Defaults to the best available PDF."),
    ] = None,
    what: Annotated[
        Literal["text", "outline"],
        Field(
            description=(
                "'text' reads page content. 'outline' returns the document's table of "
                "contents, which is the cheap way to find the section worth reading."
            )
        ),
    ] = "text",
) -> Any:
    """Read attachment content."""
    key = c.resolve_item_key(item_key)

    if what == "outline":
        entries, title = read_outline(c.backend(), key)
        return c.respond(
            render_outline(entries, heading=f"Outline: {title}"),
            {"item_key": key, "entries": [e.model_dump(mode="json") for e in entries]},
        )

    chunk = read_content(
        c.backend(),
        key,
        limits=c.limits(),
        pages=pages,
        attachment_key=attachment_key,
    )
    return c.respond(render_content_chunk(chunk), chunk, limit=c.limits().max_content_chars + 2_000)


def _collect_annotations(item_key: str) -> list[Annotation]:
    """Annotations belonging to an item, descending through its attachments.

    Zotero stores annotations as children of the *attachment*, not of the item,
    so asking for an item's children returns none of them. Both predecessors
    shipped that bug and fixed it more than once.
    """
    backend = c.backend()
    found: list[Annotation] = []

    direct = backend.get_children(item_key, item_type="annotation")
    found.extend(to_annotation(raw) for raw in direct)

    for attachment in backend.get_children(item_key, item_type="attachment"):
        attachment_key = attachment.get("key") or (attachment.get("data") or {}).get("key")
        if not attachment_key:
            continue
        for raw in backend.get_children(attachment_key, item_type="annotation"):
            found.append(to_annotation(raw))

    # Zotero's sortIndex orders annotations as they appear in the document,
    # which is the order a reader expects; page number alone is not enough.
    return sorted(found, key=lambda a: (a.page_index or 0, a.sort_index or ""))


@mcp.tool(
    name="zotero_get_annotations",
    description=(
        "Highlights, comments and notes attached to an item. Annotations are grouped "
        "by page in reading order; every one carries its key so it can be edited."
    ),
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"annotations", "core"},
)
@c.tool
def zotero_get_annotations(
    item_key: Annotated[str, Field(description="The item whose annotations to fetch.")],
    kind: Annotated[
        Literal["all", "annotations", "notes"],
        Field(description="Which to return. Annotations live on attachments; notes on the item."),
    ] = "all",
    query: Annotated[
        str | None,
        Field(description="Only return annotations or notes containing this text."),
    ] = None,
) -> Any:
    """Fetch annotations and notes."""
    key = c.resolve_item_key(item_key)
    needle = (query or "").strip().lower()

    annotations: list[Annotation] = []
    notes: list[Note] = []

    if kind in {"all", "annotations"}:
        annotations = _collect_annotations(key)
        if needle:
            annotations = [
                a
                for a in annotations
                if needle in (a.text or "").lower() or needle in (a.comment or "").lower()
            ]

    if kind in {"all", "notes"}:
        notes = [to_note(raw) for raw in c.backend().get_children(key, item_type="note")]
        if needle:
            notes = [n for n in notes if needle in n.text.lower()]

    sections = []
    if kind in {"all", "annotations"}:
        sections.append(render_annotations(annotations, heading="Annotations"))
    if kind in {"all", "notes"}:
        sections.append(render_notes(notes, heading="Notes"))

    if not annotations and not notes:
        hint = (
            "\n\n*Zotero stores annotations inside a PDF attachment. If this item has "
            "annotations you can see in Zotero, check that its PDF is synced to this "
            "machine.*"
        )
        sections.append(hint)

    return c.respond(
        "\n\n---\n\n".join(sections),
        {
            "item_key": key,
            "annotations": [a.model_dump(mode="json", exclude_none=True) for a in annotations],
            "notes": [n.model_dump(mode="json", exclude_none=True) for n in notes],
        },
    )


__all__ = ["NotFound", "zotero_get_annotations", "zotero_get_item", "zotero_read"]
