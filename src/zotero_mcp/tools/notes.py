# Copyright 2026 Vinay

"""Creating and editing notes and annotations.

Two tools replace six. ``zotero_create_note``, ``zotero_update_note``,
``zotero_delete_note``, ``zotero_create_annotation`` and
``zotero_create_area_annotation`` are the same operation with an action and a
shape, which is what they become here.

Notes are HTML in Zotero. Callers write markdown or plain text far more often
than HTML, so text is converted unless ``html=True`` is passed. Getting that
backwards produces notes displaying literal ``<p>`` tags, which is the
single most common complaint about note-writing tools.
"""

from __future__ import annotations

import html as html_module
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from zotero_mcp.app import mcp
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.mapping import to_note
from zotero_mcp.models import Change, WriteResult
from zotero_mcp.render import render_write_result
from zotero_mcp.tools import _common as c

#: Zotero's own annotation types. 'image' is what the UI calls an area
#: selection, and is the one that needs coordinates.
ANNOTATION_TYPES = ("highlight", "underline", "note", "image", "ink", "text")

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_BULLET = re.compile(r"^[-*]\s+(.+)$", re.MULTILINE)


def text_to_html(text: str) -> str:
    """Convert plain text or light markdown to the HTML Zotero stores.

    Deliberately small: headings, bold, italic, inline code, bullets and
    paragraphs. A full markdown engine would be a dependency and a much larger
    surface for producing HTML Zotero's editor then mangles.
    """
    escaped = html_module.escape(text.strip())

    escaped = _MD_HEADING.sub(
        lambda m: f"<h{min(len(m.group(1)), 6)}>{m.group(2).strip()}</h{min(len(m.group(1)), 6)}>",
        escaped,
    )
    escaped = _MD_BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _MD_ITALIC.sub(r"<em>\1</em>", escaped)
    escaped = _MD_CODE.sub(r"<code>\1</code>", escaped)

    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", escaped):
        block = block.strip()
        if not block:
            continue
        if block.startswith("<h"):
            blocks.append(block)
            continue
        if _MD_BULLET.match(block):
            items = "".join(f"<li>{m.group(1).strip()}</li>" for m in _MD_BULLET.finditer(block))
            blocks.append(f"<ul>{items}</ul>")
            continue
        blocks.append("<p>" + block.replace("\n", "<br/>") + "</p>")
    return "".join(blocks) or "<p></p>"


@mcp.tool(
    name="zotero_manage_note",
    description=(
        "Create, update or delete a note, either attached to an item or standalone. "
        "Accepts plain text or markdown and converts it to the HTML Zotero stores."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    tags={"notes", "core"},
)
@c.tool
def zotero_manage_note(
    action: Annotated[Literal["create", "update", "delete"], Field(description="What to do.")],
    text: Annotated[
        str | None,
        Field(description="Note content, as plain text or light markdown."),
    ] = None,
    note_key: Annotated[
        str | None, Field(description="The note to change. Required for update and delete.")
    ] = None,
    parent_item_key: Annotated[
        str | None,
        Field(description="Attach the new note to this item. Omit for a standalone note."),
    ] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags for the note.")] = None,
    html: Annotated[
        bool,
        Field(description="Treat 'text' as raw HTML rather than converting it."),
    ] = False,
    append: Annotated[
        bool,
        Field(description="On update, add to the end of the note rather than replacing it."),
    ] = False,
    dry_run: Annotated[bool, Field(description="Preview without applying.")] = False,
) -> Any:
    """Manage a note."""
    backend = c.backend()

    if action == "delete":
        if not note_key:
            raise InvalidInput("note_key is required to delete a note.")
        key = c.resolve_item_key(note_key)
        item = c.require_item(key)
        if (item.get("data") or {}).get("itemType") != "note":
            raise InvalidInput(f"{key} is not a note.", hint="Use zotero_manage_items instead.")
        if dry_run:
            note = to_note(item)
            result = WriteResult(
                action="delete_note",
                dry_run=True,
                succeeded=[key],
                message=f"Would delete: {note.title or 'Untitled note'}",
            )
            return c.respond(render_write_result(result), result)
        outcome = backend.delete_items({key: item.get("version", 0)})
        result = c.outcome_to_result(outcome, action="delete_note")
        return c.respond(render_write_result(result), result)

    if not text or not text.strip():
        raise InvalidInput("A note needs some text.")

    body = text if html else text_to_html(text)
    tag_payload = [{"tag": tag} for tag in c.normalise_list(tags)]

    if action == "create":
        data: dict[str, Any] = {"itemType": "note", "note": body}
        if tag_payload:
            data["tags"] = tag_payload
        if parent_item_key:
            data["parentItem"] = c.resolve_item_key(parent_item_key)

        if dry_run:
            result = WriteResult(
                action="create_note", dry_run=True, changes=[Change(field="note", after=body[:300])]
            )
            return c.respond(render_write_result(result), result)

        outcome = backend.create_items([data])
        if outcome.failed:
            raise InvalidInput(f"Zotero rejected the note: {'; '.join(outcome.failed.values())}")
        result = c.outcome_to_result(outcome, action="create_note")
        return c.respond(render_write_result(result), result)

    if not note_key:
        raise InvalidInput("note_key is required to update a note.")
    key = c.resolve_item_key(note_key)
    item = c.require_item(key)
    data = item.get("data") or {}
    if data.get("itemType") != "note":
        raise InvalidInput(f"{key} is not a note.")

    existing = data.get("note") or ""
    new_body = (existing + body) if append else body
    patch: dict[str, Any] = {"note": new_body}
    if tag_payload:
        patch["tags"] = tag_payload

    changes = [Change(field="note", before=existing[:200], after=new_body[:200])]
    if dry_run:
        result = WriteResult(action="update_note", dry_run=True, succeeded=[key], changes=changes)
        return c.respond(render_write_result(result), result)

    outcome = backend.update_item(key, patch, version=item.get("version", 0))
    result = c.outcome_to_result(outcome, action="update_note", changes=changes)
    return c.respond(render_write_result(result), result)


def _resolve_annotation_parent(item_key: str) -> str:
    """The attachment an annotation must hang from.

    Annotations are children of an attachment, never of the item, so a caller
    naming the paper has to be redirected to its PDF. Doing that here rather
    than making the caller do it is why annotation tools in the predecessors
    kept failing with "parent must be an attachment".
    """
    key = c.resolve_item_key(item_key)
    item = c.require_item(key)
    if (item.get("data") or {}).get("itemType") == "attachment":
        return key

    from zotero_mcp.content.reader import choose_attachment

    attachment = choose_attachment(c.backend().get_children(key, item_type="attachment"))
    if attachment is None:
        raise NotFound(
            f"Item {key} has no attachment to annotate.",
            hint="Annotations live on a PDF; attach one first.",
        )
    return attachment.get("key") or (attachment.get("data") or {}).get("key") or ""


@mcp.tool(
    name="zotero_manage_annotation",
    description=(
        "Create, update or delete a PDF annotation: a highlight, an underline, a "
        "sticky note, or an area selection. Attaches to the item's PDF automatically."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    tags={"annotations", "core"},
)
@c.tool
def zotero_manage_annotation(
    action: Annotated[Literal["create", "update", "delete"], Field(description="What to do.")],
    item_key: Annotated[
        str | None,
        Field(description="The item or attachment to annotate. Required for create."),
    ] = None,
    annotation_key: Annotated[
        str | None, Field(description="The annotation to change. Required for update and delete.")
    ] = None,
    annotation_type: Annotated[
        Literal["highlight", "underline", "note", "image", "ink", "text"],
        Field(description="'image' is an area selection and needs a rectangle."),
    ] = "highlight",
    text: Annotated[
        str | None, Field(description="The quoted passage, for a highlight or underline.")
    ] = None,
    comment: Annotated[str | None, Field(description="Your own comment on the annotation.")] = None,
    page: Annotated[
        int | None, Field(description="1-based page number the annotation sits on.")
    ] = None,
    rect: Annotated[
        list[float] | None,
        Field(
            description=(
                "Area rectangle as [x0, y0, x1, y1] in PDF user-space units. Get the "
                "page's coordinate space from zotero_read(what='outline') or the "
                "page layout before guessing."
            )
        ),
    ] = None,
    color: Annotated[
        str | None, Field(description="Highlight colour as a hex string, e.g. '#ffd400'.")
    ] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags for the annotation.")] = None,
    dry_run: Annotated[bool, Field(description="Preview without applying.")] = False,
) -> Any:
    """Manage an annotation."""
    backend = c.backend()

    if action == "delete":
        if not annotation_key:
            raise InvalidInput("annotation_key is required to delete an annotation.")
        key = c.resolve_item_key(annotation_key)
        item = c.require_item(key)
        if dry_run:
            result = WriteResult(action="delete_annotation", dry_run=True, succeeded=[key])
            return c.respond(render_write_result(result), result)
        outcome = backend.delete_items({key: item.get("version", 0)})
        result = c.outcome_to_result(outcome, action="delete_annotation")
        return c.respond(render_write_result(result), result)

    if action == "update":
        if not annotation_key:
            raise InvalidInput("annotation_key is required to update an annotation.")
        key = c.resolve_item_key(annotation_key)
        item = c.require_item(key)
        data = item.get("data") or {}
        patch: dict[str, Any] = {}
        changes: list[Change] = []
        for value, field in (
            (text, "annotationText"),
            (comment, "annotationComment"),
            (color, "annotationColor"),
        ):
            if value is not None and value != data.get(field):
                patch[field] = value
                changes.append(Change(field=field, before=data.get(field), after=value))
        if tag_list := c.normalise_list(tags):
            patch["tags"] = [{"tag": tag} for tag in tag_list]
            changes.append(Change(field="tags", after=", ".join(tag_list)))
        if not patch:
            result = WriteResult(
                action="update_annotation", unchanged=[key], message="Nothing to change."
            )
            return c.respond(render_write_result(result), result)
        if dry_run:
            result = WriteResult(
                action="update_annotation", dry_run=True, succeeded=[key], changes=changes
            )
            return c.respond(render_write_result(result), result)
        outcome = backend.update_item(key, patch, version=item.get("version", 0))
        result = c.outcome_to_result(outcome, action="update_annotation", changes=changes)
        return c.respond(render_write_result(result), result)

    if not item_key:
        raise InvalidInput("item_key is required to create an annotation.")
    if annotation_type in {"highlight", "underline"} and not text:
        raise InvalidInput(
            f"A {annotation_type} needs the text it marks.",
            hint="Read the page with zotero_read first and quote the passage exactly.",
        )
    if annotation_type == "image" and not rect:
        raise InvalidInput(
            "An area annotation needs a rectangle.",
            hint="Pass rect=[x0, y0, x1, y1] in PDF user-space units.",
        )
    if page is None:
        raise InvalidInput("An annotation needs a page number.", hint="Pages are 1-based.")

    parent_key = _resolve_annotation_parent(item_key)

    # Zotero's position is 0-based and its rects are a list of rectangles.
    position: dict[str, Any] = {"pageIndex": max(0, page - 1)}
    position["rects"] = [list(rect)] if rect else []

    data = {
        "itemType": "annotation",
        "parentItem": parent_key,
        "annotationType": annotation_type,
        "annotationText": text or "",
        "annotationComment": comment or "",
        "annotationColor": color or "#ffd400",
        "annotationPageLabel": str(page),
        "annotationPosition": position,
        # Zotero orders annotations by this string, not by page alone. The
        # format is zero-padded page, x, y, so lexical order is reading order.
        "annotationSortIndex": f"{page - 1:05d}|{int(rect[1]) if rect else 0:06d}|{int(rect[0]) if rect else 0:05d}",
    }
    if tag_list := c.normalise_list(tags):
        data["tags"] = [{"tag": tag} for tag in tag_list]

    if dry_run:
        result = WriteResult(
            action="create_annotation",
            dry_run=True,
            changes=[Change(field=k, after=str(v)[:120]) for k, v in sorted(data.items())],
            message=f"Would attach to `{parent_key}`.",
        )
        return c.respond(render_write_result(result), result)

    outcome = backend.create_items([data])
    if outcome.failed:
        raise InvalidInput(
            f"Zotero rejected the annotation: {'; '.join(outcome.failed.values())}",
            hint="Annotations can only be created on a PDF attachment that is synced.",
        )
    result = c.outcome_to_result(
        outcome, action="create_annotation", message=f"Attached to `{parent_key}`."
    )
    return c.respond(render_write_result(result), result)


__all__ = [
    "ANNOTATION_TYPES",
    "text_to_html",
    "zotero_manage_annotation",
    "zotero_manage_note",
]
