# Copyright 2026 Vinay

"""Shared fixtures.

The Zotero client stand-in deliberately reproduces the two pyzotero behaviours
the backend has to work around, rather than presenting an idealised API: if the
fake did not accumulate parameters, the test that proves we clear them would
pass against a broken implementation.
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeResponse:
    """Stands in for the ``httpx`` response pyzotero keeps on the client."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class FakeZoteroClient:
    """Minimal pyzotero client that mimics parameter accumulation.

    ``add_parameters`` in pyzotero merges into ``self.url_params`` instead of
    replacing it, so filters leak from one call into the next. That is
    reproduced here so the backend's mitigation is genuinely exercised.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.library_id = kwargs.get("library_id")
        self.library_type = kwargs.get("library_type", "user")
        self.api_key = kwargs.get("api_key")
        self.local = kwargs.get("local", False)

        self.url_params: dict[str, Any] | None = None
        self.request = FakeResponse()

        #: (method_name, args, effective_url_params) for every call made.
        self.calls: list[tuple[str, tuple, dict[str, Any]]] = []

        #: Canned data the tests populate.
        self.items_result: list[dict] = []
        self.total_results: str | None = None
        self.raises: dict[str, Exception] = {}
        self.responses: dict[str, Any] = {}

    # -- pyzotero's own parameter handling, faithfully wrong ---------------
    def add_parameters(self, **params: Any) -> None:
        preserved = dict(self.url_params or {})
        params.setdefault("format", "json")
        self.url_params = {**preserved, **params}

    def _record(self, name: str, args: tuple, params: dict[str, Any]) -> Any:
        self.add_parameters(**params)
        self.calls.append((name, args, dict(self.url_params or {})))
        if name in self.raises:
            raise self.raises[name]
        self.request = FakeResponse(
            {"Total-Results": self.total_results} if self.total_results is not None else {}
        )
        return self.responses.get(name, self.items_result)

    # -- the surface the backend uses --------------------------------------
    def items(self, **params: Any) -> Any:
        return self._record("items", (), params)

    def top(self, **params: Any) -> Any:
        return self._record("top", (), params)

    def trash(self, **params: Any) -> Any:
        return self._record("trash", (), params)

    def item(self, key: str, **params: Any) -> Any:
        return self._record("item", (key,), params)

    def children(self, key: str, **params: Any) -> Any:
        return self._record("children", (key,), params)

    def collection(self, key: str, **params: Any) -> Any:
        return self._record("collection", (key,), params)

    def collections(self, **params: Any) -> Any:
        return self._record("collections", (), params)

    def collections_sub(self, key: str, **params: Any) -> Any:
        return self._record("collections_sub", (key,), params)

    def all_collections(self, **params: Any) -> Any:
        return self._record("all_collections", (), params)

    def collection_items(self, key: str, **params: Any) -> Any:
        return self._record("collection_items", (key,), params)

    def tags(self, **params: Any) -> Any:
        return self._record("tags", (), params)

    def item_versions(self, **params: Any) -> Any:
        return self._record("item_versions", (), params)

    def count_items(self, **params: Any) -> Any:
        return self._record("count_items", (), params)

    def fulltext_item(self, key: str, **params: Any) -> Any:
        return self._record("fulltext_item", (key,), params)

    def file(self, key: str, **params: Any) -> Any:
        return self._record("file", (key,), params)

    def groups(self, **params: Any) -> Any:
        return self._record("groups", (), params)

    def searches(self, **params: Any) -> Any:
        return self._record("searches", (), params)

    def item_template(self, item_type: str, **params: Any) -> Any:
        return self._record("item_template", (item_type,), params)

    def create_items(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("create_items", (payload, *args), params)

    def update_item(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("update_item", (payload, *args), params)

    def delete_item(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("delete_item", (payload, *args), params)

    def create_collections(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("create_collections", (payload, *args), params)

    def update_collection(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("update_collection", (payload, *args), params)

    def delete_collection(self, payload, *args: Any, **params: Any) -> Any:
        return self._record("delete_collection", (payload, *args), params)

    def addto_collection(self, collection: str, payload, **params: Any) -> Any:
        return self._record("addto_collection", (collection, payload), params)

    def deletefrom_collection(self, collection: str, payload, **params: Any) -> Any:
        return self._record("deletefrom_collection", (collection, payload), params)

    def attachment_simple(self, files, parentid=None, **params: Any) -> Any:
        return self._record("attachment_simple", (files, parentid), params)


@pytest.fixture
def fake_zotero(monkeypatch):
    """Patch ``pyzotero.zotero.Zotero`` and hand back the constructed client."""
    created: list[FakeZoteroClient] = []

    def factory(**kwargs: Any) -> FakeZoteroClient:
        client = FakeZoteroClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr("zotero_mcp.backends.pyzotero_backend.zotero.Zotero", factory)
    return created


# ---------------------------------------------------------------------------
# An in-memory library, for exercising the tool layer end to end
# ---------------------------------------------------------------------------


def _item(key, item_type="journalArticle", **fields):
    data = {"key": key, "itemType": item_type, "version": 1, **fields}
    return {"key": key, "version": 1, "data": data, "meta": {}}


LIBRARY_ITEMS = [
    _item(
        "ATTN2345",
        title="Attention Is All You Need",
        date="2017-06-12",
        DOI="10.48550/arxiv.1706.03762",
        publicationTitle="NeurIPS",
        abstractNote="The dominant sequence transduction models are based on RNNs.",
        creators=[
            {"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"},
            {"creatorType": "author", "firstName": "Noam", "lastName": "Shazeer"},
        ],
        tags=[{"tag": "nlp"}, {"tag": "transformers"}],
        collections=["MACH2345"],
        extra="Citation Key: vaswani2017attention",
    ),
    _item(
        "KAHN3456",
        item_type="book",
        title="Thinking, Fast and Slow",
        date="2011",
        publisher="Farrar, Straus and Giroux",
        creators=[{"creatorType": "author", "firstName": "Daniel", "lastName": "Kahneman"}],
        tags=[{"tag": "psychology"}],
        collections=["BEHV3456"],
    ),
    _item(
        "CASE4567",
        item_type="case",
        caseName="Roe v. Wade",
        dateDecided="January 22, 1973",
        court="Supreme Court of the United States",
        creators=[{"creatorType": "author", "lastName": "Blackmun"}],
    ),
    # A near-duplicate of the first, for the deduplication tests.
    _item(
        "ATTN9876",
        title="Attention Is All You Need",
        date="2017",
        DOI="10.48550/arxiv.1706.03762",
        creators=[{"creatorType": "author", "lastName": "Vaswani"}],
    ),
]

LIBRARY_COLLECTIONS = [
    {
        "key": "MACH2345",
        "version": 1,
        "data": {
            "key": "MACH2345",
            "name": "Machine Learning",
            "parentCollection": False,
            "version": 1,
        },
        "meta": {"numItems": 1},
    },
    {
        "key": "BEHV3456",
        "version": 1,
        "data": {
            "key": "BEHV3456",
            "name": "Behavioural Economics",
            "parentCollection": False,
            "version": 1,
        },
        "meta": {"numItems": 1},
    },
    {
        "key": "TRNS4567",
        "version": 1,
        "data": {
            "key": "TRNS4567",
            "name": "Transformers",
            "parentCollection": "MACH2345",
            "version": 1,
        },
        "meta": {"numItems": 0},
    },
]

LIBRARY_CHILDREN = {
    "ATTN2345": [
        {
            "key": "PDFA2345",
            "version": 1,
            "data": {
                "key": "PDFA2345",
                "itemType": "attachment",
                "title": "Full Text PDF",
                "filename": "attention.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "parentItem": "ATTN2345",
                "version": 1,
            },
        },
        {
            "key": "NTEA2345",
            "version": 1,
            "data": {
                "key": "NTEA2345",
                "itemType": "note",
                "parentItem": "ATTN2345",
                "note": "<p>Key idea: self-attention replaces recurrence.</p>",
                "version": 1,
            },
        },
    ],
    "PDFA2345": [
        {
            "key": "ANNT2345",
            "version": 1,
            "data": {
                "key": "ANNT2345",
                "itemType": "annotation",
                "parentItem": "PDFA2345",
                "annotationType": "highlight",
                "annotationText": "attention mechanisms",
                "annotationComment": "central claim",
                "annotationColor": "#ffd400",
                "annotationPageLabel": "2",
                "annotationPosition": {"pageIndex": 1},
                "version": 1,
            },
        },
    ],
}

#: Zotero keys are base32 without I, L, O or U, so generated keys stay in it.
_KEY_LETTERS = "ABCDEFGH"

LIBRARY_TAGS = [
    {"tag": "nlp", "meta": {"numItems": 1, "type": 0}},
    {"tag": "transformers", "meta": {"numItems": 1, "type": 0}},
    {"tag": "psychology", "meta": {"numItems": 1, "type": 0}},
]


class FakeBackend:
    """An in-memory Zotero library with the LibraryBackend interface.

    Records every write so tools can be asserted on what they *did*, not only
    on what they returned. Search is deliberately substring matching over
    title and creators, because that is what Zotero actually does and the
    search cascade only makes sense against that behaviour.
    """

    name = "fake"
    writable = True
    has_local_files = True

    def __init__(self, *, writable=True, items=None, reachable=True):
        from zotero_mcp.models import LibraryRef

        self.writable = writable
        self._reachable = reachable
        self.items = {i["key"]: json_copy(i) for i in (items or LIBRARY_ITEMS)}
        self.collections = {c["key"]: json_copy(c) for c in LIBRARY_COLLECTIONS}
        self.children = {k: json_copy(v) for k, v in LIBRARY_CHILDREN.items()}
        #: Zotero addresses a note, attachment or annotation by its own key,
        #: exactly like a top-level item, so they have to be fetchable that way.
        self.child_index = {
            child["key"]: child for rows in self.children.values() for child in rows
        }
        self.tags = json_copy(LIBRARY_TAGS)
        self.trash: dict[str, dict] = {}
        self.attachment_path = None
        self.fulltext: dict[str, str] = {}
        self.writes: list[tuple[str, tuple]] = []
        self._library = LibraryRef(library_id="12345", library_type="user", name="My Library")

    # -- identity ----------------------------------------------------------
    def library_ref(self):
        return self._library

    def ping(self):
        return self._reachable

    # -- reads -------------------------------------------------------------
    def _find(self, key):
        return self.items.get(key) or self.child_index.get(key)

    def get_item(self, key):
        found = self._find(key)
        return json_copy(found) if found is not None else None

    def get_items(self, spec):
        from zotero_mcp.backends.base import RawPage

        rows = [json_copy(i) for i in self.items.values()]

        if spec.collection_key:
            rows = [r for r in rows if spec.collection_key in (r["data"].get("collections") or [])]
        if spec.item_type and not spec.item_type.startswith("-"):
            allowed = {t.strip() for t in spec.item_type.split("||")}
            rows = [r for r in rows if r["data"].get("itemType") in allowed]
        elif spec.item_type and spec.item_type.startswith("-"):
            excluded = spec.item_type[1:]
            rows = [r for r in rows if r["data"].get("itemType") != excluded]
        if spec.tags:
            rows = [
                r
                for r in rows
                if all(
                    tag in {t.get("tag") for t in r["data"].get("tags") or []} for tag in spec.tags
                )
            ]
        if spec.query:
            needle = spec.query.lower()

            def haystack(row):
                data = row["data"]
                creators = " ".join(
                    (c.get("lastName") or c.get("name") or "") for c in data.get("creators") or []
                )
                base = f"{data.get('title', '')} {data.get('caseName', '')} {creators} {data.get('date', '')}"
                if spec.qmode == "everything":
                    base += f" {data.get('abstractNote', '')} {data.get('extra', '')} {data.get('DOI', '')}"
                return base.lower()

            rows = [r for r in rows if needle in haystack(r)]

        total = len(rows)
        window = rows[spec.offset : spec.offset + spec.limit]
        return RawPage(items=window, total=total, offset=spec.offset)

    def get_children(self, key, *, item_type=None):
        rows = json_copy(self.children.get(key, []))
        if item_type:
            rows = [r for r in rows if r["data"].get("itemType") == item_type]
        return rows

    def get_collection(self, key):
        return json_copy(self.collections.get(key)) if key in self.collections else None

    def get_collections(self, *, parent_key=None, offset=0, limit=100):
        from zotero_mcp.backends.base import RawPage

        rows = [json_copy(c) for c in self.collections.values()]
        if parent_key:
            rows = [r for r in rows if r["data"].get("parentCollection") == parent_key]
        return RawPage(items=rows[offset : offset + limit], total=len(rows), offset=offset)

    def get_all_collections(self):
        return [json_copy(c) for c in self.collections.values()]

    def get_tags(self, *, filter_text=None, offset=0, limit=100):
        from zotero_mcp.backends.base import RawPage

        rows = json_copy(self.tags)
        if filter_text:
            rows = [r for r in rows if filter_text.lower() in r["tag"].lower()]
        return RawPage(items=rows[offset : offset + limit], total=len(rows), offset=offset)

    def get_trash(self, *, offset=0, limit=25):
        from zotero_mcp.backends.base import RawPage

        rows = [json_copy(i) for i in self.trash.values()]
        return RawPage(items=rows[offset : offset + limit], total=len(rows), offset=offset)

    def get_fulltext(self, attachment_key):
        return self.fulltext.get(attachment_key)

    def resolve_attachment_path(self, attachment_key):
        return self.attachment_path

    def get_attachment_bytes(self, attachment_key):
        return None

    def count_items(self):
        return len(self.items)

    def list_libraries(self):
        from zotero_mcp.models import LibraryRef

        return [
            self._library,
            LibraryRef(library_id="98765", library_type="group", name="Lab Library"),
        ]

    def get_saved_searches(self):
        return [{"key": "SRCH2345", "data": {"name": "Unread", "conditions": [{}]}}]

    def item_template(self, item_type):
        return {"itemType": item_type, "title": "", "creators": []}

    def get_item_versions(self, *, since=None):
        return {k: v["version"] for k, v in self.items.items()}

    # -- writes ------------------------------------------------------------
    def _guard(self, action):
        from zotero_mcp.errors import Unsupported

        if not self.writable:
            raise Unsupported(
                f"{self.name} is read-only; cannot {action}.",
                hint="Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID.",
            )

    def create_items(self, payloads):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("create items")
        self.writes.append(("create_items", (payloads,)))
        outcome = WriteOutcome()
        for index, payload in enumerate(payloads):
            key = f"NEW{_KEY_LETTERS[index % 8]}2345"
            self.items[key] = {"key": key, "version": 1, "data": {**payload, "key": key}}
            outcome.succeeded[key] = 1
        return outcome

    def update_item(self, key, patch, *, version):
        from zotero_mcp.backends.base import WriteOutcome
        from zotero_mcp.errors import WriteConflict

        self._guard("update items")
        self.writes.append(("update_item", (key, patch, version)))
        item = self._find(key)
        if item is None:
            raise KeyError(key)
        if item["version"] != version:
            raise WriteConflict("Stale version.", details={"item_key": key})
        item["data"].update(patch)
        item["version"] = version + 1
        return WriteOutcome(succeeded={key: version + 1})

    def delete_items(self, versions_by_key):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("delete items")
        self.writes.append(("delete_items", (versions_by_key,)))
        outcome = WriteOutcome()
        for key, version in versions_by_key.items():
            self.items.pop(key, None)
            self.trash.pop(key, None)
            if self.child_index.pop(key, None) is not None:
                for rows in self.children.values():
                    rows[:] = [row for row in rows if row["key"] != key]
            outcome.succeeded[key] = version
        return outcome

    def trash_items(self, versions_by_key):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("trash items")
        self.writes.append(("trash_items", (versions_by_key,)))
        outcome = WriteOutcome()
        for key, version in versions_by_key.items():
            if key in self.items:
                self.trash[key] = self.items[key]
            outcome.succeeded[key] = version + 1
        return outcome

    def restore_items(self, versions_by_key):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("restore items")
        self.writes.append(("restore_items", (versions_by_key,)))
        for key in versions_by_key:
            self.trash.pop(key, None)
        return WriteOutcome(succeeded=dict(versions_by_key))

    def empty_trash(self):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("empty the trash")
        self.writes.append(("empty_trash", ()))
        keys = list(self.trash)
        self.trash.clear()
        return WriteOutcome(succeeded=dict.fromkeys(keys, 0))

    def create_collection(self, name, *, parent_key=None):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("create collections")
        self.writes.append(("create_collection", (name, parent_key)))
        key = f"NEWC{_KEY_LETTERS[len(self.collections) % 8]}234"
        self.collections[key] = {
            "key": key,
            "version": 1,
            "data": {
                "key": key,
                "name": name,
                "parentCollection": parent_key or False,
                "version": 1,
            },
            "meta": {},
        }
        return WriteOutcome(succeeded={key: 1})

    def update_collection(self, key, patch, *, version):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("update collections")
        self.writes.append(("update_collection", (key, patch, version)))
        self.collections[key]["data"].update(patch)
        return WriteOutcome(succeeded={key: version + 1})

    def delete_collection(self, key, *, version):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("delete collections")
        self.writes.append(("delete_collection", (key, version)))
        self.collections.pop(key, None)
        return WriteOutcome(succeeded={key: version})

    def add_to_collection(self, collection_key, item_keys):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("add items to collections")
        self.writes.append(("add_to_collection", (collection_key, item_keys)))
        for key in item_keys:
            collections = self.items[key]["data"].setdefault("collections", [])
            if collection_key not in collections:
                collections.append(collection_key)
        return WriteOutcome(succeeded=dict.fromkeys(item_keys, 1))

    def remove_from_collection(self, collection_key, item_keys):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("remove items from collections")
        self.writes.append(("remove_from_collection", (collection_key, item_keys)))
        for key in item_keys:
            collections = self.items[key]["data"].get("collections") or []
            if collection_key in collections:
                collections.remove(collection_key)
        return WriteOutcome(succeeded=dict.fromkeys(item_keys, 1))

    def attach_file(self, parent_key, path, *, title=None, link_mode="imported_file"):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("attach files")
        self.writes.append(("attach_file", (parent_key, str(path), title, link_mode)))
        return WriteOutcome(succeeded={"ATTN5555": 1})

    def attach_link(self, parent_key, url, *, title=None):
        from zotero_mcp.backends.base import WriteOutcome

        self._guard("attach links")
        self.writes.append(("attach_link", (parent_key, url, title)))
        return WriteOutcome(succeeded={"ATTN6666": 1})


def json_copy(value):
    """Deep copy through JSON, so a test cannot mutate the fixture data."""
    import json

    return json.loads(json.dumps(value))


@pytest.fixture
def fake_backend():
    """An in-memory library, installed as the active runtime."""
    from zotero_mcp.config import ZoteroConfig
    from zotero_mcp.runtime import Runtime, reset_runtime, set_runtime

    backend = FakeBackend()
    set_runtime(Runtime(config=ZoteroConfig(), backend=backend))
    yield backend
    reset_runtime()


@pytest.fixture
def no_index(monkeypatch):
    """Make the semantic index report as absent, which is the default state."""
    monkeypatch.setattr("zotero_mcp.index.query.search", lambda *a, **k: None)
    monkeypatch.setattr(
        "zotero_mcp.index.query.status",
        lambda: {"state": "unavailable", "detail": "no index built", "count": None},
    )


def result_text(value) -> str:
    """The markdown half of a tool result, whatever shape it came back in."""
    content = getattr(value, "content", None)
    if content is None:
        return value if isinstance(value, str) else str(value)
    if isinstance(content, str):
        return content
    return "\n".join(getattr(block, "text", "") for block in content)


def result_data(value):
    """The structured half of a tool result, or None when there is none."""
    return getattr(value, "structured_content", None)
