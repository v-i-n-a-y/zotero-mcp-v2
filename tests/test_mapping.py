"""Zotero payloads to models — the rules that were previously got wrong."""

import pytest

from zotero_mcp.mapping import (
    citation_key_of,
    collection_paths,
    related_keys_of,
    strip_html,
    tags_of,
    to_annotation,
    to_collection_ref,
    to_item_detail,
    to_item_summary,
    to_note,
    to_tag_count,
    year_of,
)
from zotero_mcp.models import LibraryRef

ARTICLE = {
    "key": "ABCD2345",
    "version": 12,
    "data": {
        "key": "ABCD2345",
        "itemType": "journalArticle",
        "title": "Attention Is All You Need",
        "abstractNote": "The dominant sequence transduction models are based on...",
        "date": "2017-06-12",
        "DOI": "10.48550/arXiv.1706.03762",
        "publicationTitle": "NeurIPS",
        "creators": [
            {"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"},
            {"creatorType": "author", "firstName": "Noam", "lastName": "Shazeer"},
            {"creatorType": "author", "firstName": "Niki", "lastName": "Parmar"},
        ],
        "tags": [{"tag": "nlp"}, {"tag": "attention", "type": 1}],
        "extra": "Citation Key: vaswani2017attention",
    },
    "meta": {"creatorSummary": "Vaswani et al.", "numChildren": 3},
}


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("2017", "2017"),
        ("2017-06-12", "2017"),
        # date[:4] — what both predecessors do — yields "June" and "n.d." here.
        ("June 2017", "2017"),
        ("12/06/2017", "2017"),
        ("n.d.", None),
        ("", None),
        (None, None),
        ("forthcoming", None),
    ],
)
def test_year_of(date, expected):
    assert year_of(date) == expected


def test_strip_html_preserves_paragraph_structure():
    """A naive tag strip runs paragraphs together into one unreadable block."""
    assert strip_html("<p>One</p><p>Two</p>") == "One\nTwo"
    assert strip_html("Line<br/>Break") == "Line\nBreak"
    assert strip_html("<ul><li>A</li><li>B</li></ul>") == "A\nB"


def test_strip_html_unescapes_entities():
    assert strip_html("Tom &amp; Jerry &lt;3") == "Tom & Jerry <3"


def test_strip_html_collapses_runaway_blank_lines():
    assert strip_html("<p>A</p><p></p><p></p><p>B</p>") == "A\n\nB"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_strip_html_handles_empty(value):
    assert strip_html(value) == ""


def test_citation_key_from_extra():
    assert citation_key_of({"extra": "Citation Key: vaswani2017"}) == "vaswani2017"
    assert citation_key_of({"extra": "tex.ids: x\nciteKey: smith2020"}) == "smith2020"


def test_native_citation_key_wins_over_extra():
    data = {"citationKey": "native2024", "extra": "Citation Key: legacy2020"}
    assert citation_key_of(data) == "native2024"


def test_no_citation_key():
    assert citation_key_of({"extra": "PMID: 12345"}) is None
    assert citation_key_of({}) is None


def test_related_keys_handles_a_bare_string():
    """Zotero sends one relation as a string; iterating it yields characters."""
    data = {"relations": {"dc:relation": "http://zotero.org/users/1/items/ABCD2345"}}
    assert related_keys_of(data) == ["ABCD2345"]


def test_related_keys_handles_a_list():
    data = {
        "relations": {
            "dc:relation": [
                "http://zotero.org/users/1/items/ABCD2345",
                "http://zotero.org/users/1/items/EFGH6789",
            ]
        }
    }
    assert related_keys_of(data) == ["ABCD2345", "EFGH6789"]


def test_related_keys_when_absent():
    assert related_keys_of({}) == []
    assert related_keys_of({"relations": {}}) == []


def test_tags_accepts_both_encodings():
    assert tags_of({"tags": [{"tag": "a"}, "b"]}) == ["a", "b"]
    assert tags_of({}) == []


def test_item_summary_maps_the_whole_row():
    summary = to_item_summary(ARTICLE)
    assert summary.key == "ABCD2345"
    assert summary.title == "Attention Is All You Need"
    assert summary.year == "2017"
    assert summary.publication == "NeurIPS"
    assert summary.creator_summary == "Vaswani et al."
    assert summary.citation_key == "vaswani2017attention"
    assert summary.tags == ["nlp", "attention"]
    assert summary.num_children == 3
    assert summary.zotero_uri == "zotero://select/library/items/ABCD2345"


def test_item_summary_truncates_the_abstract():
    summary = to_item_summary(ARTICLE, abstract_chars=20)
    assert summary.abstract_preview.endswith("…")
    assert len(summary.abstract_preview) <= 21


def test_item_summary_derives_a_byline_without_meta():
    raw = {"key": "K", "data": {**ARTICLE["data"], "key": "K"}}
    assert to_item_summary(raw).creator_summary == "Vaswani et al."


def test_item_summary_uses_the_type_specific_title_field():
    """A case has no 'title'; it has 'caseName'."""
    case = {
        "key": "CASE1234",
        "data": {"itemType": "case", "caseName": "Roe v. Wade", "dateDecided": "January 22, 1973"},
    }
    summary = to_item_summary(case)
    assert summary.title == "Roe v. Wade"


def test_item_summary_accepts_an_unwrapped_payload():
    """Some local read paths hand back the data dict alone."""
    summary = to_item_summary({"key": "ABCD2345", "itemType": "book", "title": "T"})
    assert summary.key == "ABCD2345"
    assert summary.title == "T"


def test_group_library_produces_a_group_uri():
    library = LibraryRef(library_id="98765", library_type="group", name="Lab")
    summary = to_item_summary(ARTICLE, library=library)
    assert summary.zotero_uri == "zotero://select/groups/98765/items/ABCD2345"


def test_item_detail_folds_in_children():
    children = [
        {
            "key": "ATT1",
            "data": {
                "itemType": "attachment",
                "title": "PDF",
                "contentType": "application/pdf",
                "filename": "p.pdf",
            },
        },
        {"key": "NOTE1", "data": {"itemType": "note", "note": "<p>hi</p>"}},
        {"key": "ANN1", "data": {"itemType": "annotation", "annotationType": "highlight"}},
    ]
    detail = to_item_detail(ARTICLE, children=children)
    assert [a.key for a in detail.attachments] == ["ATT1"]
    assert detail.note_count == 1
    assert detail.annotation_count == 1


def test_item_detail_excludes_zotero_managed_keys_from_fields():
    detail = to_item_detail(ARTICLE)
    for reserved in ("key", "version", "itemType", "creators", "tags", "dateAdded"):
        assert reserved not in detail.fields
    assert detail.fields["publicationTitle"] == "NeurIPS"


def test_annotation_page_index_is_converted_to_one_based():
    """Zotero stores pageIndex 0-based; every human-facing page number is not."""
    raw = {
        "key": "ANN1",
        "data": {
            "itemType": "annotation",
            "parentItem": "ATT1",
            "annotationType": "highlight",
            "annotationText": "quoted",
            "annotationPosition": {"pageIndex": 0},
        },
    }
    assert to_annotation(raw).page_index == 1


def test_annotation_position_as_a_json_string():
    raw = {
        "key": "ANN1",
        "data": {
            "itemType": "annotation",
            "parentItem": "ATT1",
            "annotationType": "note",
            "annotationPosition": '{"pageIndex": 4}',
        },
    }
    assert to_annotation(raw).page_index == 5


def test_annotation_with_unparseable_position():
    raw = {
        "key": "ANN1",
        "data": {"itemType": "annotation", "parentItem": "ATT1", "annotationPosition": "not json"},
    }
    assert to_annotation(raw).page_index is None


def test_note_title_is_derived_from_its_first_line():
    raw = {
        "key": "N1",
        "data": {
            "itemType": "note",
            "parentItem": "ABCD2345",
            "note": "<p>Key finding</p><p>More detail</p>",
        },
    }
    note = to_note(raw)
    assert note.title == "Key finding"
    assert note.text == "Key finding\nMore detail"
    assert note.parent_key == "ABCD2345"


def test_standalone_note_has_no_parent():
    raw = {"key": "N1", "data": {"itemType": "note", "note": "<p>x</p>", "parentItem": False}}
    assert to_note(raw).parent_key is None


def test_collection_ref_treats_false_parent_as_root():
    """Zotero encodes 'no parent' as boolean false, not null."""
    raw = {"key": "COL1", "data": {"name": "Reading", "parentCollection": False}}
    assert to_collection_ref(raw).parent_key is None


def test_collection_paths_builds_a_hierarchy():
    collections = [
        {"key": "A", "data": {"name": "Root", "parentCollection": False}},
        {"key": "B", "data": {"name": "Child", "parentCollection": "A"}},
        {"key": "C", "data": {"name": "Grandchild", "parentCollection": "B"}},
    ]
    paths = collection_paths(collections)
    assert paths == {"A": "Root", "B": "Root/Child", "C": "Root/Child/Grandchild"}


def test_collection_paths_survives_a_cycle():
    """Impossible in Zotero's data model, trivial to hit in a partial listing."""
    collections = [
        {"key": "A", "data": {"name": "A", "parentCollection": "B"}},
        {"key": "B", "data": {"name": "B", "parentCollection": "A"}},
    ]
    paths = collection_paths(collections)
    assert set(paths) == {"A", "B"}


def test_tag_count_from_both_shapes():
    assert to_tag_count("plain").tag == "plain"
    rich = to_tag_count({"tag": "auto", "meta": {"numItems": 4, "type": 1}})
    assert (rich.tag, rich.count, rich.automatic) == ("auto", 4, True)
