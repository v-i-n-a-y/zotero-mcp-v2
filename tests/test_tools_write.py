# Copyright 2026 Vinay

"""Adding, updating and trashing items.

Every write tool previews before it acts, so most of these tests assert on
what the backend was *not* asked to do. The duplicate check and the field
routing are the two behaviours that stop an assistant quietly corrupting a
library, so they get the most attention.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.write import zotero_add_item, zotero_manage_items, zotero_update_item


@pytest.fixture
def offline(monkeypatch):
    """No metadata lookup leaves this test suite."""
    monkeypatch.setattr(
        "zotero_mcp.external.metadata.fetch",
        lambda parsed, settings: {"itemType": "journalArticle", "title": "Fetched Title"},
    )


# -- adding -----------------------------------------------------------------


def test_adding_nothing_is_refused_with_the_three_ways_to_add(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item()
    assert "file_path" in str(excinfo.value)


def test_a_doi_already_in_the_library_is_not_added_twice(fake_backend, offline):
    data = result_data(zotero_add_item(identifier="10.48550/arxiv.1706.03762"))
    assert data["unchanged"]
    assert fake_backend.writes == []


def test_an_item_key_is_not_an_identifier_to_import(fake_backend, offline):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(identifier="ATTN2345")
    assert "already a Zotero item key" in str(excinfo.value)


def test_a_new_doi_is_fetched_and_created(fake_backend, offline):
    data = result_data(zotero_add_item(identifier="10.1000/brand-new", attach_pdf=False))
    assert data["created_key"].startswith("NEW")
    assert fake_backend.writes[0][0] == "create_items"
    created = fake_backend.writes[0][1][0][0]
    assert created["title"] == "Fetched Title"


def test_a_dry_run_resolves_metadata_without_creating_anything(fake_backend, offline):
    data = result_data(zotero_add_item(identifier="10.1000/brand-new", dry_run=True))
    assert data["dry_run"] is True
    assert fake_backend.writes == []
    assert any(change["field"] == "title" for change in data["changes"])


def test_generic_field_names_are_routed_to_the_item_types_real_field(fake_backend):
    result_data(
        zotero_add_item(
            item_type="case",
            fields={"title": "Brown v. Board of Education", "date": "1954"},
            attach_pdf=False,
        )
    )
    created = fake_backend.writes[0][1][0][0]
    assert created["caseName"] == "Brown v. Board of Education"
    assert created["dateDecided"] == "1954"
    assert "title" not in created


def test_a_field_the_item_type_cannot_hold_is_reported_not_dropped(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"nonsenseField": "x"}, attach_pdf=False)
    assert "nonsenseField" in str(excinfo.value)


def test_an_unknown_item_type_lists_real_ones(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="preprintish", fields={"title": "x"})
    assert "bookSection" in str(excinfo.value)


def test_tags_and_collections_are_applied_at_creation(fake_backend):
    result_data(
        zotero_add_item(
            item_type="book",
            fields={"title": "A Book"},
            tags="one, two",
            collections="MACH2345",
            attach_pdf=False,
        )
    )
    created = fake_backend.writes[0][1][0][0]
    assert [t["tag"] for t in created["tags"]] == ["one", "two"]
    assert created["collections"] == ["MACH2345"]


def test_a_read_only_backend_refuses_the_write_rather_than_attempting_it(offline):
    from conftest import FakeBackend
    from zotero_mcp.config import ZoteroConfig
    from zotero_mcp.runtime import Runtime, reset_runtime, set_runtime

    backend = FakeBackend(writable=False)
    set_runtime(Runtime(config=ZoteroConfig(), backend=backend))
    try:
        with pytest.raises(ToolError) as excinfo:
            zotero_add_item(item_type="book", fields={"title": "A Book"}, attach_pdf=False)
        assert "ZOTERO_API_KEY" in str(excinfo.value)
    finally:
        reset_runtime()


# -- updating ---------------------------------------------------------------


def test_an_update_previews_before_and_after_by_default(fake_backend):
    data = result_data(zotero_update_item(item_key="KAHN3456", fields={"title": "New Title"}))
    assert data["dry_run"] is True
    change = next(c for c in data["changes"] if c["field"] == "title")
    assert change["before"] == "Thinking, Fast and Slow"
    assert change["after"] == "New Title"
    assert fake_backend.writes == []


def test_an_applied_update_sends_only_the_changed_fields(fake_backend):
    result_data(
        zotero_update_item(item_key="KAHN3456", fields={"title": "New Title"}, dry_run=False)
    )
    name, (key, patch, version) = fake_backend.writes[0]
    assert name == "update_item"
    assert key == "KAHN3456"
    assert patch == {"title": "New Title"}
    assert version == 1


def test_an_update_that_changes_nothing_says_so_and_writes_nothing(fake_backend):
    data = result_data(
        zotero_update_item(
            item_key="KAHN3456", fields={"title": "Thinking, Fast and Slow"}, dry_run=False
        )
    )
    assert data["unchanged"] == ["KAHN3456"]
    assert fake_backend.writes == []


def test_tags_are_merged_rather_than_replaced(fake_backend):
    result_data(
        zotero_update_item(item_key="ATTN2345", add_tags="rag", remove_tags="nlp", dry_run=False)
    )
    _, (_, patch, _) = fake_backend.writes[0]
    assert [t["tag"] for t in patch["tags"]] == ["transformers", "rag"]


def test_collections_are_merged_rather_than_replaced(fake_backend):
    result_data(zotero_update_item(item_key="ATTN2345", add_collections="TRNS4567", dry_run=False))
    _, (_, patch, _) = fake_backend.writes[0]
    assert patch["collections"] == ["MACH2345", "TRNS4567"]


def test_changing_item_type_names_the_fields_that_will_be_lost(fake_backend):
    data = result_data(zotero_update_item(item_key="ATTN2345", change_item_type="book"))
    dropped = {c["field"] for c in data["changes"] if c["field"].endswith("(dropped)")}
    assert "publicationTitle (dropped)" in dropped


def test_changing_to_an_unknown_item_type_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_update_item(item_key="ATTN2345", change_item_type="notAType")


def test_a_stale_version_surfaces_as_a_conflict(fake_backend):
    fake_backend.items["KAHN3456"]["version"] = 7
    fake_backend.items["KAHN3456"]["data"]["version"] = 7

    original = fake_backend.get_item

    def stale(key):
        item = original(key)
        if item and key == "KAHN3456":
            item["version"] = 6
            item["data"]["version"] = 6
        return item

    fake_backend.get_item = stale
    with pytest.raises(ToolError) as excinfo:
        zotero_update_item(item_key="KAHN3456", fields={"title": "Race"}, dry_run=False)
    assert "conflict" in str(excinfo.value).lower() or "stale" in str(excinfo.value).lower()


# -- trashing ---------------------------------------------------------------


def test_trashing_previews_the_titles_it_would_move(fake_backend):
    text = result_text(zotero_manage_items(action="trash", item_keys="KAHN3456"))
    assert "Thinking, Fast and Slow" in text
    assert fake_backend.writes == []


def test_delete_warns_that_it_is_permanent(fake_backend):
    text = result_text(zotero_manage_items(action="delete", item_keys="KAHN3456"))
    assert "cannot be undone" in text


def test_trashing_applies_when_asked_to(fake_backend):
    result_data(zotero_manage_items(action="trash", item_keys="KAHN3456", dry_run=False))
    assert fake_backend.writes[0][0] == "trash_items"
    assert "KAHN3456" in fake_backend.trash


def test_restoring_takes_an_item_back_out_of_the_trash(fake_backend):
    zotero_manage_items(action="trash", item_keys="KAHN3456", dry_run=False)
    zotero_manage_items(action="restore", item_keys="KAHN3456", dry_run=False)
    assert fake_backend.trash == {}


def test_the_trash_can_be_listed(fake_backend):
    zotero_manage_items(action="trash", item_keys="KAHN3456", dry_run=False)
    data = result_data(zotero_manage_items(action="list_trash"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_emptying_the_trash_counts_first(fake_backend):
    zotero_manage_items(action="trash", item_keys="KAHN3456", dry_run=False)
    text = result_text(zotero_manage_items(action="empty_trash"))
    assert "1 item(s)" in text
    zotero_manage_items(action="empty_trash", dry_run=False)
    assert fake_backend.trash == {}


def test_an_action_with_no_items_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_items(action="trash", item_keys="")
    assert "No items" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Copying items into another library
# ---------------------------------------------------------------------------


@pytest.fixture
def target_library(monkeypatch):
    """A second in-memory library, standing in for a group."""
    from conftest import FakeBackend

    target = FakeBackend(items=[])
    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", lambda config: target)
    return target


def test_copying_previews_before_it_writes(fake_backend, target_library):
    text = result_text(
        zotero_manage_items(action="copy", item_keys="ATTN2345", target_library_id="98765")
    )
    assert "Would copy 1 item(s) into **Lab Library**" in text
    assert "not carried across" in text
    assert target_library.writes == []


def test_copying_creates_new_items_in_the_target(fake_backend, target_library):
    result = zotero_manage_items(
        action="copy", item_keys="ATTN2345,KAHN3456", target_library_id="98765", dry_run=False
    )
    action, (payloads,) = target_library.writes[0]
    assert action == "create_items"
    assert [p["title"] for p in payloads] == [
        "Attention Is All You Need",
        "Thinking, Fast and Slow",
    ]
    assert result_data(result)["succeeded"] == ["NEWA2345", "NEWB2345"]


def test_a_copy_carries_no_identity_from_the_original(fake_backend, target_library):
    zotero_manage_items(
        action="copy", item_keys="ATTN2345", target_library_id="98765", dry_run=False
    )
    payload = target_library.writes[0][1][0][0]
    assert not {"key", "version", "dateAdded", "dateModified", "relations"} & set(payload)
    assert payload["creators"][0]["lastName"] == "Vaswani"


def test_a_copy_can_be_filed_straight_into_a_collection(fake_backend, target_library):
    zotero_manage_items(
        action="copy",
        item_keys="ATTN2345",
        target_library_id="98765",
        target_collection_key="MACH2345",
        dry_run=False,
    )
    assert target_library.writes[0][1][0][0]["collections"] == ["MACH2345"]


def test_a_copy_is_not_filed_in_the_originals_collections(fake_backend, target_library):
    """Collection keys mean nothing in another library, so they are dropped."""
    zotero_manage_items(
        action="copy", item_keys="ATTN2345", target_library_id="98765", dry_run=False
    )
    assert target_library.writes[0][1][0][0]["collections"] == []


def test_copying_needs_a_target(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_items(action="copy", item_keys="ATTN2345")
    assert "needs a target_library_id" in str(excinfo.value)


def test_copying_into_the_open_library_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_items(action="copy", item_keys="ATTN2345", target_library_id="12345")
    assert "already open" in str(excinfo.value)


def test_a_target_that_cannot_be_reached_is_reported(fake_backend, monkeypatch):
    from conftest import FakeBackend

    monkeypatch.setattr(
        "zotero_mcp.backends.factory.build_backend",
        lambda config: FakeBackend(items=[], reachable=False),
    )
    with pytest.raises(ToolError) as excinfo:
        zotero_manage_items(
            action="copy", item_keys="ATTN2345", target_library_id="98765", dry_run=False
        )
    assert "could not be reached" in str(excinfo.value)


def test_an_unlisted_target_is_assumed_to_be_a_group(fake_backend, target_library, monkeypatch):
    seen = {}
    from zotero_mcp.backends import factory

    monkeypatch.setattr(
        factory,
        "build_backend",
        lambda config: seen.setdefault("config", config) and target_library,
    )
    zotero_manage_items(
        action="copy", item_keys="ATTN2345", target_library_id="55555", dry_run=False
    )
    assert seen["config"].library.library_type.value == "group"
