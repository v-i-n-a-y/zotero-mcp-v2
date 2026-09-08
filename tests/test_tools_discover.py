# Copyright 2026 Vinay

"""Semantic discovery, and the ChatGPT connector's two tools."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from conftest import LIBRARY_ITEMS, json_copy, result_data, result_text
from zotero_mcp.tools.connectors import connector_fetch, connector_search
from zotero_mcp.tools.discover import zotero_coverage, zotero_find_related


def _hit(index: int, score: float):
    item = json_copy(LIBRARY_ITEMS[index])
    item["_score"] = score
    item["_matched_text"] = "a matching passage"
    return item


@pytest.fixture
def index_with(monkeypatch):
    """Install a stand-in semantic index returning fixed hits."""

    def install(hits):
        monkeypatch.setattr("zotero_mcp.index.query.search", lambda *a, **k: hits)

    return install


def test_related_items_exclude_the_seed_itself(fake_backend, index_with):
    index_with([_hit(0, 1.0), _hit(1, 0.7)])
    data = result_data(zotero_find_related(item_key="ATTN2345"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]
    assert data["items"][0]["score"] == 0.7


def test_related_items_need_an_index(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        zotero_find_related(item_key="ATTN2345")
    assert "index build" in str(excinfo.value)


def test_an_item_with_nothing_to_compare_is_reported(fake_backend, index_with):
    index_with([])
    fake_backend.items["EMTY2345"] = {
        "key": "EMTY2345",
        "version": 1,
        "data": {"key": "EMTY2345", "itemType": "document", "version": 1},
    }
    with pytest.raises(ToolError) as excinfo:
        zotero_find_related(item_key="EMTY2345")
    assert "no title or abstract" in str(excinfo.value)


def test_a_case_is_indexed_under_its_real_title_field(fake_backend):
    """A case stores its title as caseName, so indexing data['title'] loses it."""
    from zotero_mcp.index.builder import document_for

    assert "Roe v. Wade" in document_for(fake_backend.get_item("CASE4567"))


def test_coverage_calls_a_well_matched_topic_well_covered(fake_backend, index_with):
    index_with([_hit(0, 0.9)] * 5)
    data = result_data(zotero_coverage(topic="attention mechanisms"))
    assert "Well covered" in data["verdict"]
    assert data["strong_matches"] == 5


def test_coverage_calls_a_weak_match_partial(fake_backend, index_with):
    index_with([_hit(0, 0.6), _hit(1, 0.2)])
    data = result_data(zotero_coverage(topic="something"))
    assert "Partial" in data["verdict"]


def test_coverage_calls_an_absent_topic_thin(fake_backend, index_with):
    index_with([_hit(0, 0.1)])
    data = result_data(zotero_coverage(topic="badger husbandry"))
    assert "Thin" in data["verdict"]


def test_coverage_says_similarity_is_relative_not_absolute(fake_backend, index_with):
    index_with([_hit(0, 0.42)])
    text = result_text(zotero_coverage(topic="anything"))
    assert "relative, not absolute" in text


def test_coverage_needs_an_index(fake_backend, no_index):
    with pytest.raises(ToolError):
        zotero_coverage(topic="anything")


# -- the connector contract -------------------------------------------------


def test_connector_search_returns_the_shape_chatgpt_expects(fake_backend, no_index):
    payload = json.loads(connector_search(query="Attention"))
    assert set(payload["results"][0]) == {"id", "title", "url"}
    assert payload["results"][0]["id"] == "ATTN2345"


def test_connector_search_prefers_the_index_when_there_is_one(fake_backend, index_with):
    index_with([_hit(1, 0.9)])
    payload = json.loads(connector_search(query="anything at all"))
    assert payload["results"][0]["id"] == "KAHN3456"


def test_connector_fetch_returns_metadata_even_with_no_readable_attachment(fake_backend):
    payload = json.loads(connector_fetch(id="KAHN3456"))
    assert payload["id"] == "KAHN3456"
    assert payload["metadata"]["itemType"] == "book"
    assert "Thinking, Fast and Slow" in payload["text"]


def test_connector_fetch_appends_the_document_text_when_it_is_there(fake_backend):
    fake_backend.fulltext["PDFA2345"] = "The body of the paper."
    payload = json.loads(connector_fetch(id="ATTN2345"))
    assert "The body of the paper." in payload["text"]
