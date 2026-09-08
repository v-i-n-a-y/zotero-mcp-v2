"""Semantic search: a ChromaDB vector index over the library.

Design choices that set this apart from a naive port:

* **Chunk-level, item-aggregated.** Each item becomes a metadata chunk plus,
  when available, fulltext chunks. A query retrieves chunks, and the index
  collapses them to the best-scoring chunk per item — so a hit points at the
  exact passage that matched, not just "this document is vaguely relevant".
* **Incremental by version.** Every chunk records its item's Zotero version.
  A rebuild re-embeds only items whose version changed, adds new ones, and
  drops items deleted from the library. A second build over an unchanged
  library does almost no work.
* **Self-contained retrieval.** Enough summary detail (title, creators, year)
  rides on each chunk's metadata that a search renders a full result list
  without a single live Zotero call — the index is useful even offline.
* **Fingerprinted.** The embedding provider and model are stored on the
  collection; changing either resets the store rather than silently mixing
  incompatible vectors.
"""

from __future__ import annotations

import logging
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zotero_mcp import content
from zotero_mcp.config import DEFAULT_CONFIG_PATH, ZoteroConfig
from zotero_mcp.errors import Unsupported
from zotero_mcp.mapping import creator_summary, creators_of, year_of

logger = logging.getLogger(__name__)

_COLLECTION = "zotero_library"
_CHILD_TYPES = {"attachment", "note", "annotation"}
_DEFAULT_LOCAL_MODEL = "all-MiniLM-L6-v2"
_PAGE = 2_000  # rows per store read; keeps well under SQLite's variable limit


@dataclass
class Hit:
    """One search result: an item, its similarity, and the passage that matched."""

    item_key: str
    score: float
    matched_text: str
    metadata: dict[str, Any]


@dataclass
class BuildStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    chunks: int = 0
    fulltext_items: int = 0


def semantic_available() -> bool:
    """True when the optional embedding/vector dependencies are importable."""
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


class SemanticIndex:
    """A persistent vector index. Construct cheaply; the store opens lazily."""

    def __init__(self, config: ZoteroConfig) -> None:
        self.config = config
        self.settings = config.semantic
        self.limits = config.limits
        self._collection: Any = None

    # -- wiring -------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return Path(self.settings.db_path or (DEFAULT_CONFIG_PATH.parent / "chroma"))

    @property
    def fingerprint(self) -> str:
        return f"{self.settings.embedding_provider}:{self._model_name()}"

    def _model_name(self) -> str:
        return self.settings.embedding_model or (
            _DEFAULT_LOCAL_MODEL if self.settings.embedding_provider == "default" else "?"
        )

    def _require(self) -> None:
        if not semantic_available():
            raise Unsupported(
                "Semantic search needs the optional dependencies.",
                hint="Reinstall with the 'semantic' extra: uv tool install --force '.[semantic]'.",
            )

    def _embedding_function(self) -> Any:
        from chromadb.utils import embedding_functions

        provider = self.settings.embedding_provider
        if provider in ("default", "sentence-transformers", "local"):
            return embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self._model_name()
            )
        if provider == "openai":
            return embedding_functions.OpenAIEmbeddingFunction(
                model_name=self.settings.embedding_model or "text-embedding-3-small"
            )
        if provider == "gemini":
            return embedding_functions.GoogleGenerativeAiEmbeddingFunction(
                model_name=self.settings.embedding_model or "models/text-embedding-004"
            )
        raise Unsupported(f"Unknown embedding provider {provider!r}.")

    @property
    def collection(self) -> Any:
        if self._collection is None:
            self._require()
            import chromadb

            self.db_path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.db_path))
            col = client.get_or_create_collection(
                name=_COLLECTION,
                embedding_function=self._embedding_function(),
                metadata={"fingerprint": self.fingerprint, "hnsw:space": "cosine"},
            )
            stored = (col.metadata or {}).get("fingerprint")
            if stored and stored != self.fingerprint:
                logger.warning(
                    "Embedding model changed (%s -> %s); resetting index.",
                    stored,
                    self.fingerprint,
                )
                client.delete_collection(_COLLECTION)
                col = client.create_collection(
                    name=_COLLECTION,
                    embedding_function=self._embedding_function(),
                    metadata={"fingerprint": self.fingerprint, "hnsw:space": "cosine"},
                )
            self._collection = col
        return self._collection

    def warm_up(self) -> None:
        """Open the store and load the embedding model on a background thread.

        Cold start (model weights plus HNSW load) can take tens of seconds; doing
        it at server start means the first query does not pay for it.
        """

        def _load() -> None:
            if not semantic_available():  # heavy imports happen here, off the main thread
                return
            with suppress(Exception):  # failures resurface on the first real call
                self.collection.count()

        # Short delay so the MCP handshake completes before model loading
        # (which holds the GIL for a few seconds) begins.
        timer = threading.Timer(2.0, _load)
        timer.daemon = True
        timer.name = "semantic-warm-up"
        timer.start()

    # -- status -------------------------------------------------------------

    def status(self) -> tuple[str, int]:
        """Return ``(state, item_count)`` for health reporting.

        Never raises: a missing dependency or unopenable store is reported as a
        state string, because health must answer even when search cannot.
        """
        if not semantic_available():
            return ("unavailable", 0)
        try:
            count = len(self._indexed_versions())
            return ("empty" if not count else "ready", count)
        except Exception as exc:  # noqa: BLE001 - health degrades gracefully
            logger.warning("Semantic index unreadable: %s", exc)
            return ("error", 0)

    def _indexed_versions(self) -> dict[str, int]:
        """Map of item key -> indexed Zotero version, read from metadata chunks.

        Every item has exactly one ``kind="metadata"`` chunk, so filtering on it
        gives one row per item. Reads are paged: a single ``get()`` over a large
        store overflows SQLite's bound-variable limit.
        """
        col = self.collection
        versions: dict[str, int] = {}
        offset = 0
        while True:
            page = col.get(
                where={"kind": "metadata"},
                include=["metadatas"],
                limit=_PAGE,
                offset=offset,
            )
            metas = page["metadatas"] or []
            for meta in metas:
                versions[meta["item_key"]] = int(meta.get("item_version", -1))
            if len(metas) < _PAGE:
                return versions
            offset += _PAGE

    def clear(self) -> None:
        self._require()
        import chromadb

        client = chromadb.PersistentClient(path=str(self.db_path))
        with suppress(Exception):  # already absent is fine
            client.delete_collection(_COLLECTION)
        self._collection = None

    # -- build --------------------------------------------------------------

    def build(self, backend: Any, *, progress: Any = None) -> BuildStats:
        """Bring the index in step with the library, re-embedding only changes."""
        self._require()
        col = self.collection
        stats = BuildStats()

        indexed_version = self._indexed_versions()

        raw = backend.all_items()
        tops: dict[str, dict[str, Any]] = {}
        children: dict[str, list[dict[str, Any]]] = {}
        for item in raw:
            data = item.get("data", {})
            key = data.get("key")
            if not key:
                continue
            item_type = data.get("itemType")
            parent = data.get("parentItem")
            if item_type in _CHILD_TYPES and parent:
                children.setdefault(parent, []).append(item)
            elif item_type not in _CHILD_TYPES and not data.get("deleted"):
                tops[key] = item

        live_keys = set(tops)

        for key, item in tops.items():
            data = item["data"]
            version = int(item.get("version") or data.get("version") or 0)
            if indexed_version.get(key) == version:
                stats.skipped += 1
                continue
            is_update = key in indexed_version
            col.delete(where={"item_key": key})

            docs, metas, ids, had_fulltext = self._chunks_for(
                key, data, children.get(key, []), backend
            )
            if docs:
                col.add(ids=ids, documents=docs, metadatas=metas)
                stats.chunks += len(docs)
            if had_fulltext:
                stats.fulltext_items += 1
            stats.updated += int(is_update)
            stats.added += int(not is_update)
            if progress:
                progress(key, stats)

        stale = set(indexed_version) - live_keys
        for key in stale:
            col.delete(where={"item_key": key})
            stats.removed += 1

        return stats

    def _chunks_for(
        self, key: str, data: dict[str, Any], child_items: list[dict[str, Any]], backend: Any
    ) -> tuple[list[str], list[dict[str, Any]], list[str], bool]:
        summary_meta = self._summary_meta(key, data)
        version = int(data.get("version") or 0)

        docs: list[str] = []
        metas: list[dict[str, Any]] = []

        # Always present, even for a bare item: it is the row that carries the
        # item's version for incremental rebuilds.
        meta_doc = content.metadata_document(data) or summary_meta["title"]
        docs.append(meta_doc)
        metas.append({**summary_meta, "item_version": version, "kind": "metadata"})

        had_fulltext = False
        if self.settings.index_fulltext:
            fulltext = self._gather_fulltext(child_items, backend)
            if fulltext:
                had_fulltext = True
                for chunk in content.chunk_text(
                    fulltext,
                    size=self.settings.chunk_chars,
                    overlap=self.settings.chunk_overlap_chars,
                ):
                    docs.append(chunk)
                    metas.append({**summary_meta, "item_version": version, "kind": "fulltext"})

        ids = [f"{key}#{i}" for i in range(len(docs))]
        return docs, metas, ids, had_fulltext

    def _gather_fulltext(self, child_items: list[dict[str, Any]], backend: Any) -> str:
        parts: list[str] = []
        for child in child_items:
            cdata = child.get("data", {})
            if cdata.get("itemType") != "attachment":
                continue
            ckey = cdata.get("key")
            if not ckey:
                continue
            text = backend.fulltext(ckey)  # Zotero's own index first
            if not text:
                text = self._extract_child(ckey, cdata, backend)
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()

    def _extract_child(self, ckey: str, cdata: dict[str, Any], backend: Any) -> str:
        content_type = cdata.get("contentType")
        if content_type not in ("application/pdf", "application/epub+zip") and not (
            (cdata.get("filename") or "").lower().endswith((".pdf", ".epub"))
        ):
            return ""
        try:
            blob = backend.file_bytes(ckey)
        except Exception as exc:  # noqa: BLE001 - a missing file is not fatal
            logger.info("No file bytes for attachment %s: %s", ckey, exc)
            return ""
        return content.extract_text(blob, content_type, cdata.get("filename"))

    @staticmethod
    def _summary_meta(key: str, data: dict[str, Any]) -> dict[str, Any]:
        creators = creators_of(data)
        return {
            "item_key": key,
            "title": str(data.get("title") or "Untitled"),
            "item_type": str(data.get("itemType") or "document"),
            "year": year_of(data.get("date")) or "",
            "creator_summary": creator_summary(creators) or "",
            "publication": str(
                data.get("publicationTitle") or data.get("bookTitle") or data.get("publisher") or ""
            ),
        }

    # -- search -------------------------------------------------------------

    def search(self, query: str, *, limit: int) -> list[Hit]:
        """Return up to *limit* items, best-matching passage per item."""
        self._require()
        if not query.strip():
            return []
        col = self.collection
        if col.count() == 0:
            return []

        # Over-fetch chunks so that collapsing to distinct items still fills the
        # page: a long, on-topic paper can own dozens of the top chunks.
        n = max(limit * 25, 100)
        res = col.query(
            query_texts=[query],
            n_results=min(n, col.count()),
            include=["documents", "metadatas", "distances"],
        )
        metadatas = res["metadatas"][0]
        documents = res["documents"][0]
        distances = res["distances"][0]

        best: dict[str, Hit] = {}
        for meta, doc, dist in zip(metadatas, documents, distances, strict=False):
            key = meta["item_key"]
            score = 1.0 - float(dist)  # cosine distance -> similarity
            if key not in best or score > best[key].score:
                best[key] = Hit(item_key=key, score=score, matched_text=doc, metadata=meta)

        ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
        return ranked[:limit]


__all__ = ["BuildStats", "Hit", "SemanticIndex", "semantic_available"]
