# Copyright 2026 Vinay

"""Page ranges, the vocabulary of a bounded document read.

A caller says ``pages="1-5"``, ``pages="3"``, ``pages="1-5,12,20-22"`` or
nothing at all. This module is the only place that syntax is understood, and
it is pure, so every edge case is a unit test rather than a surprise inside a
tool.

Ranges are 1-based and inclusive, matching what a person reads off a page and
what Zotero shows, not PyMuPDF's 0-based index. That conversion happens once,
at the extraction boundary.
"""

from __future__ import annotations

import re

from zotero_mcp.errors import InvalidInput

_PART = re.compile(r"^\s*(\d+)\s*(?:[-–:]\s*(\d+)\s*)?$")


def parse_pages(spec: str | int | None, *, total: int | None = None, cap: int = 40) -> list[int]:
    """Turn a page specification into a sorted list of 1-based page numbers.

    Args:
        spec: ``"1-5"``, ``"3"``, ``"1-5,12"``, an int, or None for "not specified".
        total: Page count, when known. Pages beyond it are dropped rather than
            rejected, because a caller continuing through a document should not
            have to know exactly where it ends.
        cap: Most pages one call may return.

    Raises:
        InvalidInput: The specification is unparseable or inverted.
    """
    if spec is None or spec == "":
        return []
    if isinstance(spec, int):
        parts = [str(spec)]
    else:
        parts = [p for p in str(spec).replace(" ", "").split(",") if p]

    pages: list[int] = []
    for part in parts:
        match = _PART.match(part)
        if not match:
            raise InvalidInput(
                f"Cannot read page range {part!r}.",
                hint="Use a form like pages='1-5', pages='3', or pages='1-5,12'.",
            )
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1:
            raise InvalidInput("Page numbers start at 1.", hint=f"Got {first}.")
        if last < first:
            raise InvalidInput(
                f"Page range {part!r} runs backwards.",
                hint=f"Did you mean '{last}-{first}'?",
            )
        pages.extend(range(first, last + 1))

    unique = sorted(set(pages))
    if total is not None:
        unique = [p for p in unique if p <= total]
    return unique[:cap]


def default_pages(total: int | None, *, size: int) -> list[int]:
    """The opening pages, used when the caller did not ask for a range."""
    last = min(size, total) if total else size
    return list(range(1, last + 1))


def describe(pages: list[int]) -> str:
    """Render a page list back to compact range syntax, for round-tripping.

    Used to build the ``next_pages`` hint, so the caller can continue with a
    value it can pass straight back rather than computing one.
    """
    if not pages:
        return ""
    spans: list[tuple[int, int]] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        spans.append((start, previous))
        start = previous = page
    spans.append((start, previous))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def next_range(pages: list[int], *, total: int | None, size: int) -> str | None:
    """The range that continues after *pages*, or None at the end of the document."""
    if not pages:
        return None
    following = max(pages) + 1
    if total is not None and following > total:
        return None
    last = following + size - 1
    if total is not None:
        last = min(last, total)
    return describe(list(range(following, last + 1)))


__all__ = ["default_pages", "describe", "next_range", "parse_pages"]
