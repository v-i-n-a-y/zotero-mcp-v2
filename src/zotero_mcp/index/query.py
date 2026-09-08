# Copyright 2026 Vinay

"""Reading the semantic index, if one exists.

The index is *built* by the CLI and only *read* here. That split is
deliberate: indexing pulls in ChromaDB, sentence-transformers and a model
download, and none of that belongs in the import path of a server whose job is
to answer questions about a library. A missing or broken index degrades
searching to a clear message instead of taking the server down.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def available() -> bool:
    """Whether a usable index exists for the active library."""
    try:
        return _store() is not None
    except Exception:  # noqa: BLE001 (an unusable index is simply unavailable)
        return False


def _store() -> Any | None:
    from zotero_mcp.index.store import open_store
    from zotero_mcp.runtime import get_runtime

    config = get_runtime().config
    if not config.semantic.enabled:
        return None
    return open_store(config, create=False)


def search(
    query: str, *, limit: int = 10, collection_key: str | None = None
) -> list[dict[str, Any]] | None:
    """Semantically similar items as raw Zotero payloads, or None with no index.

    None and an empty list mean different things to the caller: "there is no
    index to ask" versus "the index has nothing like this". Collapsing them
    would make a missing index look like a genuinely empty library.
    """
    try:
        store = _store()
    except Exception as exc:  # noqa: BLE001 (report absence, never fail the search)
        logger.debug("Semantic index unavailable: %s", exc)
        return None
    if store is None:
        return None

    try:
        hits = store.query(query, limit=limit, collection_key=collection_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Semantic query failed: %s", exc)
        return None

    from zotero_mcp.runtime import get_runtime

    backend = get_runtime().backend
    results: list[dict[str, Any]] = []
    for hit in hits:
        item = backend.get_item(hit.item_key)
        if item is None:
            # The index outlived the item. Skipping is right; reporting a key
            # that no longer resolves is worse than a shorter result list.
            continue
        item = dict(item)
        item["_score"] = hit.score
        item["_matched_text"] = hit.text
        results.append(item)
    return results


def status() -> dict[str, Any]:
    """A description of the index for health output."""
    try:
        store = _store()
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "detail": str(exc), "count": None}
    if store is None:
        return {"state": "unavailable", "detail": "no index built", "count": None}
    try:
        return {"state": "ready", "detail": store.describe(), "count": store.count()}
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "detail": str(exc), "count": None}


__all__ = ["available", "search", "status"]
