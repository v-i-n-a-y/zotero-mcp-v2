# Copyright 2026 Vinay

"""Export, duplicate detection and the semantic index tool."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.maintenance import zotero_duplicates, zotero_export, zotero_index


@pytest.fixture
def no_better_bibtex(monkeypatch):
    """Better BibTeX is a local plugin, so it is absent in a test run."""
    monkeypatch.setattr("zotero_mcp.external.bibtex.from_better_bibtex", lambda keys: None)


# -- export -----------------------------------------------------------------


def test_bibtex_export_uses_the_citation_key_from_extra(fake_backend, no_better_bibtex):
    data = result_data(zotero_export(item_keys="ATTN2345"))
    assert data["format"] == "bibtex"
    assert "@article{vaswani2017attention," in data["content"]
    assert "Attention Is All You Need" in data["content"]


def test_better_bibtex_is_preferred_when_it_answers(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.external.bibtex.from_better_bibtex", lambda keys: "@article{bbt,}"
    )
    data = result_data(zotero_export(item_keys="ATTN2345"))
    assert data["source"] == "Better BibTeX"


def test_csl_json_export_is_valid_json(fake_backend, no_better_bibtex):
    data = result_data(zotero_export(item_keys="ATTN2345", format="csl-json"))
    records = json.loads(data["content"])
    assert records[0]["title"] == "Attention Is All You Need"
    assert records[0]["type"] == "article-journal"


def test_ris_export_carries_the_expected_tags(fake_backend, no_better_bibtex):
    data = result_data(zotero_export(item_keys="KAHN3456", format="ris"))
    assert "TY  - BOOK" in data["content"]
    assert "ER  -" in data["content"]


def test_a_whole_collection_can_be_exported(fake_backend, no_better_bibtex):
    data = result_data(zotero_export(collection_key="MACH2345"))
    assert data["count"] == 1


def test_exporting_nothing_says_what_to_pass(fake_backend, no_better_bibtex):
    with pytest.raises(ToolError) as excinfo:
        zotero_export()
    assert "collection_key" in str(excinfo.value)


# -- duplicates -------------------------------------------------------------


def test_an_identical_doi_is_the_strongest_duplicate_signal(fake_backend):
    data = result_data(zotero_duplicates(action="find"))
    group = data["groups"][0]
    assert "identical DOI" in group["reason"]
    assert {item["key"] for item in group["items"]} == {"ATTN2345", "ATTN9876"}


def test_the_richer_copy_is_proposed_as_the_one_to_keep(fake_backend):
    data = result_data(zotero_duplicates(action="find"))
    assert data["groups"][0]["master_key"] == "ATTN2345"


def test_a_library_with_no_duplicates_says_so(fake_backend):
    del fake_backend.items["ATTN9876"]
    text = result_text(zotero_duplicates(action="find"))
    assert "Scanned 3 items" in text


def test_merging_previews_what_it_would_absorb_and_trash(fake_backend):
    text = result_text(zotero_duplicates(action="merge", item_keys="ATTN2345, ATTN9876"))
    assert "Would keep `ATTN2345`" in text
    assert "can be restored" in text
    assert fake_backend.writes == []


def test_merging_unions_tags_and_collections_onto_the_master(fake_backend):
    fake_backend.items["ATTN9876"]["data"]["tags"] = [{"tag": "extra"}]
    fake_backend.items["ATTN9876"]["data"]["collections"] = ["BEHV3456"]
    zotero_duplicates(action="merge", item_keys="ATTN2345, ATTN9876", dry_run=False)

    _, (_, patch, _) = fake_backend.writes[0]
    assert [t["tag"] for t in patch["tags"]] == ["extra", "nlp", "transformers"]
    assert patch["collections"] == ["BEHV3456", "MACH2345"]


def test_merging_trashes_rather_than_deletes_the_absorbed_copies(fake_backend):
    zotero_duplicates(action="merge", item_keys="ATTN2345, ATTN9876", dry_run=False)
    assert fake_backend.writes[-1][0] == "trash_items"
    assert "ATTN9876" in fake_backend.trash


def test_a_merge_of_one_item_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_duplicates(action="merge", item_keys="ATTN2345")
    assert "at least two" in str(excinfo.value)


# -- index ------------------------------------------------------------------


def test_index_status_without_an_index_says_how_to_build_one(fake_backend, no_index):
    text = result_text(zotero_index(action="status"))
    assert "zotero-mcp index build" in text


def test_index_status_reports_a_ready_index(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.query.status",
        lambda: {"state": "ready", "detail": "chroma", "count": 42},
    )
    text = result_text(zotero_index(action="status"))
    assert "Indexed items:** 42" in text


def test_an_index_update_reports_what_it_did(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.builder.update_index",
        lambda runtime, limit: {"indexed": 3, "chunks": 7, "skipped": 1, "errors": []},
    )
    data = result_data(zotero_index(action="update"))
    assert data["indexed"] == 3
    assert data["chunks"] == 7
