# Copyright 2026 Vinay

"""Toolset gating, resources and prompts.

The gating tests matter more than they look: FastMCP ignores an unknown tool
name silently, so a typo in the toolset table would leave a tool permanently
enabled with nothing to say about it. :func:`validate` is what catches that,
and it is exercised here against the real registry.
"""

from __future__ import annotations

import pytest

from zotero_mcp import toolsets
from zotero_mcp.prompts import find_support, literature_review, summarise_paper, tidy_library
from zotero_mcp.resources import (
    collection_items_resource,
    collections_resource,
    item_resource,
    tags_resource,
)

# -- toolset selection ------------------------------------------------------


def test_no_selection_gives_the_default_groups():
    assert toolsets.parse(None) == set(toolsets.DEFAULT_ON)


def test_an_empty_selection_is_treated_as_unset():
    assert toolsets.parse("   ") == set(toolsets.DEFAULT_ON)


def test_all_turns_on_every_group():
    assert toolsets.parse("all") == set(toolsets.TOOLSETS)


def test_none_leaves_only_the_core():
    assert toolsets.parse("none") == set()


def test_groups_can_be_named_explicitly():
    assert toolsets.parse("scite, duplicates") == {"scite", "duplicates"}


def test_a_group_can_be_subtracted_from_all():
    groups = toolsets.parse("all,-scite,-chatgpt-connector")
    assert "scite" not in groups
    assert "duplicates" in groups


def test_whitespace_separates_names_as_well_as_commas():
    assert toolsets.parse("scite duplicates") == {"scite", "duplicates"}


def test_names_are_case_insensitive():
    assert toolsets.parse("SCITE") == {"scite"}


def test_an_unknown_group_is_warned_about_not_fatal(caplog):
    with caplog.at_level("WARNING"):
        assert toolsets.parse("not-a-group") == set()
    assert "not-a-group" in caplog.text


def test_enabled_tools_expands_group_names():
    assert toolsets.enabled_tools({"duplicates"}) == {"zotero_duplicates"}


def test_every_named_tool_actually_exists_in_the_registry():
    """Guards against drift between the toolset table and the real tools."""
    import asyncio

    import zotero_mcp.tools  # noqa: F401 (registration is a side effect of import)
    from zotero_mcp.app import mcp

    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert toolsets.validate(registered) == []


def test_applying_a_selection_removes_the_groups_that_are_off():
    class FakeProvider:
        def __init__(self):
            self.removed = []

        def remove_tool(self, name):
            self.removed.append(name)

    class FakeServer:
        def __init__(self):
            self.local_provider = FakeProvider()

    server = FakeServer()
    groups = toolsets.apply(server, selection="duplicates")
    assert groups == {"duplicates"}
    assert "zotero_duplicates" not in server.local_provider.removed
    assert "scite_enrich_item" in server.local_provider.removed


def test_the_compat_flag_enables_the_compat_group():
    class FakeServer:
        class local_provider:
            @staticmethod
            def remove_tool(name):
                return None

    assert "compat" in toolsets.apply(FakeServer(), selection="none", compat=True)


def test_a_fastmcp_without_a_removal_api_is_reported_not_ignored(caplog):
    class Bare:
        pass

    with caplog.at_level("WARNING"):
        toolsets.apply(Bare(), selection="none")
    assert "no tool removal API" in caplog.text


def test_the_environment_variable_is_read_when_no_selection_is_passed(monkeypatch):
    class FakeServer:
        class local_provider:
            @staticmethod
            def remove_tool(name):
                return None

    monkeypatch.setenv(toolsets.TOOLSETS_ENV_VAR, "scite")
    assert toolsets.apply(FakeServer()) == {"scite"}


# -- resources --------------------------------------------------------------


def test_the_collections_resource_renders_the_hierarchy(fake_backend):
    text = collections_resource()
    assert "Machine Learning" in text
    assert "TRNS4567" in text


def test_the_item_resource_renders_one_item(fake_backend):
    text = item_resource("ATTN2345")
    assert "Attention Is All You Need" in text


def test_the_item_resource_reports_a_missing_key_rather_than_raising(fake_backend):
    assert "No item with key" in item_resource("ZZZZ2345")


def test_the_collection_items_resource_lists_a_collection(fake_backend):
    assert "Attention Is All You Need" in collection_items_resource("MACH2345")


def test_the_tags_resource_lists_tags(fake_backend):
    assert "psychology" in tags_resource()


@pytest.mark.parametrize(
    ("render", "args"),
    [
        (collections_resource, ()),
        (item_resource, ("ATTN2345",)),
        (collection_items_resource, ("MACH2345",)),
        (tags_resource, ()),
    ],
)
def test_a_resource_renders_an_error_rather_than_raising(render, args):
    """A resource that raises breaks the client's context; one that explains does not."""
    from zotero_mcp.runtime import reset_runtime

    reset_runtime()
    text = render(*args)
    assert "Could not load" in text or "No item" in text


# -- prompts ----------------------------------------------------------------


def test_the_review_prompt_names_the_topic_and_the_tools():
    text = literature_review(topic="attention mechanisms")
    assert "attention mechanisms" in text
    assert "zotero_search" in text


def test_a_deep_review_adds_the_coverage_and_related_steps():
    assert "zotero_coverage" in literature_review(topic="x", depth="deep")
    assert "zotero_coverage" not in literature_review(topic="x", depth="quick")


def test_the_summary_prompt_insists_on_reading_by_range():
    text = summarise_paper(item_key="ATTN2345")
    assert "pages='1-5'" in text
    assert "whole paper in one call" in text


def test_the_tidy_prompt_leaves_dry_run_alone():
    assert "leave dry_run alone" in tidy_library()
    assert "`MACH2345`" in tidy_library(collection="MACH2345")


def test_the_support_prompt_asks_for_contradicting_evidence_too():
    assert "contradict" in find_support(claim="attention beats recurrence")
