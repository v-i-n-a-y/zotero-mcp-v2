# Copyright 2026 Vinay

"""Fetching items, reading attachments and collecting annotations."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.items import zotero_get_annotations, zotero_get_item, zotero_read


def test_a_single_key_returns_one_detail_not_a_list(fake_backend):
    data = result_data(zotero_get_item(keys="ATTN2345"))
    assert data["key"] == "ATTN2345"
    assert data["title"] == "Attention Is All You Need"
    assert "items" not in data


def test_several_keys_come_back_as_a_list(fake_backend):
    data = result_data(zotero_get_item(keys="ATTN2345, KAHN3456"))
    assert [item["key"] for item in data["items"]] == ["ATTN2345", "KAHN3456"]


def test_children_are_summarised_rather_than_dumped(fake_backend):
    data = result_data(zotero_get_item(keys="ATTN2345", include="children"))
    assert [a["key"] for a in data["attachments"]] == ["PDFA2345"]
    assert data["note_count"] == 1


def test_collection_names_are_resolved_when_asked_for(fake_backend):
    data = result_data(zotero_get_item(keys="ATTN2345", include="children,collections"))
    assert [col["name"] for col in data["collections"]] == ["Machine Learning"]


def test_a_doi_is_accepted_where_a_key_is_expected(fake_backend):
    data = result_data(zotero_get_item(keys="10.48550/arxiv.1706.03762"))
    assert data["key"] in {"ATTN2345", "ATTN9876"}


def test_a_zotero_uri_is_accepted_where_a_key_is_expected(fake_backend):
    data = result_data(zotero_get_item(keys="zotero://select/library/items/ATTN2345"))
    assert data["key"] == "ATTN2345"


def test_an_unknown_doi_says_how_to_import_it(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_get_item(keys="10.1234/not-in-this-library")
    assert "zotero_add_item" in str(excinfo.value)


def test_a_malformed_reference_points_at_search(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_get_item(keys="the attention paper")
    assert "zotero_search" in str(excinfo.value)


def test_no_keys_at_all_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_get_item(keys="")


def test_too_many_keys_in_one_call_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_get_item(keys=["ATTN2345"] * 26)
    assert "25" in str(excinfo.value)


def test_a_missing_item_is_reported_as_missing(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_get_item(keys="ZZZZ2345")
    assert "ZZZZ2345" in str(excinfo.value)


# -- reading ----------------------------------------------------------------


def test_reading_falls_back_to_the_stored_full_text(fake_backend):
    fake_backend.fulltext["PDFA2345"] = "Page one text.\nPage two text."
    data = result_data(zotero_read(item_key="ATTN2345"))
    assert "Page one text." in data["text"]
    assert data["attachment_key"] == "PDFA2345"


def test_reading_an_item_with_no_readable_attachment_explains_why(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_read(item_key="KAHN3456")
    assert "attachment" in str(excinfo.value).lower()


def test_an_outline_needs_the_file_itself_and_says_so(fake_backend):
    """An outline comes from the PDF structure, so the stored text cannot stand in."""
    fake_backend.fulltext["PDFA2345"] = "Some text."
    with pytest.raises(ToolError) as excinfo:
        zotero_read(item_key="ATTN2345", what="outline")
    assert "not available on this machine" in str(excinfo.value)


# -- annotations ------------------------------------------------------------


def test_annotations_are_found_through_the_attachment(fake_backend):
    data = result_data(zotero_get_annotations(item_key="ATTN2345"))
    assert [a["key"] for a in data["annotations"]] == ["ANNT2345"]
    assert [n["key"] for n in data["notes"]] == ["NTEA2345"]


def test_annotations_can_be_filtered_to_notes_only(fake_backend):
    data = result_data(zotero_get_annotations(item_key="ATTN2345", kind="notes"))
    assert data["annotations"] == []
    assert len(data["notes"]) == 1


def test_annotations_can_be_searched_by_text(fake_backend):
    data = result_data(zotero_get_annotations(item_key="ATTN2345", query="central claim"))
    assert len(data["annotations"]) == 1
    data = result_data(zotero_get_annotations(item_key="ATTN2345", query="no such phrase"))
    assert data["annotations"] == []


def test_an_item_with_no_annotations_gets_the_synced_pdf_hint(fake_backend):
    text = result_text(zotero_get_annotations(item_key="KAHN3456"))
    assert "synced" in text
