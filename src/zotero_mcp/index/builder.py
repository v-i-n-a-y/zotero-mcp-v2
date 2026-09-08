# Copyright 2026 Vinay

"""Building the semantic index.

Runs from the CLI, and incrementally from ``zotero_index(action='update')``.
Two properties matter more than speed:

* **Idempotence.** Chunk ids are derived from item key and ordinal, so a
  rerun overwrites rather than duplicating, and an interrupted build resumes.
* **Not needing the whole library in memory.** Items are paged, chunked and
  written as they arrive, so a sixteen-thousand-item library indexes in bounded
  memory.
"""

from __future__ import annotations

import logging
from typing import Any

from zotero_mcp.errors import Unsupported
from zotero_mcp.mapping import strip_html, title_of
from zotero_mcp.runtime import Runtime

logger = logging.getLogger(__name__)

#: Written per embedding call. Small enough to stay under every provider's
#: request limit, large enough that a local model is not called per document.
_BATCH = 32


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split *text* into overlapping windows, preferring paragraph boundaries.

    Overlap exists so a passage spanning a boundary is still findable; without
    it, the sentence that answers the question is exactly the one cut in half.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start + size // 2, end)
            if boundary == -1:
                boundary = text.rfind(". ", start + size // 2, end)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def document_for(item: dict[str, Any]) -> str:
    """The text that represents an item to the index.

    Metadata first, then the abstract. Titles and author names carry most of
    the retrieval signal for a library search, and putting them at the front of
    every chunk keeps them in the window even when a chunk is truncated.
    """
    data = item.get("data") or {}
    # Through title_of, not data["title"]: a case stores its title under
    # caseName, and indexing it as untitled makes it unfindable by name. Its
    # "Untitled" placeholder is dropped, since embedding that word would put
    # every untitled item next to every other one.
    title = title_of(data, data.get("itemType", "document"))
    parts = [] if title.startswith("Untitled") else [title]

    creators = [
        creator.get("lastName") or creator.get("name") or ""
        for creator in data.get("creators") or []
    ]
    if names := [n for n in creators if n]:
        parts.append(", ".join(names))

    for field in ("date", "publicationTitle", "proceedingsTitle", "publisher"):
        if value := data.get(field):
            parts.append(str(value))

    if abstract := data.get("abstractNote"):
        parts.append(strip_html(abstract))

    if tags := [t.get("tag") for t in data.get("tags") or [] if isinstance(t, dict)]:
        parts.append("Tags: " + ", ".join(t for t in tags if t))

    return "\n".join(part for part in parts if part).strip()


def update_index(
    runtime: Runtime,
    *,
    limit: int | None = None,
    rebuild: bool = False,
    include_fulltext: bool | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Index items that are not indexed yet.

    Args:
        runtime: The active runtime, for its backend and configuration.
        limit: Most items to process. None means the whole library.
        rebuild: Reindex everything, not just what is missing.
        include_fulltext: Index attachment text as well as metadata. Defaults
            to the configured value.
        progress: Optional callable taking (done, total) for a CLI progress bar.
    """
    from zotero_mcp.backends.base import ItemQuery
    from zotero_mcp.index.store import open_store

    config = runtime.config
    if not config.semantic.enabled:
        raise Unsupported(
            "Semantic search is disabled in the configuration.",
            hint="Set semantic.enabled to true, or ZOTERO_MCP_SEMANTIC=1.",
        )

    store = open_store(config, create=True)
    if store is None:  # pragma: no cover - open_store raises when create=True
        raise Unsupported("Could not open the semantic index.")

    already = set() if rebuild else store.indexed_keys()
    want_fulltext = config.semantic.index_fulltext if include_fulltext is None else include_fulltext

    stats = {"indexed": 0, "chunks": 0, "skipped": 0, "errors": []}
    pending: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None

    while limit is None or stats["indexed"] < limit:
        batch_size = min(100, (limit - stats["indexed"]) if limit else 100)
        raw = runtime.backend.get_items(
            ItemQuery(item_type="-attachment", offset=offset, limit=batch_size)
        )
        if not raw.items:
            break
        if total is None:
            total = raw.total
        offset += len(raw.items)

        for item in raw.items:
            key = item.get("key")
            data = item.get("data") or {}
            if not key or data.get("itemType") in {"note", "attachment", "annotation"}:
                continue
            if key in already:
                stats["skipped"] += 1
                continue

            text = document_for(item)
            if want_fulltext:
                text = _append_fulltext(runtime, key, text)
            if not text:
                stats["skipped"] += 1
                continue

            chunks = chunk_text(
                text,
                size=config.semantic.chunk_chars,
                overlap=config.semantic.chunk_overlap_chars,
            )
            for ordinal, chunk in enumerate(chunks):
                pending.append(
                    {
                        "item_key": key,
                        "chunk": ordinal,
                        "text": chunk,
                        "title": data.get("title", ""),
                        "collections": list(data.get("collections") or []),
                    }
                )
            stats["indexed"] += 1

            if len(pending) >= _BATCH:
                stats["chunks"] += _flush(store, pending, stats)
            if progress:
                progress(stats["indexed"] + stats["skipped"], total)
            if limit and stats["indexed"] >= limit:
                break

        if raw.total is not None and offset >= raw.total:
            break

    stats["chunks"] += _flush(store, pending, stats)
    logger.info(
        "Indexed %s items (%s chunks), skipped %s",
        stats["indexed"],
        stats["chunks"],
        stats["skipped"],
    )
    return stats


def _flush(store: Any, pending: list[dict[str, Any]], stats: dict[str, Any]) -> int:
    """Write a batch, recording rather than raising on failure.

    An embedding provider rejecting one batch must not throw away the work
    already done: the cache of what is indexed lives in the store itself, so a
    rerun picks up exactly where this left off.
    """
    if not pending:
        return 0
    try:
        written = store.upsert(list(pending))
    except Exception as exc:  # noqa: BLE001 (recorded, so a rerun can resume)
        logger.warning("Failed to write a batch of %s chunks: %s", len(pending), exc)
        stats["errors"].append(str(exc))
        written = 0
    pending.clear()
    return written


def _append_fulltext(runtime: Runtime, item_key: str, text: str) -> str:
    """Add attachment text to an item's document, if it is cheaply available.

    Only Zotero's own index is consulted. Extracting from files during a
    library-wide build would turn minutes into hours, and the index already
    holds text for anything the desktop client has processed.
    """
    try:
        for child in runtime.backend.get_children(item_key, item_type="attachment"):
            key = child.get("key") or (child.get("data") or {}).get("key")
            if not key:
                continue
            if indexed := runtime.backend.get_fulltext(key):
                return f"{text}\n\n{indexed}"
    except Exception as exc:  # noqa: BLE001 (fulltext is an enrichment, not a requirement)
        logger.debug("No full text for %s: %s", item_key, exc)
    return text


__all__ = ["chunk_text", "document_for", "update_index"]
