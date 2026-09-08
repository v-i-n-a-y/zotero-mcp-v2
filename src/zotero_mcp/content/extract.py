# Copyright 2026 Vinay

"""Getting text out of the files Zotero stores.

Every extractor is optional at import time. PyMuPDF and EbookLib live behind
the ``[pdf]`` extra, so a base install still starts, still searches, still
reads metadata, and reports a precise "install this extra" when asked for
something it genuinely cannot do. Raising :class:`Unsupported` with the exact
pip command beats an ImportError traceback from inside a tool.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from zotero_mcp.errors import InvalidInput, Unsupported
from zotero_mcp.models import OutlineEntry

logger = logging.getLogger(__name__)

PDF_EXTRA_HINT = "Install the PDF extra: pip install 'zotero-mcp-next[pdf]'"

#: Whitespace normalisation applied to every extracted page. PDF text layers
#: are full of soft hyphens, ligatures and column artefacts that cost tokens
#: and help nobody.
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_MANY_SPACES = re.compile(r"[ \t ]{2,}")
_MANY_BLANKS = re.compile(r"\n{3,}")


def _import_fitz():
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError as exc:
        raise Unsupported("Reading PDFs needs PyMuPDF.", hint=PDF_EXTRA_HINT) from exc
    return fitz


def tidy(text: str) -> str:
    """Normalise extracted text without changing its content.

    Rejoining hyphenated line breaks is the one transformation that changes
    characters, and it is worth it: without it, searching or quoting a word
    that happened to wrap is impossible.
    """
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = text.replace("­", "")
    text = _MANY_SPACES.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _MANY_BLANKS.sub("\n\n", text).strip()


def pdf_page_count(path: Path) -> int:
    """Number of pages in a PDF."""
    fitz = _import_fitz()
    try:
        with fitz.open(str(path)) as document:
            return document.page_count
    except Exception as exc:
        raise InvalidInput(f"Could not open {path.name} as a PDF: {exc}") from exc


def extract_pdf_pages(path: Path, pages: list[int]) -> tuple[str, int]:
    """Extract 1-based *pages* from a PDF. Returns (text, total_pages).

    Pages are labelled in the output. Without labels a model asked to "quote
    the passage on page 12" has no way to tell which page a sentence came
    from, which is the whole reason for reading by range.
    """
    fitz = _import_fitz()
    try:
        with fitz.open(str(path)) as document:
            total = document.page_count
            chunks: list[str] = []
            for number in pages:
                if not 1 <= number <= total:
                    continue
                page_text = tidy(document.load_page(number - 1).get_text("text"))
                chunks.append(
                    f"\n\n## Page {number}\n\n{page_text}"
                    if page_text
                    else f"\n\n## Page {number}\n\n*(no extractable text)*"
                )
            return "".join(chunks).strip(), total
    except Unsupported:
        raise
    except Exception as exc:
        raise InvalidInput(f"Could not read pages from {path.name}: {exc}") from exc


def pdf_outline(path: Path) -> list[OutlineEntry]:
    """The document's embedded table of contents, if it has one."""
    fitz = _import_fitz()
    try:
        with fitz.open(str(path)) as document:
            return [
                OutlineEntry(
                    level=max(1, int(level)),
                    title=str(title).strip(),
                    page=int(page) if page and page > 0 else None,
                )
                for level, title, page in document.get_toc(simple=True)
            ]
    except Exception as exc:  # noqa: BLE001 (an absent outline is normal, not an error)
        logger.debug("No usable outline in %s: %s", path, exc)
        return []


def pdf_page_layout(path: Path, page: int) -> dict:
    """Geometry of one page, for placing area annotations.

    Area annotations need a coordinate space, and PDF user-space units are not
    something a caller can guess. Returning the page box alongside the text
    blocks lets a caller express "the figure in the upper right" as real
    coordinates.
    """
    fitz = _import_fitz()
    with fitz.open(str(path)) as document:
        if not 1 <= page <= document.page_count:
            raise InvalidInput(
                f"Page {page} is outside this document (1-{document.page_count}).",
            )
        loaded = document.load_page(page - 1)
        rect = loaded.rect
        blocks = []
        for block in loaded.get_text("blocks"):
            x0, y0, x1, y1, text, *_ = block
            snippet = tidy(str(text))[:160]
            if snippet:
                blocks.append(
                    {
                        "rect": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                        "text": snippet,
                    }
                )
        return {
            "page": page,
            "width": round(rect.width, 1),
            "height": round(rect.height, 1),
            "rotation": loaded.rotation,
            "blocks": blocks,
        }


def extract_epub(path: Path, *, max_chars: int) -> str:
    """Flatten an EPUB to text, in spine order."""
    try:
        import ebooklib  # type: ignore[import-untyped]
        from ebooklib import epub  # type: ignore[import-untyped]
    except ImportError as exc:
        raise Unsupported("Reading EPUBs needs EbookLib.", hint=PDF_EXTRA_HINT) from exc

    from zotero_mcp.mapping import strip_html

    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        raise InvalidInput(f"Could not open {path.name} as an EPUB: {exc}") from exc

    parts: list[str] = []
    length = 0
    for document in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        text = strip_html(document.get_content().decode("utf-8", errors="replace"))
        if not text:
            continue
        parts.append(text)
        length += len(text)
        # Stop reading once the budget is met; parsing the rest of a long book
        # only to discard it is pure latency.
        if length >= max_chars:
            break
    return tidy("\n\n".join(parts))[:max_chars]


def extract_generic(path: Path, *, max_chars: int) -> str:
    """Last resort for HTML, text and office formats.

    Tries markitdown when available, since it handles the office formats, then
    falls back to reading the file as text. A snapshot of a web page is the
    common case and is plain HTML.
    """
    suffix = path.suffix.lower()

    if suffix in {".html", ".htm", ".xhtml"}:
        from zotero_mcp.mapping import strip_html

        raw = path.read_text(encoding="utf-8", errors="replace")
        return tidy(strip_html(raw))[:max_chars]

    if suffix in {".txt", ".md", ".csv", ".json", ".xml"}:
        return tidy(path.read_text(encoding="utf-8", errors="replace"))[:max_chars]

    try:
        from markitdown import MarkItDown  # type: ignore[import-untyped]

        return tidy(MarkItDown().convert(str(path)).text_content)[:max_chars]
    except ImportError as exc:
        raise Unsupported(
            f"No extractor available for {suffix or 'this file type'}.",
            hint=PDF_EXTRA_HINT,
        ) from exc
    except Exception as exc:
        raise InvalidInput(f"Could not extract text from {path.name}: {exc}") from exc


def available_extractors() -> dict[str, bool]:
    """Which optional extractors this installation actually has."""

    def present(module: str) -> bool:
        import importlib.util

        return importlib.util.find_spec(module) is not None

    return {
        "pdf": present("fitz"),
        "epub": present("ebooklib"),
        "markitdown": present("markitdown"),
    }


__all__ = [
    "available_extractors",
    "extract_epub",
    "extract_generic",
    "extract_pdf_pages",
    "pdf_outline",
    "pdf_page_count",
    "pdf_page_layout",
    "tidy",
]
