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
  incompatible vectors. Extracted fulltext is cached beside the store, so a
  model change re-embeds in minutes instead of re-downloading every PDF.
* **Reranked and filtered.** Candidates come from the vector index, then a
  cross-encoder re-scores the query against each passage (far more precise
  than embedding similarity alone). Bibliography-like passages are penalised,
  items with several matching passages get a small corroboration bonus, and
  results can be restricted by item type, year range, or collection.
"""

from __future__ import annotations

import logging
import math
import re
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
_DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
_PAGE = 2_000  # rows per store read; keeps well under SQLite's variable limit
_RERANK_POOL = 60  # chunks re-scored by the cross-encoder per query
_CORROBORATION_BONUS = 0.10  # max lift for an item with several matching passages
_CITATION_PENALTY = 0.6  # score multiplier at full citation density


@dataclass
class Hit:
    """One search result: an item, its similarity, and the passage that matched."""

    item_key: str
    score: float
    matched_text: str
    metadata: dict[str, Any]
    #: "metadata" (title/abstract matched) or "fulltext" (a passage matched).
    kind: str = "metadata"
    #: How many of this item's passages were in the candidate pool.
    evidence: int = 1


@dataclass
class BuildStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    chunks: int = 0
    fulltext_items: int = 0
    fulltext_cache_hits: int = 0


def semantic_available() -> bool:
    """True when the optional embedding/vector dependencies are importable."""
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "which",
        "with",
    ]
)
_DISPLAY_TOLERANCE = (
    0.15  # a term-bearing passage may trail the best by this much and still be shown
)


def _query_terms(query: str) -> list[str]:
    """Content words of *query*, lowercased, crude-stemmed to their first 5+ letters."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    terms = [w[:6] if len(w) > 6 else w for w in words if len(w) >= 3 and w not in _STOPWORDS]
    return list(dict.fromkeys(terms))


def _term_hits(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(1 for t in terms if t in low)


def _display_passage(
    passages: list[tuple[float, str, dict[str, Any]]], terms: list[str]
) -> tuple[float, str, dict[str, Any]]:
    """Pick the passage to show for an item.

    The top-scoring chunk is usually right, but for a long paper with dozens of
    on-topic chunks the reranker's favourite can be an oblique one. Among the
    chunks nearly as good as the best, prefer the one that literally mentions
    the most query terms; a reader can see at once why it matched.
    """
    best_score = passages[0][0]
    close = [p for p in passages if p[0] >= best_score - _DISPLAY_TOLERANCE]
    return max(close, key=lambda p: (_term_hits(p[1], terms), p[0]))


def _focus(doc: str, terms: list[str]) -> str:
    """Start the passage at the sentence that mentions the most query terms.

    Callers truncate passages for display, so text before the relevant
    sentence would otherwise push the actual match out of view.
    """
    if not terms:
        return doc
    pieces = re.split(r"(?<=[.!?])\s+|\n+", doc)
    best_i, best_hits = 0, 0
    for i, piece in enumerate(pieces):
        hits = _term_hits(piece, terms)
        if hits > best_hits:
            best_i, best_hits = i, hits
    if best_hits == 0 or best_i == 0:
        return doc
    # Keep one sentence of lead-in for context.
    return " ".join(pieces[best_i - 1 :]).strip()


class SemanticIndex:
    """A persistent vector index. Construct cheaply; the store opens lazily."""

    def __init__(self, config: ZoteroConfig) -> None:
        self.config = config
        self.settings = config.semantic
        self.limits = config.limits
        self._collection: Any = None
        self._reranker: Any = None

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

    def _cross_encoder(self) -> Any:
        """The reranking model, loaded once; ``None`` when reranking is off."""
        if not self.settings.rerank:
            return None
        if self._reranker is None:
            from sentence_transformers import CrossEncoder

            self._reranker = CrossEncoder(self.settings.rerank_model)
        return self._reranker

    @property
    def fulltext_cache_dir(self) -> Path:
        return self.db_path / "fulltext"

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
                self._cross_encoder()

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
                key, data, children.get(key, []), backend, stats
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
        self,
        key: str,
        data: dict[str, Any],
        child_items: list[dict[str, Any]],
        backend: Any,
        stats: BuildStats,
    ) -> tuple[list[str], list[dict[str, Any]], list[str], bool]:
        summary_meta = self._summary_meta(key, data)
        version = int(data.get("version") or 0)

        docs: list[str] = []
        metas: list[dict[str, Any]] = []

        # Always present, even for a bare item: it is the row that carries the
        # item's version for incremental rebuilds.
        meta_doc = content.metadata_document(data) or summary_meta["title"]
        docs.append(meta_doc)
        metas.append(
            {**summary_meta, "item_version": version, "kind": "metadata", "cite_density": 0.0}
        )

        had_fulltext = False
        if self.settings.index_fulltext:
            fulltext = content.strip_references(self._gather_fulltext(child_items, backend, stats))
            if fulltext:
                had_fulltext = True
                for chunk in content.chunk_text(
                    fulltext,
                    size=self.settings.chunk_chars,
                    overlap=self.settings.chunk_overlap_chars,
                ):
                    docs.append(chunk)
                    metas.append(
                        {
                            **summary_meta,
                            "item_version": version,
                            "kind": "fulltext",
                            "cite_density": round(content.citation_density(chunk), 3),
                        }
                    )

        ids = [f"{key}#{i}" for i in range(len(docs))]
        return docs, metas, ids, had_fulltext

    def _gather_fulltext(
        self, child_items: list[dict[str, Any]], backend: Any, stats: BuildStats
    ) -> str:
        parts: list[str] = []
        for child in child_items:
            cdata = child.get("data", {})
            if cdata.get("itemType") != "attachment":
                continue
            ckey = cdata.get("key")
            if not ckey:
                continue
            cversion = int(child.get("version") or cdata.get("version") or 0)
            cached = self._cached_fulltext(ckey, cversion)
            if cached is not None:
                stats.fulltext_cache_hits += 1
                text = cached
            else:
                text = backend.fulltext(ckey) or ""  # Zotero's own index first
                if not text:
                    text = self._extract_child(ckey, cdata, backend)
                self._store_fulltext(ckey, cversion, text)
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()

    def _cache_file(self, ckey: str, version: int) -> Path:
        return self.fulltext_cache_dir / f"{ckey}-{version}.txt"

    def _cached_fulltext(self, ckey: str, version: int) -> str | None:
        """Extracted text for an attachment at *version*, or ``None`` if unseen.

        An empty file is a real cache entry: "we looked, there was nothing",
        which saves re-downloading a PDF that has no extractable text.
        """
        path = self._cache_file(ckey, version)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.info("Fulltext cache unreadable for %s: %s", ckey, exc)
            return None

    def _store_fulltext(self, ckey: str, version: int, text: str) -> None:
        try:
            self.fulltext_cache_dir.mkdir(parents=True, exist_ok=True)
            for stale in self.fulltext_cache_dir.glob(f"{ckey}-*.txt"):
                stale.unlink(missing_ok=True)
            self._cache_file(ckey, version).write_text(text, encoding="utf-8")
        except OSError as exc:  # the cache is an optimisation, never a requirement
            logger.info("Could not cache fulltext for %s: %s", ckey, exc)

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
        year = year_of(data.get("date")) or ""
        meta: dict[str, Any] = {
            "item_key": key,
            "title": str(data.get("title") or "Untitled"),
            "item_type": str(data.get("itemType") or "document"),
            "year": year,
            # Numeric twin of ``year`` so range filters work; 0 = unknown.
            "year_num": int(year) if year.isdigit() else 0,
            "creator_summary": creator_summary(creators) or "",
            "publication": str(
                data.get("publicationTitle") or data.get("bookTitle") or data.get("publisher") or ""
            ),
        }
        # Chroma metadata is flat scalars, so collection membership becomes
        # one boolean flag per collection: ``{"col_ABCD1234": True}``.
        for ckey in data.get("collections") or []:
            meta[f"col_{ckey}"] = True
        return meta

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int,
        item_type: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        collection_key: str | None = None,
    ) -> list[Hit]:
        """Return up to *limit* items, best-matching passage per item.

        Pipeline: vector recall of a candidate pool (filtered by metadata),
        optional cross-encoder rerank of that pool, citation-density penalty,
        collapse to one hit per item with a corroboration bonus, sort.
        """
        self._require()
        if not query.strip():
            return []
        col = self.collection
        total = col.count()
        if total == 0:
            return []

        where = self._where(item_type, year_from, year_to, collection_key)
        # Over-fetch chunks so that collapsing to distinct items still fills the
        # page: a long, on-topic paper can own dozens of the top chunks.
        n = min(max(limit * 25, 100), total)
        res = col.query(
            query_texts=[query],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        metadatas = res["metadatas"][0]
        documents = res["documents"][0]
        scores = [1.0 - float(d) for d in res["distances"][0]]  # cosine distance -> similarity

        reranker = self._cross_encoder()
        if reranker is not None and documents:
            scores = self._rerank(reranker, query, documents, scores)

        # Group the pool by item, keeping every candidate passage.
        by_item: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}
        for meta, doc, score in zip(metadatas, documents, scores, strict=False):
            score *= 1.0 - _CITATION_PENALTY * float(meta.get("cite_density") or 0.0)
            by_item.setdefault(meta["item_key"], []).append((score, doc, meta))

        terms = _query_terms(query)
        hits: list[Hit] = []
        for key, passages in by_item.items():
            passages.sort(key=lambda t: t[0], reverse=True)
            best_score = passages[0][0]
            # Several matching passages is stronger evidence than one, but never
            # enough to overtake a clearly better single match.
            extra = min(len(passages) - 1, 5) / 5
            score = round(best_score + (1.0 - best_score) * _CORROBORATION_BONUS * extra, 4)
            _, doc, meta = _display_passage(passages, terms)
            hits.append(
                Hit(
                    item_key=key,
                    score=score,
                    matched_text=_focus(doc, terms),
                    metadata=meta,
                    kind=str(meta.get("kind") or "metadata"),
                    evidence=len(passages),
                )
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    @staticmethod
    def _where(
        item_type: str | None,
        year_from: int | None,
        year_to: int | None,
        collection_key: str | None,
    ) -> dict[str, Any] | None:
        clauses: list[dict[str, Any]] = []
        if item_type:
            clauses.append({"item_type": item_type})
        if year_from is not None:
            clauses.append({"year_num": {"$gte": int(year_from)}})
        if year_to is not None:
            clauses.append({"year_num": {"$lte": int(year_to)}})
        if collection_key:
            clauses.append({f"col_{collection_key}": True})
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    @staticmethod
    def _rerank(
        reranker: Any, query: str, documents: list[str], scores: list[float]
    ) -> list[float]:
        """Re-score the top of the pool with the cross-encoder.

        Cross-encoder logits are squashed to ``(0, 1)`` so they sit on the same
        scale as cosine similarity; chunks outside the reranked pool keep their
        vector score, which is always below the reranked ones in practice.
        """
        pool = min(_RERANK_POOL, len(documents))
        logits = reranker.predict([(query, doc) for doc in documents[:pool]])
        reranked = [1.0 / (1.0 + math.exp(-float(x))) for x in logits]
        return reranked + scores[pool:]


__all__ = ["BuildStats", "Hit", "SemanticIndex", "semantic_available"]
