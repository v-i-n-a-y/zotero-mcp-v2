"""Cursors and clamping — the machinery that keeps a response bounded.

Two separate problems, both of which the predecessor left unsolved.

**Too many rows.** Its list helper paginated the Zotero API into memory until
the library was exhausted and *then* sliced, so "show me my collections"
downloaded every collection. Here a page is a page: the backend is asked for
one page, and a cursor carries the position for the next call.

**Too much text.** A single item's fulltext could be a hundred thousand
characters, and the only mitigation was a warning prepended to text that had
already been serialised — the tokens were spent before the caller read the
warning. Here :func:`clamp` cuts to a real budget, on a sensible boundary, and
says what it did.

Cursors are opaque base64url JSON. They are bound to the query that produced
them, so replaying a cursor against a different search reports a clear error
instead of quietly paging through the wrong result set.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from zotero_mcp.errors import InvalidInput

T = TypeVar("T")

#: Bumped if the cursor payload shape ever changes, so an old cursor held by a
#: client across an upgrade is rejected cleanly instead of misread.
_CURSOR_VERSION = 1


def _fingerprint(query: dict[str, Any]) -> str:
    """Short stable digest of the query parameters a cursor belongs to."""
    canonical = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def encode_cursor(offset: int, query: dict[str, Any]) -> str:
    """Build an opaque cursor for the next page after *offset*."""
    payload = {"v": _CURSOR_VERSION, "o": int(offset), "q": _fingerprint(query)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, query: dict[str, Any]) -> int:
    """Return the offset carried by *cursor*, verifying it belongs to *query*.

    Raises:
        InvalidInput: The cursor is malformed, from an incompatible version, or
            was issued for a different query.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidInput(
            "Malformed cursor.",
            hint="Pass the exact 'next_cursor' from the previous response, or omit it to start over.",
        ) from exc

    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidInput(
            "Cursor is from an incompatible version.",
            hint="Omit the cursor to restart the listing.",
        )
    if payload.get("q") != _fingerprint(query):
        raise InvalidInput(
            "Cursor does not belong to these search parameters.",
            hint="Keep every other argument identical when paging, or omit the cursor to start over.",
        )
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise InvalidInput("Cursor carries an invalid offset.")
    return offset


@dataclass
class Page(Generic[T]):
    """One page of results plus everything needed to ask for the next.

    ``total`` is the library-wide count when the backend reports one and
    ``None`` when it does not. Zotero returns it as a header on most list
    endpoints but not all, and inventing a total by exhausting the collection
    is exactly the behaviour this class exists to prevent.
    """

    items: list[T]
    offset: int
    page_size: int
    total: int | None = None
    next_cursor: str | None = None
    query: dict[str, Any] = field(default_factory=dict)

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def shown_range(self) -> tuple[int, int]:
        """1-based inclusive range this page covers, for human-readable output."""
        if not self.items:
            return (0, 0)
        return (self.offset + 1, self.offset + len(self.items))

    @classmethod
    def build(
        cls,
        items: list[T],
        *,
        offset: int,
        page_size: int,
        query: dict[str, Any],
        total: int | None = None,
        has_more: bool | None = None,
    ) -> Page[T]:
        """Assemble a page, issuing a cursor only when more results exist.

        When *has_more* is not supplied it is inferred from a full page — the
        standard trick, and mildly over-eager: a result set that is an exact
        multiple of the page size yields one final empty page. That is
        preferable to the alternative, which is silently dropping the tail.
        """
        if has_more is None:
            has_more = len(items) == page_size
            if total is not None:
                has_more = offset + len(items) < total
        cursor = encode_cursor(offset + len(items), query) if has_more and items else None
        return cls(
            items=items,
            offset=offset,
            page_size=page_size,
            total=total,
            next_cursor=cursor,
            query=query,
        )


def normalize_page_size(value: int | str | None, *, default: int, maximum: int) -> int:
    """Coerce a caller-supplied page size into the permitted range.

    Models pass limits as strings, as floats, as ``"all"``, and as numbers far
    larger than anything useful. Every one of those is clamped rather than
    rejected: refusing the call teaches nothing, whereas returning a sensible
    page with the real size stated in the response does.
    """
    if value is None or value == "":
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"all", "max", "everything"}:
            return maximum
        try:
            value = int(float(text))
        except ValueError:
            return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


@dataclass
class Clamped:
    """Result of clamping text to a budget."""

    text: str
    truncated: bool
    original_chars: int
    kept_chars: int

    @property
    def note(self) -> str:
        """A line to append to output, or empty when nothing was cut."""
        if not self.truncated:
            return ""
        percent = round(100 * self.kept_chars / max(self.original_chars, 1))
        return (
            f"\n\n---\n*Truncated: showing {self.kept_chars:,} of "
            f"{self.original_chars:,} characters ({percent}%).*"
        )


#: Boundaries to back off to when cutting, best first. Cutting mid-word makes
#: text harder to read and can corrupt an identifier that the caller then
#: quotes back at us.
_BREAKS = ("\n\n", "\n", ". ", " ")


def clamp(text: str, limit: int, *, tolerance: float = 0.15) -> Clamped:
    """Cut *text* to *limit* characters on the nearest sensible boundary.

    The cut point is searched backwards from the limit by up to *tolerance* of
    it, preferring a paragraph break, then a line break, then a sentence end,
    then a space. If none is found in that window the text is cut hard, since a
    single unbroken run that long is machine output where a clean break does
    not exist anyway.
    """
    original = len(text)
    if original <= limit:
        return Clamped(text=text, truncated=False, original_chars=original, kept_chars=original)

    window = max(1, int(limit * tolerance))
    cut = limit
    for boundary in _BREAKS:
        found = text.rfind(boundary, limit - window, limit)
        if found > 0:
            cut = found
            break

    kept = text[:cut].rstrip()
    return Clamped(text=kept, truncated=True, original_chars=original, kept_chars=len(kept))


def clamp_with_note(text: str, limit: int) -> str:
    """:func:`clamp`, with the truncation note already appended."""
    result = clamp(text, limit)
    return result.text + result.note


def estimate_tokens(text: str) -> int:
    """Rough token count for *text*.

    Four characters per token is the usual English approximation. It is only
    used for advisory messages, never for a decision that must be exact —
    every real limit in this package is expressed in characters, which are
    cheap and unambiguous to count.
    """
    return len(text) // 4


__all__ = [
    "Clamped",
    "Page",
    "clamp",
    "clamp_with_note",
    "decode_cursor",
    "encode_cursor",
    "estimate_tokens",
    "normalize_page_size",
]
