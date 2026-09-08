# Copyright 2026 Vinay

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
    assert (
        creator_summary([Creator(name="World Health Organization")]) == "World Health Organization"
    )


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
    page = ResultPage(
        items=[SUMMARY], returned=1, diagnostics=SearchDiagnostics(strategy="exact", timed_out=True)
    )
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
        key="K",
        title="T",
        item_type="book",
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
        key="K",
        title="T",
        item_type="book",
        attachments=[AttachmentRef(key="ATT1", title="paper.pdf", available=False)],
    )
    assert "file not available" in render_item_detail(detail)


def test_content_chunk_states_its_range_and_how_to_continue():
    chunk = ContentChunk(
        item_key="ABCD2345",
        title="Paper",
        text="body",
        first_page=1,
        last_page=5,
        total_pages=20,
        has_more=True,
        next_pages="6-10",
        chars=4,
    )
    out = render_content_chunk(chunk)
    assert "pages 1–5 of 20" in out
    assert 'pages="6-10"' in out


def test_annotations_group_by_page_and_keep_their_keys():
    annotations = [
        Annotation(
            key="A1", parent_key="P", annotation_type="highlight", text="first", page_label="3"
        ),
        Annotation(
            key="A2", parent_key="P", annotation_type="note", comment="second", page_label="3"
        ),
        Annotation(
            key="A3", parent_key="P", annotation_type="highlight", text="third", page_label="9"
        ),
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
    assert "- **Root** (`A`) – 3 items" in out
    assert "  - **Child** (`B`) – 1 items" in out


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
    assert "`B`: not found" in out


def test_write_result_elides_a_very_long_key_list():
    result = WriteResult(action="batch_update", succeeded=[f"K{i:04d}" for i in range(40)])
    out = render_write_result(result)
    assert "(+15 more)" in out


# -- the remaining renderers ----------------------------------------------


def test_tags_show_counts_and_flag_automatic_ones():
    from zotero_mcp.models import TagCount
    from zotero_mcp.render import render_tags

    out = render_tags(
        [TagCount(tag="nlp", count=12), TagCount(tag="imported", automatic=True)],
        heading="Tags",
    )
    assert "`nlp` (12)" in out
    assert "*auto*" in out
    assert "2 tags" in out


def test_tags_empty():
    from zotero_mcp.render import render_tags

    assert "No tags found." in render_tags([], heading="Tags")


def test_outline_is_indented_by_level():
    from zotero_mcp.models import OutlineEntry
    from zotero_mcp.render import render_outline

    out = render_outline(
        [
            OutlineEntry(level=1, title="Introduction", page=1),
            OutlineEntry(level=2, title="Background", page=3),
        ],
        heading="Outline",
    )
    assert "- Introduction – p. 1" in out
    assert "  - Background – p. 3" in out


def test_outline_absent_says_so_rather_than_rendering_nothing():
    from zotero_mcp.render import render_outline

    assert "no embedded outline" in render_outline([], heading="Outline")


def test_stats_render_counts_and_the_type_breakdown():
    from zotero_mcp.models import LibraryRef, LibraryStats
    from zotero_mcp.render import render_stats

    out = render_stats(
        LibraryStats(
            library=LibraryRef(library_id="1", library_type="user", name="My Library"),
            total_items=1234,
            by_item_type={"journalArticle": 900, "book": 334},
            collection_count=12,
            tag_count=88,
            attachment_count=700,
            note_count=40,
            items_with_pdf=650,
            items_without_collection=17,
            oldest_year="1953",
            newest_year="2026",
        )
    )
    assert "**Items:** 1,234" in out
    assert "journalArticle: 900" in out
    assert "1953" in out and "2026" in out
    assert "My Library" in out


def test_health_reports_an_unreachable_backend_prominently():
    from zotero_mcp.models import HealthReport
    from zotero_mcp.render import render_health

    out = render_health(
        HealthReport(
            backend="local-http",
            reachable=False,
            schema_version=42,
            semantic_index="unavailable",
            optional_features={"pdf": True, "semantic": False},
            warnings=["Zotero desktop does not appear to be running."],
        )
    )
    assert "**not reachable**" in out
    assert "pdf: available" in out
    assert "semantic: not installed" in out
    assert "Zotero desktop does not appear to be running." in out


def test_health_reports_indexed_item_count_when_known():
    from zotero_mcp.models import HealthReport
    from zotero_mcp.render import render_health

    out = render_health(
        HealthReport(backend="web", reachable=True, semantic_index="ready", indexed_items=4321)
    )
    assert "ready (4,321 items)" in out


def test_duplicates_name_the_suggested_master():
    from zotero_mcp.models import DuplicateGroup, ItemRef
    from zotero_mcp.render import render_duplicates

    out = render_duplicates(
        [
            DuplicateGroup(
                items=[
                    ItemRef(
                        key="AAAA1111",
                        title="Paper",
                        item_type="journalArticle",
                        year="2017",
                        creator_summary="Smith",
                    ),
                    ItemRef(key="BBBB2222", title="Paper", item_type="journalArticle", year="2017"),
                ],
                reason="identical DOI",
                confidence=0.98,
                master_key="AAAA1111",
            )
        ],
        heading="Duplicates",
    )
    assert "identical DOI (98% confidence)" in out
    assert "suggested master" in out
    assert "`BBBB2222`" in out


def test_duplicates_empty():
    from zotero_mcp.render import render_duplicates

    assert "No duplicates found." in render_duplicates([], heading="Duplicates")


def test_content_chunk_for_a_single_page():
    chunk = ContentChunk(
        item_key="K", text="body", first_page=7, last_page=7, total_pages=20, chars=4
    )
    assert "page 7 of 20" in render_content_chunk(chunk)


def test_content_chunk_reports_truncation_when_there_is_no_next_range():
    chunk = ContentChunk(item_key="K", text="body", truncated=True, chars=4)
    assert "truncated to fit the response budget" in render_content_chunk(chunk)


def test_item_detail_lists_related_keys_and_extra():
    detail = ItemDetail(
        key="K",
        title="T",
        item_type="book",
        related_keys=["AAAA1111"],
        fields={"extra": "Citation Key: smith2020"},
    )
    out = render_item_detail(detail)
    assert "## Related items" in out
    assert "`AAAA1111`" in out
    assert "## Extra" in out


def test_item_detail_summarises_note_and_annotation_counts():
    detail = ItemDetail(key="K", title="T", item_type="book", note_count=2, annotation_count=1)
    out = render_item_detail(detail)
    assert "2 notes" in out
    assert "1 annotation" in out


def test_result_page_can_render_unnumbered():
    out = render_result_page(
        ResultPage(items=[SUMMARY], returned=1), heading="Search", numbered=False
    )
    assert "1. **" not in out
    assert "- **Attention" in out


def test_result_page_names_a_non_direct_strategy():
    page = ResultPage(
        items=[SUMMARY], returned=1, diagnostics=SearchDiagnostics(strategy="semantic fallback")
    )
    assert "Matched via semantic fallback." in render_result_page(page, heading="Search")


def test_search_hit_shows_the_matched_passage_instead_of_the_abstract():
    hit = SUMMARY.model_copy(update={"matched_text": "the exact sentence", "score": 0.87})
    out = render_result_page(ResultPage(items=[hit], returned=1), heading="Search")
    assert "> the exact sentence" in out
    assert "score 0.87" in out
    assert "dominant sequence" not in out


def test_annotations_without_a_page_are_labelled_unpaged():
    annotations = [Annotation(key="A1", parent_key="P", annotation_type="note", comment="thought")]
    assert "### Unpaged" in render_annotations(annotations, heading="Annotations")


def test_collections_without_counts_render_cleanly():
    page = CollectionPage(collections=[CollectionRef(key="A", name="Root")], returned=1)
    out = render_collections(page, heading="Collections")
    assert "- **Root** (`A`)" in out
    assert "–" not in out


def test_collections_empty():
    assert "No collections found." in render_collections(CollectionPage(), heading="Collections")


def test_notes_empty():
    assert "No notes found." in render_notes([], heading="Notes")
