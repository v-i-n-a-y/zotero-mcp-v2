# Copyright 2026 Vinay

"""Shared plumbing for tools, so no tool has to reinvent it.

Everything here exists because the same three or four decisions recur in every
tool, and the servers this replaces made them slightly differently each time:
how to clamp a page size, how to turn a caller's item reference into a key, how
to preview a destructive write, how to build a page of results.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from zotero_mcp.backends.base import ItemQuery, LibraryBackend, RawPage, WriteOutcome
from zotero_mcp.config import LimitSettings
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.identifiers import IdentifierKind, is_zotero_key, parse_identifier
from zotero_mcp.mapping import to_item_summary
from zotero_mcp.models import Change, ItemSummary, ResultPage, WriteResult
from zotero_mcp.paging import decode_cursor, encode_cursor, normalize_page_size
from zotero_mcp.results import reply
from zotero_mcp.runtime import Runtime, get_runtime

T = TypeVar("T")


def runtime() -> Runtime:
    """The active runtime. A thin alias so tools read cleanly."""
    return get_runtime()


def backend() -> LibraryBackend:
    return get_runtime().backend


def limits() -> LimitSettings:
    return get_runtime().limits


def respond(markdown: str, structured: Any = None, *, limit: int | None = None) -> Any:
    """Build a tool result within the configured budget."""
    active = get_runtime()
    return reply(
        markdown,
        structured,
        limit=limit or active.limits.max_response_chars,
        allow_structured=active.structured,
    )


def page_size(value: int | str | None) -> int:
    active = get_runtime().limits
    return normalize_page_size(
        value, default=active.default_page_size, maximum=active.max_page_size
    )


def offset_from(cursor: str | None, fingerprint: dict[str, Any]) -> int:
    """Decode a cursor, or start at the beginning."""
    return decode_cursor(cursor, fingerprint) if cursor else 0


def build_result_page(
    raw: RawPage,
    *,
    fingerprint: dict[str, Any],
    size: int,
    abstract_chars: int | None = None,
    diagnostics: Any = None,
    has_pdf: dict[str, bool] | None = None,
) -> ResultPage:
    """Map a backend page of raw items into the structured result shape."""
    active = get_runtime()
    library = active.backend.library_ref()
    preview = abstract_chars if abstract_chars is not None else active.limits.abstract_preview_chars

    summaries = [
        to_item_summary(
            item,
            abstract_chars=preview,
            library=library,
            has_pdf=(has_pdf or {}).get(item.get("key") or ""),
        )
        for item in raw.items
    ]

    has_more = (
        raw.offset + len(summaries) < raw.total if raw.total is not None else len(summaries) == size
    )
    cursor = (
        encode_cursor(raw.offset + len(summaries), fingerprint) if has_more and summaries else None
    )

    return ResultPage(
        items=summaries,
        total=raw.total,
        offset=raw.offset,
        returned=len(summaries),
        next_cursor=cursor,
        diagnostics=diagnostics,
    )


def resolve_item_key(reference: str) -> str:
    """Turn whatever the caller called an item into a Zotero key.

    Accepts a key directly, a ``zotero://`` link, or a DOI, since a model that
    has just seen a DOI in a search result will reach for it. Anything else is
    rejected with a pointer to search, which is the tool that produces keys.
    """
    text = (reference or "").strip()
    if not text:
        raise InvalidInput("No item was named.", hint="Pass an 8-character Zotero item key.")

    if is_zotero_key(text):
        return text.upper()

    if text.startswith("zotero://"):
        tail = text.rstrip("/").rsplit("/", 1)[-1]
        if is_zotero_key(tail):
            return tail.upper()

    parsed = parse_identifier(text)
    if parsed.kind is IdentifierKind.DOI:
        found = find_by_doi(parsed.value)
        if found:
            return found
        raise NotFound(
            f"No item in this library has DOI {parsed.value}.",
            hint="Use zotero_search to find it, or zotero_add_item to import it.",
        )

    raise InvalidInput(
        f"{reference!r} is not a Zotero item key.",
        hint="Keys are 8 characters, like 'ABCD2345'. Use zotero_search to find one.",
    )


def find_by_doi(doi: str) -> str | None:
    """First item in the library carrying *doi*, or None."""
    page = backend().get_items(ItemQuery(query=doi, qmode="everything", limit=5, item_type=None))
    for item in page.items:
        data = item.get("data") or {}
        if (data.get("DOI") or "").lower() == doi.lower():
            return item.get("key")
    return None


def require_item(key: str) -> dict[str, Any]:
    """Fetch an item, or fail with a message that says what to do next."""
    item = backend().get_item(key)
    if item is None:
        raise NotFound(
            f"No item with key {key} in this library.",
            hint="Keys are case-sensitive. Use zotero_search to find the right one.",
        )
    return item


def item_versions(keys: list[str]) -> dict[str, int]:
    """Current versions for *keys*, for optimistic locking on a batch write."""
    versions: dict[str, int] = {}
    for key in keys:
        item = backend().get_item(key)
        if item is None:
            raise NotFound(f"No item with key {key} in this library.")
        versions[key] = item.get("version") or (item.get("data") or {}).get("version") or 0
    return versions


def summarise(item: dict[str, Any]) -> ItemSummary:
    active = get_runtime()
    return to_item_summary(
        item,
        abstract_chars=active.limits.abstract_preview_chars,
        library=active.backend.library_ref(),
    )


def outcome_to_result(
    outcome: WriteOutcome,
    *,
    action: str,
    dry_run: bool = False,
    changes: list[Change] | None = None,
    message: str | None = None,
    created_key: str | None = None,
) -> WriteResult:
    """Adapt a backend write outcome into the tool-facing result shape."""
    version = next(iter(outcome.succeeded.values()), None) if outcome.succeeded else None
    return WriteResult(
        action=action,
        dry_run=dry_run,
        succeeded=sorted(outcome.succeeded),
        failed=dict(outcome.failed),
        unchanged=list(outcome.unchanged),
        changes=changes or [],
        version=version,
        created_key=created_key or (sorted(outcome.succeeded)[0] if outcome.succeeded else None),
        message=message,
    )


def guard_destructive(dry_run: bool, *, action: str, affected: int) -> None:
    """Refuse a large destructive write that was not explicitly confirmed.

    ``dry_run`` already defaults to True on every destructive tool. This is the
    second gate: when the configuration asks for confirmation, an unusually
    large blast radius has to be narrowed or re-issued deliberately, because
    the difference between deleting three items and three thousand is one
    mistyped filter.
    """
    active = get_runtime()
    if dry_run or not active.config.surface.confirm_destructive:
        return
    if affected > 100:
        raise InvalidInput(
            f"Refusing to {action} {affected} items in one call.",
            hint=(
                "That is unusually large and is more often a wrong filter than an "
                "intent. Narrow the selection, or set confirm_destructive=false in "
                "the config file if you really mean it."
            ),
        )


def normalise_list(value: Any) -> list[str]:
    """Coerce the several shapes a model uses for a list of strings.

    Models pass lists, comma-separated strings, JSON-encoded arrays and bare
    single values interchangeably. Accepting all of them is cheaper than
    failing and hoping the retry is better formed.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            import json

            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except ValueError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, list | tuple | set):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def tool(func: Callable[..., T]) -> Callable[..., T]:
    """Wrap a tool body with error translation.

    Applied under ``@mcp.tool`` so the registered callable is the wrapped one
    and every failure leaves as a ``ToolError`` with a code.
    """
    from zotero_mcp.errors import tool_errors

    return functools.wraps(func)(tool_errors(func))


__all__ = [
    "backend",
    "build_result_page",
    "find_by_doi",
    "guard_destructive",
    "item_versions",
    "limits",
    "normalise_list",
    "offset_from",
    "outcome_to_result",
    "page_size",
    "require_item",
    "resolve_item_key",
    "respond",
    "runtime",
    "summarise",
    "tool",
]
