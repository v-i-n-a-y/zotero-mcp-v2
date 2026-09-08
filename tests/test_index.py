"""Semantic index: incremental build, chunk collapsing, and status reporting.

Runs against a real on-disk ChromaDB in a temp dir, but swaps the embedding
function for a cheap deterministic one so no model download is needed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace

import pytest

pytest.importorskip("chromadb")

from zotero_mcp.config import ZoteroConfig
from zotero_mcp.index import SemanticIndex


class _HashEmbedding:
    """Bag-of-words hashed into a fixed vector; similar texts get similar vectors."""

    dim = 64

    def __call__(self, input: list[str]) -> list[list[float]]:
        out = []
        for text in input:
            vec = [0.0] * self.dim
            for word in text.lower().split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def name(self) -> str:
        return "hash"

    @staticmethod
    def build_from_config(config):  # pragma: no cover - chroma interface
        return _HashEmbedding()

    def get_config(self):  # pragma: no cover - chroma interface
        return {}

    def embed_query(self, input):
        return self(input)


def _item(key: str, title: str, version: int = 1, **fields):
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "version": version,
            **fields,
        },
    }


def _attachment(key: str, parent: str):
    return {
        "key": key,
        "version": 1,
        "data": {
            "key": key,
            "itemType": "attachment",
            "parentItem": parent,
            "contentType": "application/pdf",
            "filename": "x.pdf",
        },
    }


class FakeBackend:
    def __init__(self, items, fulltext=None):
        self.items = items
        self._fulltext = fulltext or {}
        self.file_calls = 0

    def all_items(self):
        return self.items

    def fulltext(self, key):
        return self._fulltext.get(key, "")

    def file_bytes(self, key):
        self.file_calls += 1
        raise RuntimeError("no file")


def _config(tmp_path, **overrides):
    base = ZoteroConfig()
    semantic = replace(
        base.semantic,
        db_path=str(tmp_path / "chroma"),
        chunk_chars=80,
        chunk_overlap_chars=10,
        rerank=False,  # no model download in tests; the rerank path is unit-tested below
        **overrides,
    )
    return replace(base, semantic=semantic)


@pytest.fixture
def index(tmp_path, monkeypatch):
    idx = SemanticIndex(_config(tmp_path))
    monkeypatch.setattr(idx, "_embedding_function", lambda: _HashEmbedding())
    return idx


def test_empty_index_status(index):
    assert index.status() == ("empty", 0)


def test_build_adds_items_and_fulltext_chunks(index):
    long_text = " ".join(["combustion instability in liquid rocket engines"] * 10)
    backend = FakeBackend(
        [_item("A1", "Rocket nozzles"), _item("B2", "Bird migration"), _attachment("A1F", "A1")],
        fulltext={"A1F": long_text},
    )
    stats = index.build(backend)
    assert (stats.added, stats.updated, stats.skipped, stats.removed) == (2, 0, 0, 0)
    assert stats.fulltext_items == 1
    assert stats.chunks > 3  # 2 metadata + several fulltext chunks
    assert index.status() == ("ready", 2)


def test_rebuild_skips_unchanged_reembeds_changed_removes_deleted(index):
    backend = FakeBackend([_item("A1", "Rocket nozzles"), _item("B2", "Bird migration")])
    index.build(backend)

    backend.items = [_item("A1", "Rocket nozzles revised", version=2), _item("C3", "New paper")]
    stats = index.build(backend)
    assert (stats.added, stats.updated, stats.skipped, stats.removed) == (1, 1, 0, 1)

    stats = index.build(backend)
    assert (stats.added, stats.updated, stats.skipped, stats.removed) == (0, 0, 2, 0)
    assert index.status() == ("ready", 2)


def test_trashed_items_are_treated_as_absent(index):
    backend = FakeBackend([_item("A1", "Rocket nozzles")])
    index.build(backend)
    backend.items = [_item("A1", "Rocket nozzles", deleted=1)]
    stats = index.build(backend)
    assert stats.removed == 1
    assert index.status() == ("empty", 0)


def test_search_collapses_to_best_chunk_per_item(index):
    text = "combustion instability in liquid rocket engines. " * 8
    backend = FakeBackend(
        [_item("A1", "Rocket nozzles"), _item("B2", "Bird migration"), _attachment("A1F", "A1")],
        fulltext={"A1F": text},
    )
    index.build(backend)
    hits = index.search("combustion instability rocket", limit=10)
    keys = [h.item_key for h in hits]
    assert keys[0] == "A1"
    assert len(keys) == len(set(keys)), "one hit per item"
    assert hits[0].matched_text
    assert hits[0].metadata["title"] == "Rocket nozzles"
    assert hits[0].score >= hits[-1].score


def test_search_blank_query_or_empty_index_returns_nothing(index):
    assert index.search("   ", limit=5) == []
    assert index.search("rockets", limit=5) == []


def test_fulltext_disabled_skips_attachments(tmp_path, monkeypatch):
    index = SemanticIndex(_config(tmp_path, index_fulltext=False))
    monkeypatch.setattr(index, "_embedding_function", lambda: _HashEmbedding())
    backend = FakeBackend(
        [_item("A1", "Rocket"), _attachment("A1F", "A1")], fulltext={"A1F": "x" * 500}
    )
    stats = index.build(backend)
    assert stats.fulltext_items == 0
    assert stats.chunks == 1


def test_extract_failure_is_not_fatal(index):
    backend = FakeBackend([_item("A1", "Rocket"), _attachment("A1F", "A1")])
    stats = index.build(backend)
    assert backend.file_calls == 1
    assert stats.added == 1 and stats.fulltext_items == 0


def test_clear_then_status_empty(index):
    index.build(FakeBackend([_item("A1", "Rocket")]))
    index.clear()
    assert index.status() == ("empty", 0)


def test_filters_by_type_year_and_collection(index):
    backend = FakeBackend(
        [
            _item("A1", "Rocket nozzles", date="2019-01-01", collections=["COL1"]),
            _item("B2", "Rocket nozzles", date="2022-01-01", collections=["COL2"]),
            {
                **_item("C3", "Rocket nozzles", date="2022-01-01"),
                "data": {**_item("C3", "Rocket nozzles", date="2022")["data"], "itemType": "book"},
            },
        ]
    )
    index.build(backend)
    keys = lambda **kw: sorted(h.item_key for h in index.search("rocket nozzles", limit=10, **kw))  # noqa: E731
    assert keys() == ["A1", "B2", "C3"]
    assert keys(year_from=2020) == ["B2", "C3"]
    assert keys(year_to=2020) == ["A1"]
    assert keys(year_from=2020, year_to=2022, item_type="book") == ["C3"]
    assert keys(collection_key="COL1") == ["A1"]
    assert keys(collection_key="NOPE") == []


def test_evidence_and_kind_reported(index):
    text = "combustion instability in liquid rocket engines. " * 8
    backend = FakeBackend(
        [_item("A1", "Rocket nozzles"), _attachment("A1F", "A1")], fulltext={"A1F": text}
    )
    index.build(backend)
    (hit,) = index.search("combustion instability rocket", limit=1)
    assert hit.kind == "fulltext"
    assert hit.evidence > 1
    assert 0 < hit.score <= 1


def test_reference_section_is_not_indexed(index):
    body = "combustion instability in liquid rocket engines. " * 30
    refs = "\nReferences\n" + "[1] Smith J. Combustion. J. Prop. 2001; vol 3, pp 1-9.\n" * 6
    backend = FakeBackend(
        [_item("A1", "Rocket"), _attachment("A1F", "A1")], fulltext={"A1F": body + refs}
    )
    index.build(backend)
    docs = index.collection.get(include=["documents"])["documents"]
    assert not any("Smith J." in d for d in docs)


def test_fulltext_cache_avoids_refetch(index):
    backend = FakeBackend(
        [_item("A1", "Rocket"), _attachment("A1F", "A1")], fulltext={"A1F": "x " * 100}
    )
    index.build(backend)
    assert (index.fulltext_cache_dir / "A1F-1.txt").exists()
    # Same attachment version, new item version: text must come from the cache.
    backend.items = [_item("A1", "Rocket revised", version=2), _attachment("A1F", "A1")]
    backend._fulltext = {}  # would now return nothing if consulted
    stats = index.build(backend)
    assert stats.fulltext_cache_hits == 1 and stats.fulltext_items == 1


def test_rerank_squashes_logits_and_keeps_tail(monkeypatch):
    import zotero_mcp.index as mod

    monkeypatch.setattr(mod, "_RERANK_POOL", 2)

    class CE:
        def predict(self, pairs):
            assert len(pairs) == 2
            return [4.0, -4.0]

    out = SemanticIndex._rerank(CE(), "q", ["a", "b", "c"], [0.5, 0.4, 0.3])
    assert out[0] > 0.98 and out[1] < 0.02 and out[2] == 0.3


def test_where_clause_shapes():
    assert SemanticIndex._where(None, None, None, None) is None
    assert SemanticIndex._where("book", None, None, None) == {"item_type": "book"}
    assert SemanticIndex._where(None, 2000, 2010, "K") == {
        "$and": [{"year_num": {"$gte": 2000}}, {"year_num": {"$lte": 2010}}, {"col_K": True}]
    }


def test_display_passage_prefers_term_bearing_chunk_when_close():
    from zotero_mcp.index import _display_passage, _focus, _query_terms

    terms = _query_terms("Thermal control of the CubeSats")
    assert terms == ["therma", "contro", "cubesa"]
    passages = [
        (0.90, "COTS components are usually adopted.", {"k": 1}),
        (0.85, "Passive thermal control keeps the payload warm.", {"k": 2}),
        (0.50, "Thermal thermal thermal.", {"k": 3}),
    ]
    assert _display_passage(passages, terms)[2] == {"k": 2}
    # Too far behind the best: fall back to the best.
    assert _display_passage([passages[0], passages[2]], terms)[2] == {"k": 1}
    # No term anywhere: best.
    assert _display_passage(passages[:1], terms)[2] == {"k": 1}

    doc = "Launch slots are scarce. CubeSats are small. Passive thermal control is used. More."
    # The sentence with most terms wins, with one sentence of lead-in.
    assert _focus(doc, terms).startswith("CubeSats are small. Passive thermal control")
    assert _focus("no match here", terms) == "no match here"
