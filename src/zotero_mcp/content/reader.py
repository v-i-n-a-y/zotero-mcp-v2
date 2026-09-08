# Copyright 2026 Vinay

"""Reading an item's content, always within a budget.

This is the module that replaces ``get_item_fulltext``. The predecessor
returned an entire paper as one string and prepended a warning that said so,
after the text had already been serialised, so the tokens were spent before
anyone could act on the warning. A tool description asking the model not to
call it is not a bound.

What replaces it:

* attachments are chosen deliberately, preferring a PDF with a readable file;
* PDFs are read by 1-based page range, never whole;
* everything else is clamped to a character budget;
* the result always states what it covered and how to continue.

Cleaning up after a download is handled here too, and carefully: a path that
came out of the user's own Zotero storage must never be deleted, only one this
process downloaded into its own temporary directory.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from zotero_mcp.backends.base import LibraryBackend
from zotero_mcp.config import LimitSettings
from zotero_mcp.content import ranges
from zotero_mcp.content.extract import (
    extract_epub,
    extract_generic,
    extract_pdf_pages,
    pdf_outline,
    pdf_page_count,
    tidy,
)
from zotero_mcp.errors import NotFound, Unsupported
from zotero_mcp.models import AttachmentRef, ContentChunk, OutlineEntry
from zotero_mcp.paging import clamp

logger = logging.getLogger(__name__)

#: Marks a directory this process created for a download. Only a file whose
#: parent carries this prefix may ever be deleted.
TEMP_PREFIX = "zotero_mcp_dl_"

#: Attachment preference. A PDF with a text layer is what a research question
#: usually wants; an HTML snapshot is a reasonable second; a linked URL with no
#: file is last because it has no bytes to read at all.
_CONTENT_PRIORITY = {
    "application/pdf": 0,
    "application/epub+zip": 1,
    "text/html": 2,
    "application/xhtml+xml": 2,
    "text/plain": 3,
}


def _priority(content_type: str | None) -> int:
    return _CONTENT_PRIORITY.get((content_type or "").split(";")[0].strip(), 9)


def choose_attachment(
    children: list[dict[str, Any]], *, prefer: str | None = None
) -> dict[str, Any] | None:
    """Pick the attachment most likely to answer a content question.

    Args:
        children: Raw child items of the parent.
        prefer: An explicit attachment key, which always wins if present.
    """
    attachments = [
        child for child in children if (child.get("data") or child).get("itemType") == "attachment"
    ]
    if not attachments:
        return None

    if prefer:
        for child in attachments:
            if (child.get("key") or (child.get("data") or {}).get("key")) == prefer:
                return child

    def rank(child: dict[str, Any]) -> tuple[int, int]:
        data = child.get("data") or child
        # A linked_url attachment has no bytes, so it sorts last regardless of
        # its declared content type.
        has_file = 0 if data.get("linkMode") != "linked_url" else 1
        return (has_file, _priority(data.get("contentType")))

    return sorted(attachments, key=rank)[0]


def cleanup_download(path: Path | None) -> None:
    """Remove a file this process downloaded, and nothing else.

    Two guards, both of which have bitten real code. The parent directory must
    carry :data:`TEMP_PREFIX`, and it must be a strict subdirectory of the
    temporary root. Without the first, a path like ``/tmp/paper.pdf`` has
    ``/tmp`` as its parent and a naive "is it under the temp dir" test passes,
    which deletes the entire system temporary directory. Without the second,
    the temporary root itself could be the target.
    """
    if path is None:
        return
    try:
        parent = path.resolve().parent
        root = Path(tempfile.gettempdir()).resolve()
        if not parent.is_dir() or parent == root:
            return
        if root not in parent.parents:
            return
        if not parent.name.startswith(TEMP_PREFIX):
            return
        shutil.rmtree(parent, ignore_errors=True)
    except OSError as exc:
        logger.debug("Could not clean up %s: %s", path, exc)


def _is_download(path: Path) -> bool:
    return path.parent.name.startswith(TEMP_PREFIX)


def read_content(
    backend: LibraryBackend,
    item_key: str,
    *,
    limits: LimitSettings,
    pages: str | int | None = None,
    attachment_key: str | None = None,
    title: str | None = None,
) -> ContentChunk:
    """Read a bounded slice of an item's attachment.

    Resolution order for a PDF: read the requested pages from the file. If the
    file is not reachable, fall back to Zotero's own full-text index, clamped,
    and say that page ranges were unavailable rather than silently ignoring
    them.
    """
    children = backend.get_children(item_key, item_type="attachment")
    attachment = choose_attachment(children, prefer=attachment_key)

    if attachment is None:
        # The item may itself be the attachment; that is a normal thing to ask for.
        candidate = backend.get_item(item_key)
        if candidate and (candidate.get("data") or {}).get("itemType") == "attachment":
            attachment = candidate

    if attachment is None:
        raise NotFound(
            f"Item {item_key} has no attachment to read.",
            hint="Use zotero_get_item to see its metadata, or attach a file first.",
        )

    data = attachment.get("data") or attachment
    key = attachment.get("key") or data.get("key") or ""
    content_type = (data.get("contentType") or "").split(";")[0].strip()
    display_title = title or data.get("title") or data.get("filename") or item_key

    path = backend.resolve_attachment_path(key)
    downloaded = path is not None and _is_download(path)

    try:
        if path is not None and content_type == "application/pdf":
            return _read_pdf(path, item_key, key, display_title, pages, limits)
        if path is not None and content_type == "application/epub+zip":
            text = extract_epub(path, max_chars=limits.max_content_chars)
            return _wrap(item_key, key, display_title, text, limits, source="epub")
        if path is not None:
            text = extract_generic(path, max_chars=limits.max_content_chars)
            return _wrap(item_key, key, display_title, text, limits, source=content_type or "file")
    except Unsupported:
        # No extractor installed. Zotero's own index may still have the text,
        # which is a better answer than refusing outright.
        indexed = backend.get_fulltext(key)
        if indexed:
            return _wrap(
                item_key, key, display_title, tidy(indexed), limits, source="Zotero full-text index"
            )
        raise
    finally:
        if downloaded and path is not None:
            cleanup_download(path)

    indexed = backend.get_fulltext(key)
    if indexed:
        chunk = _wrap(
            item_key, key, display_title, tidy(indexed), limits, source="Zotero full-text index"
        )
        if pages:
            chunk.text = (
                "*The attachment file is not available locally, so page ranges could "
                "not be applied. This is Zotero's indexed text for the whole document.*\n\n"
                + chunk.text
            )
        return chunk

    raise NotFound(
        f"No readable content for {item_key}: the attachment file is not available "
        "and Zotero has no indexed text for it.",
        hint=(
            "In local mode the file may not be synced to this machine. "
            "Check the attachment in Zotero, or set Web API credentials so the "
            "file can be downloaded."
        ),
    )


def _read_pdf(
    path: Path,
    item_key: str,
    attachment_key: str,
    title: str,
    pages: str | int | None,
    limits: LimitSettings,
) -> ContentChunk:
    total = pdf_page_count(path)
    wanted = ranges.parse_pages(pages, total=total, cap=limits.max_pdf_pages)
    if not wanted:
        wanted = ranges.default_pages(total, size=limits.default_pdf_pages)

    text, total = extract_pdf_pages(path, wanted)
    clamped = clamp(text, limits.max_content_chars)
    following = ranges.next_range(wanted, total=total, size=limits.default_pdf_pages)

    return ContentChunk(
        item_key=item_key,
        attachment_key=attachment_key,
        title=title,
        text=clamped.text,
        first_page=min(wanted) if wanted else None,
        last_page=max(wanted) if wanted else None,
        total_pages=total,
        has_more=following is not None,
        next_pages=following,
        truncated=clamped.truncated,
        source="PDF text layer",
        chars=len(clamped.text),
    )


def _wrap(
    item_key: str,
    attachment_key: str,
    title: str,
    text: str,
    limits: LimitSettings,
    *,
    source: str,
) -> ContentChunk:
    clamped = clamp(text, limits.max_content_chars)
    return ContentChunk(
        item_key=item_key,
        attachment_key=attachment_key,
        title=title,
        text=clamped.text,
        truncated=clamped.truncated,
        source=source,
        chars=len(clamped.text),
    )


def read_outline(backend: LibraryBackend, item_key: str) -> tuple[list[OutlineEntry], str]:
    """A PDF attachment's table of contents, with the attachment title."""
    children = backend.get_children(item_key, item_type="attachment")
    attachment = choose_attachment(children)
    if attachment is None:
        raise NotFound(f"Item {item_key} has no attachment.")

    data = attachment.get("data") or attachment
    key = attachment.get("key") or data.get("key") or ""
    path = backend.resolve_attachment_path(key)
    if path is None:
        raise NotFound(f"The attachment file for {item_key} is not available on this machine.")
    try:
        return pdf_outline(path), data.get("title") or item_key
    finally:
        if _is_download(path):
            cleanup_download(path)


def describe_attachments(children: list[dict[str, Any]]) -> list[AttachmentRef]:
    """Summarise an item's attachments, flagging ones with no reachable file."""
    from zotero_mcp.mapping import to_attachment_ref

    refs = []
    for child in children:
        data = child.get("data") or child
        if data.get("itemType") != "attachment":
            continue
        available = None if data.get("linkMode") != "linked_url" else False
        refs.append(to_attachment_ref(child, available=available))
    return refs


__all__ = [
    "TEMP_PREFIX",
    "choose_attachment",
    "cleanup_download",
    "describe_attachments",
    "read_content",
    "read_outline",
]
