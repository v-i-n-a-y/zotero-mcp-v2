"""Write tools: previews mutate nothing, real writes call the backend correctly.

The backend is faked so these stay offline and deterministic — they test the
tools' own logic (dry-run gating, additive tag/collection merges, error
mapping, version reporting), not pyzotero or the network.
"""

from __future__ import annotations

import copy

import pytest
from fastmcp.exceptions import ToolError

from zotero_mcp.config import load_config
from zotero_mcp.models import LibraryRef
from zotero_mcp.tools import register_tools


class FakeMCP:
    """Captures the functions register_tools decorates with @mcp.tool."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class FakeBackend:
    can_write = True
    local = False

    def __init__(self, item: dict | None = None) -> None:
        self._item = item or {
            "key": "AAA11111",
            "version": 10,
            "data": {
                "key": "AAA11111",
                "version": 10,
                "itemType": "journalArticle",
                "title": "Original",
                "tags": [{"tag": "keep"}, {"tag": "old"}],
                "collections": ["COLL0001"],
            },
        }
        self.created: list = []
        self.updated: list = []
        self.trashed: list = []

    def library_ref(self) -> LibraryRef:
        return LibraryRef(library_id="123", library_type="user")

    def write_item(self, key: str) -> dict:
        if key != self._item["key"]:
            from zotero_mcp.errors import NotFound

            raise NotFound("no such item")
        return copy.deepcopy(self._item)

    def item_template(self, item_type: str) -> dict:
        return {"itemType": item_type, "title": "", "creators": [], "tags": [], "collections": []}

    def create_items(self, items: list) -> dict:
        self.created.extend(items)
        return {"successful": {"0": {"key": "NEW00001", "version": 11}}, "failed": {}}

    def update_item(self, item: dict) -> int:
        self.updated.append(copy.deepcopy(item))
        return 99

    def trash_item(self, item: dict) -> None:
        self.trashed.append(item["key"])


def _tools(backend: FakeBackend) -> dict:
    config = load_config(
        use_env=False,
        overrides={"library": {"api_key": "k", "library_id": "123", "mode": "web"}},
    )
    mcp = FakeMCP()
    register_tools(mcp, config, backend)
    return mcp.tools


def _structured(result) -> dict:
    # reply() returns a ToolResult when structured output is on (the default).
    return result.structured_content


# -- registration -----------------------------------------------------------


def test_write_tools_absent_when_backend_cannot_write():
    class RO(FakeBackend):
        can_write = False

    tools = _tools(RO())
    assert "search_library" in tools
    assert "create_item" not in tools
    assert "delete_item" not in tools


def test_write_tools_present_when_writable():
    tools = _tools(FakeBackend())
    for name in (
        "create_item",
        "update_item",
        "delete_item",
        "modify_tags",
        "modify_collections",
        "create_note",
    ):
        assert name in tools


# -- dry-run mutates nothing -------------------------------------------------


def test_create_dry_run_creates_nothing():
    backend = FakeBackend()
    out = _tools(backend)["create_item"](item_type="journalArticle", title="X")
    assert _structured(out)["dry_run"] is True
    assert backend.created == []


def test_update_dry_run_writes_nothing():
    backend = FakeBackend()
    out = _tools(backend)["update_item"](item_key="AAA11111", fields={"title": "New"})
    assert _structured(out)["dry_run"] is True
    assert backend.updated == []


def test_delete_dry_run_trashes_nothing():
    backend = FakeBackend()
    out = _tools(backend)["delete_item"](item_key="AAA11111")
    assert _structured(out)["dry_run"] is True
    assert backend.trashed == []


# -- real writes -------------------------------------------------------------


def test_create_item_real_calls_backend_and_reports_key():
    backend = FakeBackend()
    out = _tools(backend)["create_item"](
        item_type="journalArticle",
        title="Real",
        creators=[{"creator_type": "author", "first_name": "Ada", "last_name": "Lovelace"}],
        tags=["t1"],
        collections=["C1"],
        dry_run=False,
    )
    sc = _structured(out)
    assert sc["created_key"] == "NEW00001"
    assert sc["version"] == 11
    assert len(backend.created) == 1
    made = backend.created[0]
    assert made["title"] == "Real"
    assert made["creators"][0]["lastName"] == "Lovelace"
    assert made["tags"] == [{"tag": "t1"}]
    assert made["collections"] == ["C1"]


def test_create_item_rejects_unknown_type():
    with pytest.raises(ToolError):
        _tools(FakeBackend())["create_item"](item_type="notARealType", title="X", dry_run=False)


def test_update_item_reports_post_write_version():
    backend = FakeBackend()
    out = _tools(backend)["update_item"](
        item_key="AAA11111", fields={"title": "New title"}, dry_run=False
    )
    sc = _structured(out)
    assert sc["version"] == 99  # from backend.update_item, not the stale read version
    assert backend.updated[0]["data"]["title"] == "New title"


def test_update_noop_when_value_unchanged():
    backend = FakeBackend()
    out = _tools(backend)["update_item"](
        item_key="AAA11111", fields={"title": "Original"}, dry_run=False
    )
    assert _structured(out)["unchanged"] == ["AAA11111"]
    assert backend.updated == []


def test_modify_tags_is_additive_and_preserves_others():
    backend = FakeBackend()
    _tools(backend)["modify_tags"](
        item_key="AAA11111", add_tags=["new"], remove_tags=["old"], dry_run=False
    )
    written = {t["tag"] for t in backend.updated[0]["data"]["tags"]}
    assert written == {"keep", "new"}  # "old" removed, "keep" preserved


def test_modify_collections_is_additive_and_preserves_others():
    backend = FakeBackend()
    _tools(backend)["modify_collections"](item_key="AAA11111", add_to=["COLL0002"], dry_run=False)
    assert set(backend.updated[0]["data"]["collections"]) == {"COLL0001", "COLL0002"}


def test_delete_item_trashes():
    backend = FakeBackend()
    out = _tools(backend)["delete_item"](item_key="AAA11111", dry_run=False)
    assert _structured(out)["dry_run"] is False
    assert backend.trashed == ["AAA11111"]


def test_create_note_wraps_plain_text():
    backend = FakeBackend()
    _tools(backend)["create_note"](text="hello", item_key="AAA11111", dry_run=False)
    note = backend.created[0]
    assert note["note"] == "<p>hello</p>"
    assert note["parentItem"] == "AAA11111"


def test_create_note_keeps_html():
    backend = FakeBackend()
    _tools(backend)["create_note"](text="<p>already</p>", dry_run=False)
    assert backend.created[0]["note"] == "<p>already</p>"


def test_missing_item_raises():
    with pytest.raises(ToolError):
        _tools(FakeBackend())["update_item"](
            item_key="NOPE0000", fields={"title": "x"}, dry_run=False
        )


def test_modify_requires_something_to_do():
    with pytest.raises(ToolError):
        _tools(FakeBackend())["modify_tags"](item_key="AAA11111", dry_run=False)
