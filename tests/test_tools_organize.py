# Copyright 2026 Vinay

"""Collections and tags."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.organize import zotero_collections, zotero_tags

# -- collections ------------------------------------------------------------


def test_listing_collections_resolves_the_hierarchy_into_paths(fake_backend):
    data = result_data(zotero_collections(action="list"))
    paths = {col["key"]: col.get("path") for col in data["collections"]}
    assert paths["TRNS4567"] == "Machine Learning/Transformers"
    assert paths["MACH2345"] == "Machine Learning"


def test_collections_can_be_searched_by_name_or_path(fake_backend):
    data = result_data(zotero_collections(action="search", name="transform"))
    assert [col["key"] for col in data["collections"]] == ["TRNS4567"]


def test_a_search_with_nothing_to_search_for_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_collections(action="search")


def test_listing_a_collections_items(fake_backend):
    data = result_data(zotero_collections(action="items", collection_key="MACH2345"))
    assert [item["key"] for item in data["items"]] == ["ATTN2345"]


def test_listing_items_of_a_collection_that_is_not_there(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_collections(action="items", collection_key="ZZZZ2345")
    assert "action='search'" in str(excinfo.value)


def test_creating_a_collection_previews_where_it_would_go(fake_backend):
    text = result_text(zotero_collections(action="create", name="New Shelf", parent_key="MACH2345"))
    assert "under `MACH2345`" in text
    assert fake_backend.writes == []


def test_creating_a_collection_applies_when_asked(fake_backend):
    zotero_collections(action="create", name="New Shelf", dry_run=False)
    assert fake_backend.writes[0] == ("create_collection", ("New Shelf", None))


def test_creating_a_collection_without_a_name_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_collections(action="create", dry_run=False)


def test_renaming_shows_the_old_and_new_names(fake_backend):
    data = result_data(zotero_collections(action="rename", collection_key="MACH2345", name="ML"))
    change = data["changes"][0]
    assert (change["before"], change["after"]) == ("Machine Learning", "ML")


def test_renaming_applies_when_asked(fake_backend):
    zotero_collections(action="rename", collection_key="MACH2345", name="ML", dry_run=False)
    assert fake_backend.collections["MACH2345"]["data"]["name"] == "ML"


def test_deleting_a_collection_explains_that_items_survive(fake_backend):
    text = result_text(zotero_collections(action="delete", collection_key="MACH2345"))
    assert "stay in the library" in text


def test_deleting_a_collection_applies_when_asked(fake_backend):
    zotero_collections(action="delete", collection_key="TRNS4567", dry_run=False)
    assert "TRNS4567" not in fake_backend.collections


def test_an_action_needing_a_collection_key_says_so(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_collections(action="rename", name="ML")
    assert "collection_key is required" in str(excinfo.value)


def test_items_are_filed_into_a_collection(fake_backend):
    zotero_collections(
        action="add_items", collection_key="TRNS4567", item_keys="KAHN3456", dry_run=False
    )
    assert "TRNS4567" in fake_backend.items["KAHN3456"]["data"]["collections"]


def test_items_are_removed_from_a_collection(fake_backend):
    zotero_collections(
        action="remove_items", collection_key="MACH2345", item_keys="ATTN2345", dry_run=False
    )
    assert fake_backend.items["ATTN2345"]["data"]["collections"] == []


def test_moving_items_with_no_items_named_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_collections(action="add_items", collection_key="MACH2345", dry_run=False)


# -- tags -------------------------------------------------------------------


def test_tags_are_listed_with_their_counts(fake_backend):
    data = result_data(zotero_tags(action="list"))
    assert {t["tag"] for t in data["tags"]} == {"nlp", "transformers", "psychology"}


def test_tags_can_be_filtered(fake_backend):
    data = result_data(zotero_tags(action="list", filter_text="psy"))
    assert [t["tag"] for t in data["tags"]] == ["psychology"]


def test_adding_a_tag_previews_the_per_item_change(fake_backend):
    data = result_data(zotero_tags(action="add", tags="to-read", item_keys="KAHN3456"))
    assert data["dry_run"] is True
    assert data["changes"][0]["after"] == "psychology, to-read"
    assert fake_backend.writes == []


def test_adding_a_tag_applies_when_asked(fake_backend):
    zotero_tags(action="add", tags="to-read", item_keys="KAHN3456", dry_run=False)
    tags = [t["tag"] for t in fake_backend.items["KAHN3456"]["data"]["tags"]]
    assert tags == ["psychology", "to-read"]


def test_adding_a_tag_an_item_already_has_writes_nothing(fake_backend):
    data = result_data(
        zotero_tags(action="add", tags="psychology", item_keys="KAHN3456", dry_run=False)
    )
    assert data["unchanged"] == ["KAHN3456"]
    assert fake_backend.writes == []


def test_removing_a_tag_applies_when_asked(fake_backend):
    zotero_tags(action="remove", tags="nlp", item_keys="ATTN2345", dry_run=False)
    tags = [t["tag"] for t in fake_backend.items["ATTN2345"]["data"]["tags"]]
    assert tags == ["transformers"]


def test_renaming_a_tag_finds_its_items_by_itself(fake_backend):
    zotero_tags(action="rename", tags="nlp", new_name="language", dry_run=False)
    tags = [t["tag"] for t in fake_backend.items["ATTN2345"]["data"]["tags"]]
    assert "language" in tags
    assert "nlp" not in tags


def test_renaming_needs_a_new_name(fake_backend):
    with pytest.raises(ToolError):
        zotero_tags(action="rename", tags="nlp")


def test_renaming_is_one_tag_at_a_time(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_tags(action="rename", tags="nlp, transformers", new_name="x")
    assert "one tag at a time" in str(excinfo.value)


def test_a_tag_change_with_no_tags_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_tags(action="add", item_keys="KAHN3456")


def test_a_tag_change_with_no_items_is_refused(fake_backend):
    with pytest.raises(ToolError):
        zotero_tags(action="add", tags="x")


def test_one_rejected_item_does_not_lose_the_rest_of_the_batch(fake_backend, monkeypatch):
    real = fake_backend.update_item

    def flaky(key, patch, *, version):
        if key == "KAHN3456":
            raise RuntimeError("Zotero said no")
        return real(key, patch, version=version)

    fake_backend.update_item = flaky
    data = result_data(
        zotero_tags(action="add", tags="batch", item_keys="ATTN2345, KAHN3456", dry_run=False)
    )
    assert data["succeeded"] == ["ATTN2345"]
    assert "KAHN3456" in data["failed"]
