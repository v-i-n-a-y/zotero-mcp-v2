"""Markdown rendering. Every rendered object must show its key."""

import pytest

from zotero_mcp.models import (
    Annotation,
    AttachmentRef,
    Change,
    CollectionPage,
    CollectionRef,
    ContentChunk,
    Creator,
    ItemDetail,
    ItemSummary,
    Note,
    ResultPage,
    SearchDiagnostics,
    WriteResult,
)
from zotero_mcp.render import (
    creator_summary,
    render_annotations,
    render_collections,
    render_content_chunk,
    render_item_detail,
    render_notes,
    render_result_page,
    render_write_result,
)


def _creators(*last_names):
    return [Creator(creator_type="author", last_name=n) for n in last_names]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], ""),
        (["Smith"], "Smith"),
        (["Smith", "Jones"], "Smith & Jones"),
        (["Smith", "Jones", "Patel"], "Smith, Jones & Patel"),
        (["A", "B", "C", "D", "E"], "A et al."),
    ],
)
def test_creator_summary(names, expected):
    assert creator_summary(_creators(*names)) == expected


def test_creator_summary_prefers_authors_over_editors():
    people = [
        Creator(creator_type="editor", last_name="Editor"),
        Creator(creator_type="author", last_name="Author"),
    ]
    assert creator_summary(people) == "Author"


def test_creator_summary_falls_back_when_there_are_no_authors():
    assert creator_summary([Creator(creator_type="editor", last_name="Editor")]) == "Editor"


def test_creator_summary_uses_institutional_names():
    assert creator_summary([Creator(name="World Health Organization")]) == \
        "World Health Organization"


SUMMARY = ItemSummary(
    key="ABCD2345",
    title="Attention Is All You Need",
    item_type="journalArticle",
    year="2017",
    creator_summary="Vaswani et al.",
    publication="NeurIPS",
    doi="10.1/x",
    has_pdf=True,
    abstract_preview="The dominant sequence transduction models…",
    tags=["nlp"],
)


def test_result_page_always_shows_the_key():
    """The key is what every follow-up call needs; burying it makes models guess."""
    out = render_result_page(ResultPage(items=[SUMMARY], returned=1), heading="Search")
    assert "`ABCD2345`" in out


def test_result_page_reports_an_honest_count_and_the_cursor():
    page = ResultPage(items=[SUMMARY], total=42, offset=20, returned=1, next_cursor="CUR")
    out = render_result_page(page, heading="Search")
    assert "Showing 21–21 of 42" in out
    assert 'cursor="CUR"' in out


def test_result_page_without_a_cursor_does_not_promise_more():
    out = render_result_page(ResultPage(items=[SUMMARY], returned=1), heading="Search")
    assert "cursor" not in out


def test_empty_result_page_surfaces_the_suggestion_and_attempts():
    page = ResultPage(
        items=[],
        diagnostics=SearchDiagnostics(
            strategy="none",
            attempts=["exact (0)", "author-only (0)"],
            suggestion="Try just the author's surname.",
        ),
    )
    out = render_result_page(page, heading="Search")
    assert "No matching items." in out
    assert "Try just the author's surname." in out
    assert "author-only (0)" in out


def test_result_page_notes_a_timeout_rather_than_pretending_completeness():
    page = ResultPage(items=[SUMMARY], returned=1,
                      diagnostics=SearchDiagnostics(strategy="exact", timed_out=True))
    assert "time budget" in render_result_page(page, heading="Search")


def test_item_detail_renders_unusual_types_without_losing_fields():
    detail = ItemDetail(
        key="CASE1234",
        title="Roe v. Wade",
        item_type="case",
        fields={"court": "SCOTUS", "docketNumber": "70-18", "reporter": "U.S."},
    )
    out = render_item_detail(detail)
    assert "`CASE1234`" in out
    for value in ("SCOTUS", "70-18", "U.S."):
        assert value in out


def test_item_detail_groups_creators_by_role():
    detail = ItemDetail(
        key="K", title="T", item_type="book",
        creators=[
            Creator(creator_type="author", last_name="A", first_name="Ann"),
            Creator(creator_type="editor", last_name="E", first_name="Ed"),
        ],
    )
    out = render_item_detail(detail)
    assert "**Author:** Ann A" in out
    assert "**Editor:** Ed E" in out


def test_item_detail_flags_an_unreachable_attachment():
    """A linked file whose path no longer resolves is an otherwise baffling failure."""
    detail = ItemDetail(
        key="K", title="T", item_type="book",
        attachments=[AttachmentRef(key="ATT1", title="paper.pdf", available=False)],
    )
    assert "file not available" in render_item_detail(detail)


def test_content_chunk_states_its_range_and_how_to_continue():
    chunk = ContentChunk(
        item_key="ABCD2345", title="Paper", text="body",
        first_page=1, last_page=5, total_pages=20,
        has_more=True, next_pages="6-10", chars=4,
    )
    out = render_content_chunk(chunk)
    assert "pages 1–5 of 20" in out
    assert 'pages="6-10"' in out


def test_annotations_group_by_page_and_keep_their_keys():
    annotations = [
        Annotation(key="A1", parent_key="P", annotation_type="highlight",
                   text="first", page_label="3"),
        Annotation(key="A2", parent_key="P", annotation_type="note",
                   comment="second", page_label="3"),
        Annotation(key="A3", parent_key="P", annotation_type="highlight",
                   text="third", page_label="9"),
    ]
    out = render_annotations(annotations, heading="Annotations")
    assert out.count("### Page") == 2
    assert "### Page 3" in out and "### Page 9" in out
    for key in ("A1", "A2", "A3"):
        assert f"`{key}`" in out


def test_annotations_empty():
    assert "No annotations found." in render_annotations([], heading="Annotations")


def test_notes_show_their_key_and_parentage():
    notes = [Note(key="N1", parent_key="ABCD2345", title="Finding", text="Body")]
    out = render_notes(notes, heading="Notes")
    assert "`N1`" in out
    assert "child of `ABCD2345`" in out


def test_standalone_note_is_labelled_as_such():
    out = render_notes([Note(key="N1", text="Body")], heading="Notes")
    assert "standalone" in out


def test_collections_are_indented_by_depth():
    page = CollectionPage(
        collections=[
            CollectionRef(key="A", name="Root", path="Root", item_count=3),
            CollectionRef(key="B", name="Child", path="Root/Child", item_count=1),
        ],
        returned=2,
    )
    out = render_collections(page, heading="Collections")
    assert "- **Root** (`A`) — 3 items" in out
    assert "  - **Child** (`B`) — 1 items" in out


def test_dry_run_result_leads_with_the_banner():
    """A preview mistaken for a completed write is what costs a user real data."""
    result = WriteResult(action="delete_items", dry_run=True, succeeded=["A", "B"])
    out = render_write_result(result)
    assert out.startswith("# Preview: delete items")
    assert "**Nothing was changed.**" in out
    assert "Would affect 2" in out


def test_applied_result_has_no_dry_run_banner():
    out = render_write_result(WriteResult(action="update_item", succeeded=["A"], version=9))
    assert "Nothing was changed" not in out
    assert "Affected 1" in out
    assert "Version now 9." in out


def test_write_result_renders_a_change_table():
    result = WriteResult(
        action="update_item",
        changes=[Change(field="title", before="Old", after="New")],
    )
    out = render_write_result(result)
    assert "| title | Old | New |" in out


def test_write_result_shows_failures():
    result = WriteResult(action="delete_items", succeeded=["A"], failed={"B": "not found"})
    out = render_write_result(result)
    assert "`B` — not found" in out


def test_write_result_elides_a_very_long_key_list():
    result = WriteResult(action="batch_update", succeeded=[f"K{i:04d}" for i in range(40)])
    out = render_write_result(result)
    assert "(+15 more)" in out
