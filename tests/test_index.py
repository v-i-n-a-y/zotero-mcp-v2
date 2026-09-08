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
