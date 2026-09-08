# Copyright 2026 Vinay

"""Turning an identifier into Zotero item data.

Each source is tested on the shape it actually returns, not an idealised one:
Crossref dates are part-arrays, arXiv answers in Atom XML, and publishers
embed Highwire tags in either attribute order.
"""

from __future__ import annotations

import pytest

from zotero_mcp.config import NetworkSettings
from zotero_mcp.errors import NotFound
from zotero_mcp.external import metadata
from zotero_mcp.identifiers import parse_identifier

SETTINGS = NetworkSettings(allow_external_services=True, max_retries=1, backoff_seconds=0.0)


@pytest.fixture
def responds(monkeypatch):
    """Script what http.get returns, in order."""

    def install(*payloads):
        queue = list(payloads)
        seen: list[tuple] = []

        def fake_get(url, **kwargs):
            seen.append((url, kwargs.get("params") or {}))
            return queue.pop(0) if queue else None

        monkeypatch.setattr(metadata.http, "get", fake_get)
        return seen

    return install


CROSSREF = {
    "message": {
        "type": "journal-article",
        "title": ["A Paper About Things"],
        "container-title": ["Journal of Things"],
        "author": [
            {"given": "Ada", "family": "Lovelace"},
            {"name": "The Institute of Things"},
        ],
        "editor": [{"given": "Ed", "family": "Itor"}],
        "issued": {"date-parts": [[2021, 3, 4]]},
        "URL": "https://doi.org/10.1000/xyz123",
        "abstract": "<jats:p>An <jats:italic>abstract</jats:italic> &amp; more.</jats:p>",
        "volume": "12",
        "issue": "3",
        "page": "1-10",
        "ISSN": ["1234-5678"],
        "language": "en",
    }
}


def test_crossref_dates_are_assembled_from_their_parts(responds):
    responds(CROSSREF)
    data = metadata.from_crossref("10.1000/xyz123", SETTINGS)
    assert data["date"] == "2021-03-04"


def test_crossref_container_titles_land_in_the_right_field(responds):
    responds(CROSSREF)
    assert (
        metadata.from_crossref("10.1000/xyz123", SETTINGS)["publicationTitle"]
        == "Journal of Things"
    )


def test_a_conference_paper_gets_proceedings_title_instead(responds):
    payload = {"message": {**CROSSREF["message"], "type": "proceedings-article"}}
    responds(payload)
    data = metadata.from_crossref("10.1000/xyz123", SETTINGS)
    assert data["proceedingsTitle"] == "Journal of Things"
    assert "publicationTitle" not in data


def test_an_institutional_author_keeps_its_single_name_field(responds):
    responds(CROSSREF)
    creators = metadata.from_crossref("10.1000/xyz123", SETTINGS)["creators"]
    assert {"creatorType": "author", "name": "The Institute of Things"} in creators
    assert any(c.get("creatorType") == "editor" for c in creators)


def test_jats_markup_is_stripped_from_the_abstract(responds):
    responds(CROSSREF)
    abstract = metadata.from_crossref("10.1000/xyz123", SETTINGS)["abstractNote"]
    assert abstract == "An abstract & more."


def test_an_unknown_crossref_type_becomes_a_document(responds):
    responds({"message": {"type": "invented-type", "title": ["X"]}})
    assert metadata.from_crossref("10.1000/xyz123", SETTINGS)["itemType"] == "document"


def test_crossref_saying_nothing_is_none(responds):
    responds(None)
    assert metadata.from_crossref("10.1000/xyz123", SETTINGS) is None


ARXIV_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>Attention Is
      All You Need</title>
    <summary>We propose  a new architecture.</summary>
    <published>2017-06-12T00:00:00Z</published>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Cher</name></author>
    <arxiv:doi>10.1000/arxiv1</arxiv:doi>
  </entry>
</feed>"""


def test_arxiv_atom_is_parsed_and_whitespace_normalised(responds):
    responds(ARXIV_ATOM)
    data = metadata.from_arxiv("1706.03762", SETTINGS)
    assert data["title"] == "Attention Is All You Need"
    assert data["abstractNote"] == "We propose a new architecture."
    assert data["date"] == "2017-06-12"
    assert data["itemType"] == "preprint"


def test_a_one_word_author_name_is_not_split_into_a_blank_surname(responds):
    responds(ARXIV_ATOM)
    creators = metadata.from_arxiv("1706.03762", SETTINGS)["creators"]
    assert {"creatorType": "author", "name": "Cher"} in creators


def test_malformed_arxiv_xml_is_none_rather_than_a_crash(responds):
    responds("<not xml")
    assert metadata.from_arxiv("1706.03762", SETTINGS) is None


def test_an_empty_arxiv_feed_is_none(responds):
    responds('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    assert metadata.from_arxiv("1706.03762", SETTINGS) is None


def test_open_library_answers_are_mapped_to_a_book(responds):
    responds(
        {
            "ISBN:9780374275631": {
                "title": "Thinking, Fast and Slow",
                "authors": [{"name": "Daniel Kahneman"}],
                "publish_date": "2011",
                "publishers": [{"name": "Farrar, Straus and Giroux"}],
                "number_of_pages": 499,
            }
        }
    )
    data = metadata.from_isbn("9780374275631", SETTINGS)
    assert data["itemType"] == "book"
    assert data["numPages"] == "499"
    assert data["creators"][0]["lastName"] == "Kahneman"


def test_an_unknown_isbn_is_none(responds):
    responds({})
    assert metadata.from_isbn("9780374275631", SETTINGS) is None


def test_pubmed_summaries_are_mapped_with_the_pmid_in_extra(responds):
    responds(
        {
            "result": {
                "23193287": {
                    "title": "A Study of Things.",
                    "authors": [{"name": "Lovelace A"}],
                    "fulljournalname": "Journal of Things",
                    "pubdate": "2012 Dec",
                    "articleids": [{"idtype": "doi", "value": "10.1000/xyz123"}],
                }
            }
        }
    )
    data = metadata.from_pubmed("23193287", SETTINGS)
    assert data["title"] == "A Study of Things"
    assert data["extra"] == "PMID: 23193287"
    assert data["DOI"] == "10.1000/xyz123"


def test_a_pubmed_error_record_is_none(responds):
    responds({"result": {"1": {"error": "cannot get document summary"}}})
    assert metadata.from_pubmed("1", SETTINGS) is None


HIGHWIRE = """<html><head>
<meta name="citation_title" content="Embedded Tags Beat OpenGraph"/>
<meta content="Lovelace, Ada" name="citation_author"/>
<meta name="citation_journal_title" content="Journal of Tags"/>
<meta name="citation_publication_date" content="2020-05-06"/>
<meta name="citation_firstpage" content="1"/>
<meta name="citation_lastpage" content="9"/>
</head></html>"""


def test_highwire_tags_produce_a_real_journal_article(responds):
    responds(HIGHWIRE)
    data = metadata.from_webpage("https://example.invalid/paper", SETTINGS)
    assert data["itemType"] == "journalArticle"
    assert data["title"] == "Embedded Tags Beat OpenGraph"
    assert data["publicationTitle"] == "Journal of Tags"
    assert data["pages"] == "1-9"


def test_meta_tags_are_read_in_either_attribute_order(responds):
    responds(HIGHWIRE)
    creators = metadata.from_webpage("https://example.invalid/paper", SETTINGS)["creators"]
    assert creators == [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]


def test_a_page_naming_its_doi_defers_to_crossref(responds):
    responds(
        '<html><head><meta name="citation_doi" content="10.1000/xyz123"/></head></html>',
        CROSSREF,
    )
    data = metadata.fetch(parse_identifier("https://example.invalid/paper"), SETTINGS)
    assert data["title"] == "A Paper About Things"


def test_a_page_naming_a_doi_crossref_does_not_know_says_so(responds):
    responds('<html><head><meta name="citation_doi" content="10.1000/xyz123"/></head></html>', None)
    with pytest.raises(NotFound) as excinfo:
        metadata.fetch(parse_identifier("https://example.invalid/paper"), SETTINGS)
    assert "Crossref has no record" in str(excinfo.value)


def test_a_plain_page_falls_back_to_its_title_tag(responds):
    responds("<html><head><title>  Just A  Page </title></head></html>")
    data = metadata.from_webpage("https://example.invalid/", SETTINGS)
    assert data["itemType"] == "webpage"
    assert data["title"] == "Just A Page"


def test_a_page_with_no_html_at_all_is_none(responds):
    responds(None)
    assert metadata.from_webpage("https://example.invalid/", SETTINGS) is None


@pytest.mark.parametrize(
    ("identifier", "payload", "expected"),
    [
        ("10.1000/xyz123", CROSSREF, "A Paper About Things"),
        ("arXiv:1706.03762", ARXIV_ATOM, "Attention Is All You Need"),
    ],
)
def test_fetch_routes_each_identifier_to_the_source_that_knows_it(
    responds, identifier, payload, expected
):
    responds(payload)
    assert metadata.fetch(parse_identifier(identifier), SETTINGS)["title"] == expected


@pytest.mark.parametrize(
    "identifier",
    ["10.1000/xyz123", "arXiv:1706.03762", "978-0-374-27563-1", "PMID: 23193287", "PMC3531190"],
)
def test_a_source_that_knows_nothing_produces_a_named_not_found(responds, identifier):
    responds(None)
    with pytest.raises(NotFound):
        metadata.fetch(parse_identifier(identifier), SETTINGS)


def test_an_unrecognisable_identifier_lists_the_ones_that_work(responds):
    responds(None)
    with pytest.raises(NotFound) as excinfo:
        metadata.fetch(parse_identifier("just some words"), SETTINGS)
    assert "PMCID" in excinfo.value.hint


def test_a_page_that_yields_nothing_usable_is_reported(responds):
    responds(None)
    with pytest.raises(NotFound) as excinfo:
        metadata.fetch(parse_identifier("https://example.invalid/"), SETTINGS)
    assert "Could not read metadata" in str(excinfo.value)


def test_fields_the_item_type_cannot_hold_are_dropped_not_sent(responds):
    """A preprint has no publicationTitle, and sending one makes Zotero reject the item."""
    responds({"message": {"type": "posted-content", "title": ["P"], "container-title": ["J"]}})
    data = metadata.from_crossref("10.1000/xyz123", SETTINGS)
    assert data["itemType"] == "preprint"
    assert "publicationTitle" not in data
