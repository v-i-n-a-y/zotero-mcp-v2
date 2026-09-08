# Copyright 2026 Vinay

"""Scite.ai citation tallies and the retraction scan."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.scite import (
    scite_check_retractions,
    scite_enrich_item,
    scite_enrich_search,
)

DOI = "10.48550/arxiv.1706.03762"
TALLY = {
    "doi": DOI,
    "supporting": 12,
    "contrasting": 1,
    "mentioning": 30,
    "total": 43,
}


@pytest.fixture
def scite(monkeypatch):
    """Script what the Scite endpoint returns, and record what was asked."""
    asked: list[list[str]] = []

    def install(payload):
        def fake_get(url, *, settings, params=None, **kwargs):
            asked.append((params or {}).get("dois", "").split(","))
            return payload

        monkeypatch.setattr("zotero_mcp.external.http.get", fake_get)
        return asked

    return install


def test_tallies_are_reported_with_their_breakdown(fake_backend, scite):
    scite({"tallies": [TALLY]})
    data = result_data(scite_enrich_item(item_key="ATTN2345"))
    assert data["tally"]["supporting"] == 12


def test_the_source_and_its_meaning_are_stated(fake_backend, scite):
    scite({"tallies": [TALLY]})
    text = result_text(scite_enrich_item(item_key="ATTN2345"))
    assert "scite.ai" in text
    assert "not from citation counts" in text


def test_a_tally_keyed_dictionary_is_accepted_as_well_as_a_list(fake_backend, scite):
    scite({"tallies": {DOI: TALLY}})
    assert result_data(scite_enrich_item(item_key="ATTN2345"))["tally"]["total"] == 43


def test_an_item_with_no_doi_says_why_it_cannot_be_looked_up(fake_backend, scite):
    scite({"tallies": []})
    with pytest.raises(ToolError) as excinfo:
        scite_enrich_item(item_key="KAHN3456")
    assert "no DOI" in str(excinfo.value)


def test_scite_knowing_nothing_is_reported_plainly(fake_backend, scite):
    scite({"tallies": []})
    data = result_data(scite_enrich_item(item_key="ATTN2345"))
    assert data["tally"] is None


def test_a_nonsense_response_is_treated_as_no_data(fake_backend, scite):
    scite("not a dict at all")
    assert result_data(scite_enrich_item(item_key="ATTN2345"))["tally"] is None


def test_a_search_is_annotated_item_by_item(fake_backend, scite):
    scite({"tallies": [TALLY]})
    data = result_data(scite_enrich_search(query="Attention"))
    keys = {row["key"]: row for row in data["results"]}
    assert keys["ATTN2345"]["tally"]["total"] == 43


def test_items_without_a_doi_are_shown_as_having_no_data(fake_backend, scite):
    scite({"tallies": []})
    text = result_text(scite_enrich_search(query="Kahneman"))
    assert "no Scite data" in text


def test_a_search_that_matches_nothing_says_so(fake_backend, scite):
    scite({"tallies": []})
    assert "No matching items." in result_text(scite_enrich_search(query="no such thing"))


def test_a_retraction_is_flagged_with_its_item(fake_backend, scite):
    scite({"tallies": [{**TALLY, "retracted": True}]})
    data = result_data(scite_check_retractions())
    assert data["flagged"][0]["key"] in {"ATTN2345", "ATTN9876"}
    assert data["flagged"][0]["retracted"] is True


def test_an_editorial_notice_is_flagged_but_not_called_a_retraction(fake_backend, scite):
    scite({"tallies": [{**TALLY, "editorialNotice": True}]})
    data = result_data(scite_check_retractions())
    assert data["flagged"][0]["retracted"] is False
    assert "editorial notice" in result_text(scite_check_retractions())


def test_a_clean_library_is_reported_as_clean(fake_backend, scite):
    scite({"tallies": [TALLY]})
    text = result_text(scite_check_retractions())
    assert "Nothing flagged." in text
    assert "Verify anything flagged against the publisher" in text


def test_the_scan_batches_its_dois_into_one_request(fake_backend, scite):
    asked = scite({"tallies": []})
    scite_check_retractions()
    assert len(asked) == 1
