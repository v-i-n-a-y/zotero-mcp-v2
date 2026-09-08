# Copyright 2026 Vinay

"""The search cascade.

The point of these tests is not that search returns something, it is that it
returns something *by a stated route*: a bare key is a lookup, a citation key
is an exact match, and a query that fails as typed is retried in a simpler
form before being given up on. The diagnostics are the contract.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.search import query_variants, zotero_search


def test_query_variants_include_a_diacritic_free_form():
    variants = query_variants("Müller")
    assert "Müller" in variants
    assert "Muller" in variants


def test_query_variants_are_deduplicated_for_plain_ascii():
    assert query_variants("Vaswani") == ["Vaswani"]


def test_a_bare_item_key_is_a_lookup_not_a_search(fake_backend, no_index):
    data = result_data(zotero_search(query="ATTN2345"))
    assert data["diagnostics"]["strategy"] == "item key"
    assert [item["key"] for item in data["items"]] == ["ATTN2345"]


def test_a_citation_key_matches_exactly(fake_backend, no_index):
    data = result_data(zotero_search(query="@vaswani2017attention"))
    assert data["diagnostics"]["strategy"] == "citation key"
    assert data["items"][0]["key"] == "ATTN2345"


def test_citation_key_mode_reports_a_miss_rather_than_falling_through(fake_backend, no_index):
    data = result_data(zotero_search(query="nobody2099nothing", mode="citation_key"))
    assert data["returned"] == 0
    assert "Better BibTeX" in data["diagnostics"]["suggestion"]


def test_metadata_search_matches_on_creator(fake_backend, no_index):
    data = result_data(zotero_search(query="Kahneman"))
    assert data["diagnostics"]["strategy"] == "title, creator or year"
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_an_over_specified_query_is_simplified_before_being_given_up_on(fake_backend, no_index):
    data = result_data(zotero_search(query="Vaswani transformer architecture paper"))
    assert data["diagnostics"]["strategy"].startswith("simplified to")
    assert data["items"][0]["key"] == "ATTN2345"
    assert any("title/creator/year (0)" in a for a in data["diagnostics"]["attempts"])


def test_full_text_mode_searches_the_wider_fields(fake_backend, no_index):
    data = result_data(zotero_search(query="transduction", mode="fulltext"))
    assert data["diagnostics"]["strategy"] == "full text"
    assert data["items"][0]["key"] == "ATTN2345"


def test_metadata_mode_does_not_fall_through_to_full_text(fake_backend, no_index):
    data = result_data(zotero_search(query="transduction", mode="metadata"))
    assert data["returned"] == 0
    assert data["diagnostics"]["strategy"] == "title, creator or year"
    assert data["diagnostics"]["suggestion"]


def test_semantic_mode_without_an_index_says_so(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        zotero_search(query="how do transformers work", mode="semantic")
    assert "index build" in str(excinfo.value)


def test_semantic_results_are_used_when_an_index_exists(fake_backend, monkeypatch):
    from conftest import LIBRARY_ITEMS, json_copy

    hit = json_copy(LIBRARY_ITEMS[0])
    hit["_score"] = 0.87
    monkeypatch.setattr("zotero_mcp.index.query.search", lambda *a, **k: [hit])
    data = result_data(zotero_search(query="a topic no title contains", mode="semantic"))
    assert data["diagnostics"]["strategy"] == "semantic similarity"


def test_a_tag_filter_alone_is_a_valid_search(fake_backend, no_index):
    data = result_data(zotero_search(tags="psychology"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_an_empty_search_is_refused_with_a_pointer_to_browsing(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        zotero_search()
    assert "zotero_library" in str(excinfo.value)


def test_a_miss_carries_a_concrete_suggestion(fake_backend, no_index):
    data = result_data(zotero_search(query="quantum chromodynamics of badgers"))
    assert data["returned"] == 0
    assert "substring matching" in data["diagnostics"]["suggestion"]


def test_paging_reuses_the_original_query_rather_than_the_cascade(fake_backend, no_index):
    first = result_data(zotero_search(query="Attention", limit=1))
    assert first["next_cursor"]
    second = result_data(zotero_search(query="Attention", limit=1, cursor=first["next_cursor"]))
    assert second["offset"] == 1
    assert "diagnostics" not in second
    assert second["items"][0]["key"] != first["items"][0]["key"]


def test_a_cursor_from_a_different_query_is_rejected(fake_backend, no_index):
    first = result_data(zotero_search(query="Attention", limit=1))
    with pytest.raises(ToolError):
        zotero_search(query="Kahneman", cursor=first["next_cursor"])


def test_the_markdown_half_names_the_strategy(fake_backend, no_index):
    text = result_text(zotero_search(query="Kahneman"))
    assert "Thinking, Fast and Slow" in text
    assert "KAHN3456" in text
