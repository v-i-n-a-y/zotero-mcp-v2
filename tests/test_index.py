# Copyright 2026 Vinay

"""The semantic index: chunking, the store wrapper, and the build.

ChromaDB and sentence-transformers are optional and slow, so the store is
exercised against a stand-in collection that behaves the way Chroma does,
including returning distances rather than similarities.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zotero_mcp.config import SemanticSettings, ZoteroConfig
from zotero_mcp.errors import AuthError, Unsupported
from zotero_mcp.index import query as index_query
from zotero_mcp.index.builder import chunk_text, document_for, update_index
from zotero_mcp.index.embeddings import (
    GeminiEmbedder,
    LocalEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from zotero_mcp.index.store import Hit, IndexStore, default_db_path

# -- chunking ---------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert chunk_text("Short.", size=100, overlap=10) == ["Short."]


def test_empty_text_produces_no_chunks():
    assert chunk_text("   ", size=100, overlap=10) == []


def test_long_text_is_split_with_overlap():
    text = "a" * 250
    chunks = chunk_text(text, size=100, overlap=20)
    assert len(chunks) > 2
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_splitting_prefers_a_paragraph_boundary():
    text = "A" * 60 + "\n\n" + "B" * 120
    chunks = chunk_text(text, size=100, overlap=10)
    assert chunks[0] == "A" * 60


def test_splitting_falls_back_to_a_sentence_boundary():
    text = "A" * 60 + ". " + "B" * 120
    chunks = chunk_text(text, size=100, overlap=10)
    assert chunks[0].endswith(".")


def test_a_document_leads_with_title_and_creators(fake_backend):
    text = document_for(fake_backend.get_item("ATTN2345"))
    assert text.startswith("Attention Is All You Need")
    assert "Vaswani, Shazeer" in text
    assert "Tags: nlp, transformers" in text


# -- embedders --------------------------------------------------------------


def test_the_signature_names_the_vector_space():
    assert LocalEmbedder("m").signature == "default:m"
    assert OpenAIEmbedder("m", api_key="k").signature == "openai:m"


def test_inputs_are_truncated_to_what_the_model_accepts():
    embedder = LocalEmbedder()
    assert len(embedder.prepare(["x" * 10_000])[0]) == embedder.max_chars


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("default", LocalEmbedder),
        ("local", LocalEmbedder),
        ("sentence-transformers", LocalEmbedder),
        ("OpenAI", OpenAIEmbedder),
        ("gemini", GeminiEmbedder),
        ("google", GeminiEmbedder),
    ],
)
def test_each_provider_name_builds_its_embedder(name, expected):
    assert isinstance(build_embedder(name), expected)


def test_an_unknown_provider_lists_the_real_ones():
    with pytest.raises(Unsupported) as excinfo:
        build_embedder("wishful")
    assert "default, openai, gemini" in excinfo.value.hint


def test_openai_without_a_key_says_which_variable_to_set(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AuthError) as excinfo:
        OpenAIEmbedder().embed(["x"])
    assert "OPENAI_API_KEY" in excinfo.value.hint


def test_gemini_without_a_key_says_which_variables_to_set(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(AuthError) as excinfo:
        GeminiEmbedder().embed(["x"])
    assert "GOOGLE_API_KEY" in excinfo.value.hint


def test_openai_results_are_reordered_by_the_index_the_api_returns(monkeypatch):
    class Row:
        def __init__(self, index, embedding):
            self.index = index
            self.embedding = embedding

    class Client:
        class embeddings:
            @staticmethod
            def create(model, input):
                return type("R", (), {"data": [Row(1, [2.0]), Row(0, [1.0])]})

    embedder = OpenAIEmbedder(api_key="k")
    embedder._client = Client()
    assert embedder.embed(["a", "b"]) == [[1.0], [2.0]]


def test_the_local_embedder_converts_whatever_the_model_returns(monkeypatch):
    class Encoder:
        @staticmethod
        def encode(texts, show_progress_bar=False):
            return [[1, 2] for _ in texts]

    embedder = LocalEmbedder()
    embedder._encoder = Encoder()
    assert embedder.embed(["a"]) == [[1.0, 2.0]]


def test_gemini_results_are_unwrapped(monkeypatch):
    class Client:
        class models:
            @staticmethod
            def embed_content(model, contents):
                return type("R", (), {"embeddings": [type("E", (), {"values": [0.5]})()]})

    embedder = GeminiEmbedder(api_key="k")
    embedder._client = Client()
    assert embedder.embed(["a"]) == [[0.5]]


# -- the store wrapper ------------------------------------------------------


class FakeEmbedder:
    provider = "fake"
    model = "v1"
    signature = "fake:v1"
    max_chars = 1_000

    def prepare(self, texts):
        return texts

    def embed(self, texts):
        return [[float(len(text))] for text in texts]


class FakeCollection:
    """Behaves like a Chroma collection, distances and all."""

    def __init__(self, response=None):
        self.response = response or {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        self.upserted = []
        self.deleted = []
        self.metadata = {"embedder": "fake:v1"}

    def query(self, query_embeddings, n_results, where=None):
        self.last_where = where
        self.last_n = n_results
        return self.response

    def count(self):
        return 7

    def upsert(self, ids, documents, metadatas, embeddings):
        self.upserted.append((ids, documents, metadatas))

    def delete(self, where):
        self.deleted.append(where)

    def get(self, include=None):
        return {"metadatas": [{"item_key": "ATTN2345"}, {}, None]}


def store_with(response=None):
    collection = FakeCollection(response)
    return IndexStore(Path("/tmp/index"), FakeEmbedder(), collection), collection


def test_a_chroma_distance_becomes_a_similarity_score():
    store, _ = store_with(
        {
            "documents": [["some text"]],
            "metadatas": [[{"item_key": "ATTN2345", "chunk": 0}]],
            "distances": [[0.25]],
        }
    )
    hits = store.query("anything")
    assert hits == [Hit(item_key="ATTN2345", score=0.75, text="some text", chunk=0)]


def test_only_the_best_chunk_of_an_item_is_returned():
    store, _ = store_with(
        {
            "documents": [["worse", "better"]],
            "metadatas": [[{"item_key": "A", "chunk": 0}, {"item_key": "A", "chunk": 1}]],
            "distances": [[0.9, 0.1]],
        }
    )
    hits = store.query("anything")
    assert len(hits) == 1
    assert hits[0].chunk == 1


def test_a_chunk_with_no_item_key_is_skipped():
    store, _ = store_with({"documents": [["x"]], "metadatas": [[{}]], "distances": [[0.1]]})
    assert store.query("anything") == []


def test_a_collection_filter_is_passed_to_chroma():
    store, collection = store_with()
    store.query("anything", collection_key="MACH2345")
    assert collection.last_where == {"collections": {"$in": ["MACH2345"]}}


def test_the_store_over_fetches_so_distinct_items_survive_deduplication():
    store, collection = store_with()
    store.query("anything", limit=10)
    assert collection.last_n == 40


def test_chunk_ids_are_deterministic_so_a_rebuild_overwrites():
    store, collection = store_with()
    written = store.upsert(
        [{"item_key": "ATTN2345", "chunk": 0, "text": "t", "collections": ["MACH2345"]}]
    )
    assert written == 1
    ids, _, metadatas = collection.upserted[0]
    assert ids == ["ATTN2345#0"]
    assert metadatas[0]["collections"] == ["MACH2345"]


def test_upserting_nothing_writes_nothing():
    store, collection = store_with()
    assert store.upsert([]) == 0
    assert collection.upserted == []


def test_deleting_no_keys_does_not_call_the_store():
    store, collection = store_with()
    store.delete_items([])
    assert collection.deleted == []
    store.delete_items(["A"])
    assert collection.deleted == [{"item_key": {"$in": ["A"]}}]


def test_indexed_keys_ignores_rows_with_no_key():
    store, _ = store_with()
    assert store.indexed_keys() == {"ATTN2345"}


def test_count_and_describe_report_the_index():
    store, _ = store_with()
    assert store.count() == 7
    assert "fake:v1" in store.describe()


def test_the_default_index_path_follows_the_configuration(tmp_path):
    config = ZoteroConfig(semantic=SemanticSettings(db_path=str(tmp_path / "here")))
    assert default_db_path(config) == tmp_path / "here"
    assert default_db_path(ZoteroConfig()).name == "index"


# -- reading the index ------------------------------------------------------


def test_no_index_reads_as_absent_not_empty(fake_backend, monkeypatch):
    monkeypatch.setattr("zotero_mcp.index.store.open_store", lambda config, create=False: None)
    assert index_query.search("anything") is None
    assert index_query.available() is False
    assert index_query.status()["state"] == "unavailable"


def test_a_disabled_index_reads_as_absent(fake_backend, monkeypatch):
    from zotero_mcp.runtime import Runtime, set_runtime

    set_runtime(
        Runtime(config=ZoteroConfig(semantic=SemanticSettings(enabled=False)), backend=fake_backend)
    )
    assert index_query.search("anything") is None


def test_a_broken_index_is_reported_as_an_error_not_a_crash(fake_backend, monkeypatch):
    def explode(config, create=False):
        raise RuntimeError("the database is corrupt")

    monkeypatch.setattr("zotero_mcp.index.store.open_store", explode)
    assert index_query.search("anything") is None
    assert index_query.available() is False
    assert index_query.status() == {
        "state": "error",
        "detail": "the database is corrupt",
        "count": None,
    }


def test_a_failing_query_degrades_to_no_results_rather_than_an_error(fake_backend, monkeypatch):
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr("zotero_mcp.index.store.open_store", lambda config, create=False: Broken())
    assert index_query.search("anything") is None


def test_hits_are_resolved_back_to_real_items(fake_backend, monkeypatch):
    class Ready:
        def query(self, text, limit, collection_key=None):
            return [
                Hit(item_key="ATTN2345", score=0.9, text="a passage"),
                Hit(item_key="GONE2345", score=0.8, text="stale"),
            ]

        def count(self):
            return 1

        def describe(self):
            return "fake"

    monkeypatch.setattr("zotero_mcp.index.store.open_store", lambda config, create=False: Ready())
    hits = index_query.search("anything")
    assert [hit["key"] for hit in hits] == ["ATTN2345"]
    assert hits[0]["_score"] == 0.9
    assert index_query.status() == {"state": "ready", "detail": "fake", "count": 1}
    assert index_query.available() is True


def test_a_store_that_cannot_describe_itself_is_an_error(fake_backend, monkeypatch):
    class Odd:
        def describe(self):
            raise RuntimeError("no")

        def count(self):
            return 0

    monkeypatch.setattr("zotero_mcp.index.store.open_store", lambda config, create=False: Odd())
    assert index_query.status()["state"] == "error"


# -- building ---------------------------------------------------------------


class RecordingStore:
    def __init__(self, indexed=(), fail=False):
        self._indexed = set(indexed)
        self.fail = fail
        self.written: list[dict] = []

    def indexed_keys(self):
        return set(self._indexed)

    def upsert(self, records):
        if self.fail:
            raise RuntimeError("the embedding provider refused")
        self.written.extend(records)
        return len(records)


@pytest.fixture
def building(fake_backend, monkeypatch):
    """Run a build against a recording store, and hand it back."""

    def install(store, **config_kwargs):
        from zotero_mcp.runtime import Runtime

        monkeypatch.setattr("zotero_mcp.index.store.open_store", lambda config, create=False: store)
        config = ZoteroConfig(semantic=SemanticSettings(enabled=True, **config_kwargs))
        return Runtime(config=config, backend=fake_backend)

    return install


def test_a_build_indexes_every_item_once(building):
    store = RecordingStore()
    stats = update_index(building(store))
    assert stats["indexed"] == 4
    assert stats["chunks"] == len(store.written)
    assert {record["item_key"] for record in store.written} == {
        "ATTN2345",
        "KAHN3456",
        "CASE4567",
        "ATTN9876",
    }


def test_already_indexed_items_are_skipped(building):
    store = RecordingStore(indexed={"ATTN2345", "KAHN3456"})
    stats = update_index(building(store))
    assert stats["skipped"] == 2
    assert stats["indexed"] == 2


def test_a_rebuild_ignores_what_is_already_there(building):
    store = RecordingStore(indexed={"ATTN2345", "KAHN3456"})
    assert update_index(building(store), rebuild=True)["indexed"] == 4


def test_a_limit_stops_the_build_early(building):
    assert update_index(building(RecordingStore()), limit=1)["indexed"] == 1


def test_a_failing_batch_is_recorded_rather_than_raised(building):
    stats = update_index(building(RecordingStore(fail=True)))
    assert stats["chunks"] == 0
    assert stats["errors"]


def test_attachment_text_is_folded_in_when_asked_for(building, fake_backend):
    fake_backend.fulltext["PDFA2345"] = "The full body of the paper."
    store = RecordingStore()
    update_index(building(store), include_fulltext=True)
    joined = " ".join(record["text"] for record in store.written)
    assert "The full body of the paper." in joined


def test_a_disabled_semantic_index_refuses_to_build(fake_backend, monkeypatch):
    from zotero_mcp.runtime import Runtime

    runtime = Runtime(
        config=ZoteroConfig(semantic=SemanticSettings(enabled=False)), backend=fake_backend
    )
    with pytest.raises(Unsupported) as excinfo:
        update_index(runtime)
    assert "ZOTERO_MCP_SEMANTIC" in excinfo.value.hint


def test_progress_is_reported_while_building(building):
    seen: list[tuple] = []
    update_index(
        building(RecordingStore()), progress=lambda done, total: seen.append((done, total))
    )
    assert seen[-1][0] == 4
