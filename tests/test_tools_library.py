# Copyright 2026 Vinay

"""Library-level browsing, statistics and the health report."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data, result_text
from zotero_mcp.tools.library import zotero_health, zotero_library


def test_recent_lists_the_library_newest_first(fake_backend):
    data = result_data(zotero_library(action="recent"))
    assert data["returned"] == 4


def test_by_type_filters_to_one_item_type(fake_backend):
    data = result_data(zotero_library(action="by_type", item_type="book"))
    assert [item["key"] for item in data["items"]] == ["KAHN3456"]


def test_by_type_without_a_type_lists_the_real_ones(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="by_type")
    assert "book" in str(excinfo.value)


def test_by_type_rejects_a_type_that_does_not_exist(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="by_type", item_type="journalarticel")
    assert "not a Zotero item type" in str(excinfo.value)


def test_uncollected_finds_items_filed_nowhere(fake_backend):
    data = result_data(zotero_library(action="uncollected"))
    assert {item["key"] for item in data["items"]} == {"CASE4567", "ATTN9876"}


def test_listing_libraries_marks_the_active_one(fake_backend):
    text = result_text(zotero_library(action="list"))
    assert "**(active)**" in text
    assert "Lab Library" in text


def test_stats_counts_without_enumerating_the_library(fake_backend):
    data = result_data(zotero_library(action="stats"))
    assert data["total_items"] == 4
    assert data["by_item_type"]["journalArticle"] == 2
    assert data["collection_count"] == 3


def test_saved_searches_are_listed(fake_backend):
    text = result_text(zotero_library(action="saved_searches"))
    assert "Unread" in text


def test_switching_without_a_library_id_says_how_to_find_one(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="switch")
    assert "action='list'" in str(excinfo.value)


def test_switching_validates_the_target_before_changing_anything(fake_backend, monkeypatch):
    from conftest import FakeBackend

    monkeypatch.setattr(
        "zotero_mcp.backends.factory.build_backend",
        lambda config: FakeBackend(reachable=False),
    )
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="switch", library_id="98765", library_type="group")
    assert "could not be read" in str(excinfo.value)

    from zotero_mcp.runtime import get_runtime

    assert get_runtime().backend is fake_backend


def test_switching_repoints_the_runtime_when_the_target_is_reachable(fake_backend, monkeypatch):
    from conftest import FakeBackend

    replacement = FakeBackend()
    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", lambda config: replacement)
    text = result_text(zotero_library(action="switch", library_id="98765", library_type="group"))
    assert "Lab Library" in text

    from zotero_mcp.runtime import get_runtime

    assert get_runtime().backend is replacement


def test_paging_through_a_listing(fake_backend):
    first = result_data(zotero_library(action="recent", limit=2))
    assert first["next_cursor"]
    second = result_data(zotero_library(action="recent", limit=2, cursor=first["next_cursor"]))
    assert second["offset"] == 2


# -- health -----------------------------------------------------------------


def test_health_reports_the_backend_and_that_it_is_reachable(fake_backend, no_index):
    data = result_data(zotero_health())
    assert data["backend"] == "fake"
    assert data["reachable"] is True
    assert data["schema_version"]


def test_health_explains_an_unreachable_zotero(monkeypatch, no_index):
    from conftest import FakeBackend
    from zotero_mcp.config import ZoteroConfig
    from zotero_mcp.runtime import Runtime, reset_runtime, set_runtime

    set_runtime(Runtime(config=ZoteroConfig(), backend=FakeBackend(reachable=False)))
    try:
        data = result_data(zotero_health())
        assert data["reachable"] is False
        assert any("not responding" in warning for warning in data["warnings"])
    finally:
        reset_runtime()


def test_health_reports_a_startup_failure_instead_of_failing_itself(monkeypatch, no_index):
    from zotero_mcp.errors import BackendUnavailable
    from zotero_mcp.runtime import reset_runtime, set_startup_error

    reset_runtime()
    set_startup_error(BackendUnavailable("Zotero is not running.", hint="Start Zotero."))
    try:
        data = result_data(zotero_health())
        assert data["reachable"] is False
        assert "Zotero is not running." in data["warnings"]
        assert "Start Zotero." in data["warnings"]
    finally:
        set_startup_error(None)
