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
