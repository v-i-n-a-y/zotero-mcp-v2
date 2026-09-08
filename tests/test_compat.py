# Copyright 2026 Vinay

"""The pre-1.0 tool names.

The promise this layer makes is narrow and testable: an installation calling
the old tools keeps working. So these tests assert that each alias reaches the
consolidated tool with the right arguments, and that the two names that exist
on both surfaces accept either argument shape.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp import compat


def test_every_legacy_name_is_registered():
    from zotero_mcp.app import mcp

    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert registered >= compat.LEGACY_TOOL_NAMES


def test_the_legacy_names_are_the_ones_the_predecessor_shipped():
    """Fifty-one names, which is the fork's fifty-seven less its six unchanged ones."""
    assert len(compat.LEGACY_TOOL_NAMES) == 52


def test_every_alias_says_what_replaced_it():
    from zotero_mcp.app import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    aliased = compat.LEGACY_TOOL_NAMES - {"zotero_update_item", "zotero_get_annotations"}
    for name in aliased:
        assert "Prefer" in tools[name].description, name


# -- searching --------------------------------------------------------------


def test_search_items_reaches_the_search_tool(fake_backend, no_index):
    data = result_data(compat.zotero_search_items(query="Kahneman"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_everything_mode_maps_to_a_full_text_search(fake_backend, no_index):
    data = result_data(compat.zotero_search_items(query="transduction", qmode="everything"))
    assert data["diagnostics"]["strategy"] == "full text"


def test_search_by_tag_filters_on_the_tag(fake_backend, no_index):
    data = result_data(compat.zotero_search_by_tag(tag="psychology"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_search_by_citation_key_is_an_exact_match(fake_backend, no_index):
    data = result_data(compat.zotero_search_by_citation_key(citekey="vaswani2017attention"))
    assert data["items"][0]["key"] == "ATTN2345"


def test_semantic_search_passes_its_filters_through(fake_backend, monkeypatch):
    seen: dict = {}

    def capture(query, *, limit, collection_key=None):
        seen.update({"limit": limit, "collection_key": collection_key})
        return []

    monkeypatch.setattr("zotero_mcp.index.query.search", capture)
    compat.zotero_semantic_search(
        query="attention", limit=5, filters={"collection_key": "MACH2345"}
    )
    assert seen == {"limit": 5, "collection_key": "MACH2345"}


def test_advanced_search_translates_the_conditions_it_understands(fake_backend, no_index):
    data = result_data(
        compat.zotero_advanced_search(
            conditions=[
                {"condition": "creator", "value": "Vaswani"},
                {"condition": "tag", "value": "nlp"},
            ]
        )
    )
    assert [item["key"] for item in data["items"]] == ["ATTN2345"]


def test_advanced_search_refuses_a_condition_it_cannot_run(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_advanced_search(conditions=[{"condition": "annotationText", "value": "x"}])
    assert "Cannot search on: annotationText" in str(excinfo.value)


def test_advanced_search_will_not_pretend_to_or_conditions(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_advanced_search(
            conditions=[{"condition": "title", "value": "x"}], join_mode="any"
        )
    assert "one zotero_search per condition" in str(excinfo.value)


def test_advanced_search_rejects_an_unknown_join_mode(fake_backend, no_index):
    with pytest.raises(ToolError):
        compat.zotero_advanced_search(conditions=[], join_mode="maybe")


def test_advanced_search_with_nothing_usable_is_refused(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_advanced_search(conditions=[])
    assert "No usable conditions" in str(excinfo.value)


def test_note_search_looks_only_at_notes(fake_backend, no_index):
    data = result_data(compat.zotero_search_notes(query="self-attention"))
    assert [item["key"] for item in data["items"]] == ["NTEA2345"]


def test_the_search_database_status_alias_reports_the_index(fake_backend, no_index):
    assert "zotero-mcp index build" in result_text(compat.zotero_get_search_database_status())


def test_a_forced_rebuild_is_sent_to_the_command_line(fake_backend, no_index):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_update_search_database(force_rebuild=True)
    assert "zotero-mcp index build" in str(excinfo.value)


def test_an_incremental_update_reaches_the_index_tool(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.builder.update_index",
        lambda runtime, limit: {"indexed": 1, "chunks": 1, "skipped": 0, "errors": []},
    )
    assert result_data(compat.zotero_update_search_database())["indexed"] == 1


# -- retrieval --------------------------------------------------------------


def test_item_metadata_comes_back_with_children_and_collections(fake_backend):
    data = result_data(compat.zotero_get_item_metadata(item_key="ATTN2345"))
    assert data["attachments"][0]["key"] == "PDFA2345"
    assert data["collections"][0]["name"] == "Machine Learning"


def test_the_children_aliases_both_work(fake_backend):
    assert result_data(compat.zotero_get_item_children(item_key="ATTN2345"))["note_count"] == 1
    data = result_data(compat.zotero_get_items_children(item_keys="ATTN2345, KAHN3456"))
    assert len(data["items"]) == 2


def test_fulltext_reads_the_attachment(fake_backend):
    fake_backend.fulltext["PDFA2345"] = "Body text."
    assert "Body text." in result_data(compat.zotero_get_item_fulltext(item_key="ATTN2345"))["text"]


def test_the_outline_alias_asks_for_an_outline(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_get_pdf_outline(item_key="ATTN2345")
    assert "not available on this machine" in str(excinfo.value)


def test_the_notes_alias_returns_notes_only(fake_backend):
    data = result_data(compat.zotero_get_notes(item_key="ATTN2345"))
    assert data["annotations"] == []
    assert data["notes"][0]["key"] == "NTEA2345"


def test_the_library_browsing_aliases(fake_backend):
    assert result_data(compat.zotero_get_recent())["returned"] == 4
    assert result_data(compat.zotero_get_items_by_type(item_type="book"))["returned"] == 1
    assert result_data(compat.zotero_get_items_without_collection())["returned"] == 2
    assert result_data(compat.zotero_get_library_stats())["total_items"] == 4
    assert "Lab Library" in result_text(compat.zotero_list_libraries())


def test_item_versions_are_reported(fake_backend):
    data = result_data(compat.zotero_get_item_versions())
    assert data["versions"]["ATTN2345"] == 1


def test_switching_library_reaches_the_library_tool(fake_backend, monkeypatch):
    from conftest import FakeBackend

    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", lambda config: FakeBackend())
    assert "Lab Library" in result_text(compat.zotero_switch_library(library_id="98765"))


def test_the_trash_alias_lists_the_trash(fake_backend):
    from zotero_mcp.tools.write import zotero_manage_items

    zotero_manage_items(action="trash", item_keys="KAHN3456", dry_run=False)
    assert result_data(compat.zotero_get_trash())["items"][0]["key"] == "KAHN3456"


def test_the_bibtex_alias_exports_bibtex(fake_backend, monkeypatch):
    monkeypatch.setattr("zotero_mcp.external.bibtex.from_better_bibtex", lambda keys: None)
    data = result_data(compat.zotero_export_bibtex(item_keys="ATTN2345"))
    assert data["format"] == "bibtex"


# -- collections and tags ---------------------------------------------------


def test_the_collection_aliases_read(fake_backend):
    assert len(result_data(compat.zotero_get_collections())["collections"]) == 3
    found = result_data(compat.zotero_search_collections(query="transform"))
    assert found["collections"][0]["key"] == "TRNS4567"
    items = result_data(compat.zotero_get_collection_items(collection_key="MACH2345"))
    assert items["items"][0]["key"] == "ATTN2345"


def test_the_collection_aliases_write_without_a_dry_run(fake_backend):
    compat.zotero_create_collection(name="New Shelf")
    assert fake_backend.writes[0] == ("create_collection", ("New Shelf", None))

    compat.zotero_rename_collection(collection_key="MACH2345", new_name="ML")
    assert fake_backend.collections["MACH2345"]["data"]["name"] == "ML"


def test_deleting_a_collection_still_needs_confirmation(fake_backend):
    compat.zotero_delete_collection(collection_key="TRNS4567")
    assert "TRNS4567" in fake_backend.collections

    compat.zotero_delete_collection(collection_key="TRNS4567", confirm=True)
    assert "TRNS4567" not in fake_backend.collections


def test_managing_collections_adds_and_removes(fake_backend):
    compat.zotero_manage_collections(
        item_keys="ATTN2345", add_to="TRNS4567", remove_from="MACH2345"
    )
    assert fake_backend.items["ATTN2345"]["data"]["collections"] == ["TRNS4567"]


def test_managing_collections_with_nothing_to_do_is_refused(fake_backend):
    with pytest.raises(ToolError):
        compat.zotero_manage_collections(item_keys="ATTN2345")


def test_batch_tagging_applies_to_the_query_results(fake_backend):
    compat.zotero_batch_update_tags(query="Kahneman", add_tags="to-read")
    tags = [t["tag"] for t in fake_backend.items["KAHN3456"]["data"]["tags"]]
    assert tags == ["psychology", "to-read"]


def test_batch_tagging_can_remove(fake_backend):
    compat.zotero_batch_update_tags(query="Attention", remove_tags="nlp")
    tags = [t["tag"] for t in fake_backend.items["ATTN2345"]["data"]["tags"]]
    assert "nlp" not in tags


def test_batch_tagging_a_query_that_matches_nothing_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_batch_update_tags(query="no such thing", add_tags="x")
    assert "matched no items" in str(excinfo.value)


def test_batch_tagging_with_no_tags_is_refused(fake_backend):
    with pytest.raises(ToolError):
        compat.zotero_batch_update_tags(query="Kahneman")


def test_the_tag_listing_alias(fake_backend):
    assert len(result_data(compat.zotero_get_tags())["tags"]) == 3


# -- writing ----------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.external.metadata.fetch",
        lambda parsed, settings: {"itemType": "journalArticle", "title": "Fetched"},
    )
    monkeypatch.setattr("zotero_mcp.external.openaccess.find_pdf", lambda **k: None)


def test_adding_by_doi_reaches_the_add_tool(fake_backend, offline):
    data = result_data(compat.zotero_add_by_doi(doi="10.1000/brand-new"))
    assert data["created_key"].startswith("NEW")


def test_attach_mode_none_switches_the_pdf_lookup_off(fake_backend, offline, monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("no open-access lookup should have been attempted")

    monkeypatch.setattr("zotero_mcp.external.openaccess.find_pdf", refuse)
    compat.zotero_add_by_doi(doi="10.1000/brand-new", attach_mode="none")


def test_adding_by_url_reaches_the_add_tool(fake_backend, offline):
    data = result_data(compat.zotero_add_by_url(url="https://example.invalid/paper"))
    assert data["created_key"].startswith("NEW")


def test_adding_from_a_file_carries_the_title(fake_backend, tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4")
    compat.zotero_add_from_file(file_path=str(path), title="My Paper", item_type="book")
    created = fake_backend.writes[0][1][0][0]
    assert created["title"] == "My Paper"


def test_changing_item_type_applies_immediately(fake_backend):
    compat.zotero_change_item_type(item_key="ATTN2345", new_type="book", date="2018")
    assert fake_backend.items["ATTN2345"]["data"]["itemType"] == "book"


def test_the_trash_aliases_apply_without_a_dry_run(fake_backend):
    compat.zotero_trash_items(item_keys="KAHN3456")
    assert "KAHN3456" in fake_backend.trash
    compat.zotero_restore_from_trash(item_keys="KAHN3456")
    assert fake_backend.trash == {}


def test_deletion_still_needs_confirmation(fake_backend):
    compat.zotero_delete_items(item_keys="KAHN3456")
    assert "KAHN3456" in fake_backend.items
    compat.zotero_delete_items(item_keys="KAHN3456", confirm=True)
    assert "KAHN3456" not in fake_backend.items


def test_emptying_the_trash_still_needs_confirmation(fake_backend):
    compat.zotero_trash_items(item_keys="KAHN3456")
    compat.zotero_empty_trash()
    assert fake_backend.trash
    compat.zotero_empty_trash(confirm=True)
    assert fake_backend.trash == {}


def test_copying_to_another_library_creates_new_items(fake_backend, monkeypatch):
    from conftest import FakeBackend

    target = FakeBackend(items=[])
    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", lambda config: target)
    result_data(
        compat.zotero_copy_items_to_library(item_keys="ATTN2345", target_library_id="98765")
    )
    assert target.writes[0][0] == "create_items"
    payload = target.writes[0][1][0][0]
    assert payload["title"] == "Attention Is All You Need"
    assert "version" not in payload
    assert "ATTN2345" in fake_backend.items


def test_copying_needs_a_target(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_copy_items_to_library(item_keys="ATTN2345")
    assert "target_library_id" in str(excinfo.value)


def test_the_duplicate_aliases(fake_backend):
    found = result_data(compat.zotero_find_duplicates())
    assert found["groups"]

    compat.zotero_merge_duplicates(keeper_key="ATTN2345", duplicate_keys="ATTN9876", confirm=True)
    assert "ATTN9876" in fake_backend.trash


def test_merging_without_confirming_only_previews(fake_backend):
    compat.zotero_merge_duplicates(keeper_key="ATTN2345", duplicate_keys="ATTN9876")
    assert fake_backend.writes == []


# -- notes and annotations --------------------------------------------------


def test_the_note_title_becomes_a_heading(fake_backend):
    compat.zotero_create_note(item_key="ATTN2345", note_title="My Thoughts", note_text="Something.")
    body = fake_backend.writes[0][1][0][0]["note"]
    assert body.startswith("<h1>My Thoughts</h1>")


def test_the_note_aliases_update_and_delete(fake_backend):
    compat.zotero_update_note(item_key="NTEA2345", note_text="Replaced.")
    assert fake_backend.writes[0][1][1]["note"] == "<p>Replaced.</p>"

    compat.zotero_delete_note(item_key="NTEA2345")
    assert fake_backend.writes[-1][0] == "delete_items"


def test_creating_an_annotation_by_attachment_key(fake_backend):
    compat.zotero_create_annotation(attachment_key="PDFA2345", page=2, text="a passage")
    payload = fake_backend.writes[0][1][0][0]
    assert payload["parentItem"] == "PDFA2345"
    assert payload["annotationType"] == "highlight"


def test_an_annotation_with_only_a_comment_is_a_sticky_note(fake_backend):
    compat.zotero_create_annotation(attachment_key="PDFA2345", page=2, comment="a thought")
    assert fake_backend.writes[0][1][0][0]["annotationType"] == "note"


def test_an_area_annotation_becomes_a_rectangle(fake_backend):
    compat.zotero_create_area_annotation(
        attachment_key="PDFA2345", page=1, x=10, y=20, width=100, height=50
    )
    payload = fake_backend.writes[0][1][0][0]
    assert payload["annotationType"] == "image"
    assert payload["annotationPosition"]["rects"] == [[10.0, 20.0, 110.0, 70.0]]


# -- the two names on both surfaces ----------------------------------------


def test_the_update_alias_accepts_the_current_argument_shape(fake_backend):
    data = result_data(
        compat.zotero_update_item(item_key="KAHN3456", fields={"title": "New"}, dry_run=False)
    )
    assert data["succeeded"] == ["KAHN3456"]
    assert fake_backend.items["KAHN3456"]["data"]["title"] == "New"


def test_the_update_alias_accepts_the_legacy_scalar_arguments(fake_backend):
    compat.zotero_update_item(
        item_key="KAHN3456",
        title="New",
        date="2012",
        abstract="An abstract.",
        doi="10.1000/x",
        dry_run=False,
    )
    data = fake_backend.items["KAHN3456"]["data"]
    assert (data["title"], data["date"], data["DOI"]) == ("New", "2012", "10.1000/x")
    assert data["abstractNote"] == "An abstract."


def test_the_legacy_tags_argument_replaces_the_whole_list(fake_backend):
    compat.zotero_update_item(item_key="ATTN2345", tags=["only-this"], dry_run=False)
    assert [t["tag"] for t in fake_backend.items["ATTN2345"]["data"]["tags"]] == ["only-this"]


def test_the_legacy_collections_argument_replaces_the_whole_list(fake_backend):
    compat.zotero_update_item(item_key="ATTN2345", collections=["BEHV3456"], dry_run=False)
    assert fake_backend.items["ATTN2345"]["data"]["collections"] == ["BEHV3456"]


def test_collections_can_still_be_named_rather_than_keyed(fake_backend):
    compat.zotero_update_item(item_key="KAHN3456", collection_names=["Transformers"], dry_run=False)
    assert "TRNS4567" in fake_backend.items["KAHN3456"]["data"]["collections"]


def test_an_unknown_collection_name_says_how_to_find_the_key(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_update_item(
            item_key="KAHN3456", collection_names=["No Such Shelf"], dry_run=False
        )
    assert "action='search'" in str(excinfo.value)


def test_an_ambiguous_collection_name_is_refused_rather_than_guessed(fake_backend):
    fake_backend.collections["DUPE2345"] = {
        "key": "DUPE2345",
        "version": 1,
        "data": {"key": "DUPE2345", "name": "Transformers", "parentCollection": False},
    }
    with pytest.raises(ToolError) as excinfo:
        compat.zotero_update_item(
            item_key="KAHN3456", collection_names=["Transformers"], dry_run=False
        )
    assert "More than one collection" in str(excinfo.value)


def test_the_annotations_alias_ignores_the_arguments_that_no_longer_apply(fake_backend):
    data = result_data(
        compat.zotero_get_annotations(item_key="ATTN2345", use_pdf_extraction=True, limit=5)
    )
    assert [a["key"] for a in data["annotations"]] == ["ANNT2345"]
