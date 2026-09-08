# Copyright 2026 Vinay

"""The vector store behind semantic search.

ChromaDB, wrapped thinly. The wrapper exists for three reasons the
predecessors learned the hard way:

* an index built with one embedding model is meaningless when queried with
  another, so the model signature is stored as collection metadata and a
  mismatch resets the collection rather than returning nonsense;
* the whole of ChromaDB is imported lazily, so a base install never pays for
  it and a broken install degrades to "no semantic search" rather than a
  server that will not start;
* chunk identity is deterministic (item key plus chunk ordinal), so rebuilding
  is idempotent and an interrupted build resumes rather than duplicating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zotero_mcp.config import ZoteroConfig
from zotero_mcp.errors import Unsupported
from zotero_mcp.index.embeddings import Embedder, build_embedder

logger = logging.getLogger(__name__)

COLLECTION_NAME = "zotero_items"
SEMANTIC_EXTRA_HINT = "Install the semantic extra: pip install 'zotero-mcp-next[semantic]'"


@dataclass(frozen=True)
class Hit:
    """One semantic match."""

    item_key: str
    score: float
    text: str
    chunk: int = 0


def default_db_path(config: ZoteroConfig) -> Path:
    if config.semantic.db_path:
        return Path(config.semantic.db_path).expanduser()
    return Path.home() / ".config" / "zotero-mcp" / "index"


class IndexStore:
    """Read and write access to one library's vector index."""

    def __init__(self, path: Path, embedder: Embedder, collection: Any) -> None:
        self.path = path
        self.embedder = embedder
        self._collection = collection

    # -- reading -----------------------------------------------------------
    def query(self, text: str, *, limit: int = 10, collection_key: str | None = None) -> list[Hit]:
        """Nearest chunks to *text*, best first, at most one per item."""
        where = {"collections": {"$in": [collection_key]}} if collection_key else None
        # Over-fetch, because several chunks of one paper often crowd the top
        # and the caller wants distinct items, not distinct paragraphs.
        response = self._collection.query(
            query_embeddings=self.embedder.embed([text]),
            n_results=min(limit * 4, 100),
            where=where,
        )

        documents = (response.get("documents") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]

        best: dict[str, Hit] = {}
        for document, metadata, distance in zip(documents, metadatas, distances, strict=False):
            key = (metadata or {}).get("item_key")
            if not key:
                continue
            # Chroma returns a distance; a caller reads a similarity.
            score = round(max(0.0, 1.0 - float(distance)), 4)
            existing = best.get(key)
            if existing is None or score > existing.score:
                best[key] = Hit(
                    item_key=key,
                    score=score,
                    text=(document or "")[:400],
                    chunk=int((metadata or {}).get("chunk", 0)),
                )
        return sorted(best.values(), key=lambda hit: -hit.score)[:limit]

    def count(self) -> int:
        return int(self._collection.count())

    def describe(self) -> str:
        return f"{self.embedder.signature} at {self.path}"

    # -- writing -----------------------------------------------------------
    def upsert(self, records: list[dict[str, Any]]) -> int:
        """Add or replace chunks.

        Each record needs ``item_key``, ``chunk``, ``text`` and optional
        ``collections``. Ids are derived from key and ordinal, so re-running a
        build overwrites rather than duplicating.
        """
        if not records:
            return 0
        ids = [f"{r['item_key']}#{r['chunk']}" for r in records]
        documents = [r["text"] for r in records]
        metadatas = [
            {
                "item_key": r["item_key"],
                "chunk": int(r["chunk"]),
                "title": (r.get("title") or "")[:200],
                "collections": r.get("collections") or [],
            }
            for r in records
        ]
        self._collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=self.embedder.embed(documents),
        )
        return len(records)

    def delete_items(self, item_keys: list[str]) -> None:
        if item_keys:
            self._collection.delete(where={"item_key": {"$in": item_keys}})

    def indexed_keys(self) -> set[str]:
        """Item keys currently present, for incremental rebuilds."""
        result = self._collection.get(include=["metadatas"])
        return {
            (metadata or {}).get("item_key")
            for metadata in (result.get("metadatas") or [])
            if (metadata or {}).get("item_key")
        }


def open_store(config: ZoteroConfig, *, create: bool = False) -> IndexStore | None:
    """Open the index, or return None when there is nothing to open.

    Returns None rather than raising for the ordinary "not built yet" case, so
    a search can fall back quietly. Raises only when the caller explicitly
    asked to create one and cannot.
    """
    path = default_db_path(config)
    if not create and not path.exists():
        return None

    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        if create:
            raise Unsupported("Semantic search needs ChromaDB.", hint=SEMANTIC_EXTRA_HINT) from exc
        return None

    embedder = build_embedder(config.semantic.embedding_provider, config.semantic.embedding_model)
    path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(path), settings=Settings(anonymized_telemetry=False)
    )

    metadata = {"embedder": embedder.signature}
    try:
        collection = client.get_collection(COLLECTION_NAME)
        stored = (collection.metadata or {}).get("embedder")
        if stored and stored != embedder.signature:
            # Vectors from two models share no space. Querying across them
            # returns confident nonsense, which is worse than no answer.
            logger.warning(
                "Embedding model changed (%s -> %s); resetting the index.",
                stored,
                embedder.signature,
            )
            client.delete_collection(COLLECTION_NAME)
            collection = client.create_collection(COLLECTION_NAME, metadata=metadata)
    except Exception:  # noqa: BLE001 (chroma raises several types for "absent")
        if not create:
            return None
        collection = client.get_or_create_collection(COLLECTION_NAME, metadata=metadata)

    return IndexStore(path, embedder, collection)


__all__ = ["COLLECTION_NAME", "Hit", "IndexStore", "default_db_path", "open_store"]
