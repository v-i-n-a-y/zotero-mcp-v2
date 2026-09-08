# Copyright 2026 Vinay

"""Notes and annotations."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.notes import text_to_html, zotero_manage_annotation, zotero_manage_note

# -- the markdown to HTML conversion ----------------------------------------


def test_plain_text_becomes_a_paragraph():
    assert text_to_html("Hello there") == "<p>Hello there</p>"


def test_headings_bullets_and_emphasis_survive():
    html = text_to_html("# Title\n\n- one\n- two\n\n**bold** and *thin* and `code`")
    assert "<h1>Title</h1>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert "<strong>bold</strong>" in html
    assert "<em>thin</em>" in html
    assert "<code>code</code>" in html


def test_html_in_the_source_text_is_escaped_not_executed():
    assert "<script>" not in text_to_html("<script>alert(1)</script>")


def test_a_single_newline_stays_inside_one_paragraph():
    assert text_to_html("one\ntwo") == "<p>one<br/>two</p>"


def test_empty_text_still_produces_valid_html():
    assert text_to_html("   ") == "<p></p>"


# -- notes ------------------------------------------------------------------


def test_a_note_is_created_as_html_under_its_parent(fake_backend):
    result_data(
        zotero_manage_note(action="create", text="**Important**", parent_item_key="ATTN2345")
    )
    name, (payloads,) = fake_backend.writes[0]
    assert name == "create_items"
    assert payloads[0]["note"] == "<p><strong>Important</strong></p>"
    assert payloads[0]["parentItem"] == "ATTN2345"


def test_raw_html_is_passed_through_when_asked_for(fake_backend):
    zotero_manage_note(action="create", text="<p>Mine</p>", html=True)
    assert fake_backend.writes[0][1][0][0]["note"] == "<p>Mine</p>"


def test_a_note_with_no_text_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_note(action="create", text="  ")
    assert "needs some text" in str(excinfo.value)


def test_appending_keeps_what_was_already_there(fake_backend):
    zotero_manage_note(action="update", note_key="NTEA2345", text="More.", append=True)
    _, (_, patch, _) = fake_backend.writes[0]
    assert patch["note"].startswith("<p>Key idea")
    assert patch["note"].endswith("<p>More.</p>")


def test_updating_without_append_replaces_the_body(fake_backend):
    zotero_manage_note(action="update", note_key="NTEA2345", text="Replaced.")
    _, (_, patch, _) = fake_backend.writes[0]
    assert patch["note"] == "<p>Replaced.</p>"


def test_updating_a_note_needs_its_key(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_note(action="update", text="x")
    assert "note_key" in str(excinfo.value)


def test_deleting_something_that_is_not_a_note_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_note(action="delete", note_key="ATTN2345")
    assert "zotero_manage_items" in str(excinfo.value)


def test_a_note_deletion_can_be_previewed(fake_backend):
    text = result_text(zotero_manage_note(action="delete", note_key="NTEA2345", dry_run=True))
    assert "Would delete" in text
    assert fake_backend.writes == []


def test_a_note_is_deleted_when_asked_to(fake_backend):
    zotero_manage_note(action="delete", note_key="NTEA2345")
    assert fake_backend.writes[0][0] == "delete_items"


# -- annotations ------------------------------------------------------------


def test_an_annotation_named_by_item_is_attached_to_its_pdf(fake_backend):
    result_data(
        zotero_manage_annotation(action="create", item_key="ATTN2345", text="a passage", page=3)
    )
    payload = fake_backend.writes[0][1][0][0]
    assert payload["parentItem"] == "PDFA2345"
    assert payload["annotationPosition"]["pageIndex"] == 2
    assert payload["annotationPageLabel"] == "3"


def test_the_sort_index_puts_annotations_in_reading_order(fake_backend):
    zotero_manage_annotation(action="create", item_key="ATTN2345", text="x", page=12)
    payload = fake_backend.writes[0][1][0][0]
    assert payload["annotationSortIndex"].startswith("00011|")


def test_a_highlight_without_its_text_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_annotation(action="create", item_key="ATTN2345", page=1)
    assert "zotero_read" in str(excinfo.value)


def test_an_area_annotation_without_a_rectangle_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_annotation(
            action="create", item_key="ATTN2345", annotation_type="image", page=1
        )
    assert "rect" in str(excinfo.value)


def test_an_annotation_without_a_page_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_annotation(action="create", item_key="ATTN2345", text="x")
    assert "page number" in str(excinfo.value)


def test_annotating_an_item_with_no_pdf_says_to_attach_one(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_annotation(action="create", item_key="KAHN3456", text="x", page=1)
    assert "attach one first" in str(excinfo.value)


def test_an_annotation_update_sends_only_what_changed(fake_backend):
    zotero_manage_annotation(action="update", annotation_key="ANNT2345", comment="a better comment")
    _, (_, patch, _) = fake_backend.writes[0]
    assert patch == {"annotationComment": "a better comment"}


def test_an_annotation_update_that_changes_nothing_writes_nothing(fake_backend):
    data = result_data(
        zotero_manage_annotation(
            action="update", annotation_key="ANNT2345", comment="central claim"
        )
    )
    assert data["unchanged"] == ["ANNT2345"]
    assert fake_backend.writes == []


def test_deleting_an_annotation_needs_its_key(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_annotation(action="delete")
    assert "annotation_key" in str(excinfo.value)


def test_an_annotation_is_deleted_when_asked_to(fake_backend):
    zotero_manage_annotation(action="delete", annotation_key="ANNT2345")
    assert fake_backend.writes[0][0] == "delete_items"
