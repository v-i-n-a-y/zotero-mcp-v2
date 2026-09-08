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


# ---------------------------------------------------------------------------
# RSS feeds, which exist only in the desktop client's own database
# ---------------------------------------------------------------------------


@pytest.fixture
def with_zotero_database(fake_backend, tmp_path):
    """Point the active runtime at a miniature zotero.sqlite."""
    from dataclasses import replace

    from test_localdb import build_database
    from zotero_mcp.config import ZoteroConfig
    from zotero_mcp.runtime import Runtime, set_runtime

    path = build_database(tmp_path / "zotero.sqlite")
    config = ZoteroConfig()
    config = replace(config, library=replace(config.library, sqlite_path=str(path)))
    set_runtime(Runtime(config=config, backend=fake_backend))
    return path


def test_feeds_lists_the_subscriptions(with_zotero_database):
    result = zotero_library(action="feeds")
    text = result_text(result)
    assert "Nature" in text
    assert "https://nature.com/rss" in text
    assert "last error: HTTP 404" in text
    assert {feed["libraryID"] for feed in result_data(result)["feeds"]} == {7, 8}


def test_feed_items_reads_one_feed(with_zotero_database):
    result = zotero_library(action="feed_items", feed_id=7)
    data = result_data(result)
    assert data["feed_id"] == 7
    assert [item["key"] for item in data["items"]] == ["FEEDBBBB", "FEEDAAAA"]
    assert "(unread)" in result_text(result)


def test_feed_items_needs_a_feed_id(with_zotero_database):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="feed_items")
    assert "needs a feed_id" in str(excinfo.value)


def test_feed_items_rejects_something_that_is_not_an_id(with_zotero_database):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="feed_items", feed_id="nature")
    assert "not a feed id" in str(excinfo.value)


def test_an_unknown_feed_id_lists_the_real_ones(with_zotero_database):
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="feed_items", feed_id=99)
    assert "7, 8" in str(excinfo.value)


def test_a_machine_without_zotero_installed_says_so(fake_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_DATA_DIR", str(tmp_path / "no-zotero-here"))
    with pytest.raises(ToolError) as excinfo:
        zotero_library(action="feeds")
    assert "Zotero desktop application" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Item versions, for callers keeping their own copy in step
# ---------------------------------------------------------------------------


def test_versions_reports_every_item(fake_backend):
    data = result_data(zotero_library(action="versions"))
    assert data["versions"] == {key: 1 for key in fake_backend.items}
    assert data["since"] is None


def test_versions_since_a_point_reports_only_what_changed(fake_backend):
    fake_backend.items["KAHN3456"]["version"] = 9
    data = result_data(zotero_library(action="versions", since_version=1))
    assert data["versions"] == {"KAHN3456": 9}
    assert "since 1" in result_text(zotero_library(action="versions", since_version=1))
