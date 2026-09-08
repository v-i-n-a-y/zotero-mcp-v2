# Copyright 2026 Vinay

"""Locally generated BibTeX, CSL-JSON and RIS.

The fallback generator is tested as a real implementation, because that is
what it has to be: a user without the Better BibTeX plugin gets these bytes
pasted into a manuscript.
"""

from __future__ import annotations

import pytest

from zotero_mcp.external import bibtex


def item(**fields):
    return {"data": {"itemType": "journalArticle", **fields}}


AUTHORS = [
    {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
    {"creatorType": "editor", "firstName": "Ed", "lastName": "Itor"},
    {"creatorType": "author", "name": "The Institute"},
]


# -- escaping and keys ------------------------------------------------------


def test_braces_survive_escaping_because_authors_use_them():
    assert bibtex.escape_bibtex("The {DNA} of $x^2$") == r"The {DNA} of \$x\^{}2\$".replace(
        r"\^{}", r"\textasciicircum{}"
    )


def test_the_characters_latex_would_choke_on_are_escaped():
    assert bibtex.escape_bibtex("a & b_c 50%") == r"a \& b\_c 50\%"


def test_a_citation_key_reads_like_one_a_person_would_type():
    key = bibtex.make_citation_key(
        {"creators": AUTHORS, "date": "2017-06-12", "title": "Attention Is All You Need"}
    )
    assert key == "lovelace2017attention"


def test_an_existing_better_bibtex_key_wins():
    data = {"extra": "Citation Key: mine2020", "creators": AUTHORS, "date": "2017"}
    assert bibtex.make_citation_key(data) == "mine2020"


def test_a_key_with_nothing_to_build_from_still_parses():
    assert bibtex.make_citation_key({}) == "anonnd"


def test_diacritics_are_folded_out_of_the_key():
    key = bibtex.make_citation_key(
        {"creators": [{"creatorType": "author", "lastName": "Müller"}], "date": "2020"}
    )
    assert key.startswith("muller2020")


def test_stop_words_are_skipped_when_picking_the_title_word():
    key = bibtex.make_citation_key(
        {
            "creators": [{"creatorType": "author", "lastName": "Smith"}],
            "date": "2020",
            "title": "The Structure of Things",
        }
    )
    assert key == "smith2020structure"


def test_colliding_keys_are_disambiguated_with_a_suffix():
    used: set[str] = set()
    data = {"creators": [{"creatorType": "author", "lastName": "Smith"}], "date": "2020"}
    assert bibtex.make_citation_key(data, used) == "smith2020"
    assert bibtex.make_citation_key(data, used) == "smith2020a"
    assert bibtex.make_citation_key(data, used) == "smith2020b"


# -- BibTeX -----------------------------------------------------------------


def test_bibtex_renders_authors_editors_and_the_entry_type():
    out = bibtex.to_bibtex([item(title="A Paper", date="2020", creators=AUTHORS)])
    assert out.startswith("@article{lovelace2020paper,")
    assert "author = {Lovelace, Ada and {The Institute}}" in out
    assert "editor = {Itor, Ed}" in out


def test_page_ranges_use_the_double_hyphen_bibtex_expects():
    out = bibtex.to_bibtex([item(title="P", date="2020", pages="10-20")])
    assert "pages = {10--20}" in out


@pytest.mark.parametrize(
    ("item_type", "entry"),
    [
        ("book", "book"),
        ("bookSection", "incollection"),
        ("conferencePaper", "inproceedings"),
        ("thesis", "phdthesis"),
        ("somethingElse", "misc"),
    ],
)
def test_each_zotero_type_maps_to_a_bibtex_entry_type(item_type, entry):
    out = bibtex.to_bibtex([{"data": {"itemType": item_type, "title": "T", "date": "2020"}}])
    assert out.startswith(f"@{entry}{{")


def test_attachments_and_notes_are_not_bibliography_entries():
    rows = [{"data": {"itemType": t, "title": "x"}} for t in ("attachment", "note", "annotation")]
    assert bibtex.to_bibtex(rows) == ""


def test_a_raw_data_dict_works_as_well_as_a_wrapped_item():
    assert "@article" in bibtex.to_bibtex(
        [{"itemType": "journalArticle", "title": "T", "date": "2020"}]
    )


# -- CSL-JSON ---------------------------------------------------------------


def test_csl_json_uses_the_csl_type_names():
    records = bibtex.to_csl_json([item(title="A Paper", date="2020", creators=AUTHORS)])
    assert records[0]["type"] == "article-journal"
    assert records[0]["issued"] == {"date-parts": [[2020]]}


def test_csl_json_uses_a_literal_name_for_an_institutional_author():
    records = bibtex.to_csl_json([item(title="T", creators=AUTHORS)])
    assert {"literal": "The Institute"} in records[0]["author"]


def test_csl_json_maps_the_container_title():
    records = bibtex.to_csl_json([item(title="T", publicationTitle="Journal of Things")])
    assert records[0]["container-title"] == "Journal of Things"


def test_csl_json_skips_attachments():
    assert bibtex.to_csl_json([{"data": {"itemType": "attachment"}}]) == []


# -- RIS --------------------------------------------------------------------


def test_ris_opens_with_its_type_and_closes_with_the_end_tag():
    out = bibtex.to_ris([item(title="A Paper", date="2020", creators=AUTHORS)])
    assert out.startswith("TY  - JOUR")
    assert out.rstrip().endswith("ER  -")


def test_ris_splits_a_page_range_into_start_and_end():
    out = bibtex.to_ris([item(title="T", pages="10-20")])
    assert "SP  - 10" in out
    assert "EP  - 20" in out


def test_ris_carries_tags_as_keywords():
    out = bibtex.to_ris([item(title="T", tags=[{"tag": "nlp"}])])
    assert "KW  - nlp" in out


def test_ris_gives_an_unmapped_type_the_generic_tag():
    out = bibtex.to_ris([{"data": {"itemType": "somethingElse", "title": "T"}}])
    assert out.startswith("TY  - GEN")


def test_ris_skips_notes():
    assert bibtex.to_ris([{"data": {"itemType": "note"}}]) == ""


# -- the Better BibTeX bridge -----------------------------------------------


class FakePost:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


def test_better_bibtex_output_is_returned_when_the_plugin_answers(monkeypatch):
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: FakePost(payload={"result": "@article{bbt,}"})
    )
    assert bibtex.from_better_bibtex(["ATTN2345"]) == "@article{bbt,}"


def test_a_list_result_takes_its_last_element(monkeypatch):
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: FakePost(payload={"result": [0, "@book{x,}"]})
    )
    assert bibtex.from_better_bibtex(["ATTN2345"]) == "@book{x,}"


def test_the_plugin_not_running_is_none_rather_than_an_error(monkeypatch):
    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("requests.post", refuse)
    assert bibtex.from_better_bibtex(["ATTN2345"]) is None


def test_an_error_response_from_the_plugin_is_none(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: FakePost(status=500))
    assert bibtex.from_better_bibtex(["ATTN2345"]) is None


def test_an_empty_export_is_none_not_an_empty_bibliography(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: FakePost(payload={"result": "  "}))
    assert bibtex.from_better_bibtex(["ATTN2345"]) is None
