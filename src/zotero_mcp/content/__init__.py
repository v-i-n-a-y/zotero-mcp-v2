"""Turning items and attachments into embeddable text.

Two jobs, both pure and side-effect-free so they are unit-testable without a
Zotero client or an embedding model:

* build one compact *metadata document* per item — the fields worth embedding
  even when an item has no fulltext at all, and
* split long text into overlapping chunks small enough to embed cleanly.

Attachment extraction (PDF, EPUB) lives here too but is deliberately optional:
the parsers are imported lazily so the package works with the ``semantic``
extra absent, and any single file that fails to parse yields empty text rather
than taking an index build down with it.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from zotero_mcp.mapping import creators_of, strip_html, year_of

logger = logging.getLogger(__name__)


def metadata_document(data: dict[str, Any]) -> str:
    """A short, embedding-friendly rendering of an item's own fields.

    Ordered most-salient first (title, then creators, then abstract) because a
    truncating embedding model weights earlier tokens more, and a title carries
    far more retrieval signal than a publisher's name.
    """
    parts: list[str] = []
    title = data.get("title")
    if title:
        parts.append(str(title))

    creators = creators_of(data)
    if creators:
        names = ", ".join(c.display for c in creators if c.display)
        if names:
            parts.append(names)

    abstract = strip_html(data.get("abstractNote"))
    if abstract:
        parts.append(abstract)

    for field in ("publicationTitle", "bookTitle", "proceedingsTitle", "publisher"):
        if data.get(field):
            parts.append(str(data[field]))
            break

    year = year_of(data.get("date"))
    if year:
        parts.append(year)

    tags = [t.get("tag") for t in (data.get("tags") or []) if t.get("tag")]
    if tags:
        parts.append(" ".join(tags))

    return "\n".join(parts)


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split *text* into overlapping windows of roughly *size* characters.

    Splits on paragraph and sentence boundaries where one falls near the window
    edge, so a chunk rarely cuts a sentence in half; falls back to a hard cut
    only when no boundary is close. Overlap carries context across the seam so a
    passage spanning two chunks is still retrievable from either.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    if overlap >= size:
        overlap = size // 4

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            # Prefer a boundary in the last quarter of the window.
            floor = start + int(size * 0.75)
            for sep in ("\n\n", "\n", ". ", " "):
                idx = window.rfind(sep)
                if idx != -1 and start + idx >= floor:
                    end = start + idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def extract_text(data: bytes, content_type: str | None, filename: str | None) -> str:
    """Best-effort plain text from an attachment's bytes.

    Returns "" for anything unparseable or unsupported; a failed extraction is
    logged and swallowed so one bad file cannot abort an index build.
    """
    kind = _kind(content_type, filename)
    try:
        if kind == "pdf":
            return _extract_pdf(data)
        if kind == "epub":
            return _extract_epub(data)
        if kind in ("text", "html"):
            text = data.decode("utf-8", errors="replace")
            return strip_html(text) if kind == "html" else text
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the build
        logger.warning("Could not extract %s (%s): %s", filename, content_type, exc)
    return ""


def _kind(content_type: str | None, filename: str | None) -> str:
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if "pdf" in ct or name.endswith(".pdf"):
        return "pdf"
    if "epub" in ct or name.endswith(".epub"):
        return "epub"
    if "html" in ct or name.endswith((".html", ".htm")):
        return "html"
    if ct.startswith("text/") or name.endswith((".txt", ".md")):
        return "text"
    return "other"


def _extract_pdf(data: bytes) -> str:
    import fitz  # PyMuPDF

    text_parts: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text("text"))
    return "\n".join(text_parts).strip()


def _extract_epub(data: bytes) -> str:
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(io.BytesIO(data))
    parts: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        parts.append(strip_html(item.get_content().decode("utf-8", errors="replace")))
    return "\n".join(parts).strip()


__all__ = ["chunk_text", "extract_text", "metadata_document"]
