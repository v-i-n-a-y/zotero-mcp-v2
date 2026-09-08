# Copyright 2026 Vinay

"""The pyzotero-backed backend, and the pyzotero behaviours it works around."""

from __future__ import annotations

import pytest
from pyzotero import zotero_errors as ze

from zotero_mcp.backends.base import ItemQuery
from zotero_mcp.backends.pyzotero_backend import (
    LocalHttpBackend,
    PyzoteroBackend,
    WebBackend,
    _translate,
)
from zotero_mcp.config import LibrarySettings, LibraryType
from zotero_mcp.errors import (
    AuthError,
    BackendUnavailable,
    InvalidInput,
    NotFound,
    RateLimited,
    Unsupported,
    UpstreamError,
    WriteConflict,
)

WEB = LibrarySettings(library_id="12345", api_key="secret")
LOCAL = LibrarySettings()


def _web(fake_zotero) -> WebBackend:
    return WebBackend(WEB)


def _client(fake_zotero):
    return fake_zotero[-1]


# -- construction ----------------------------------------------------------


def test_web_backend_requires_credentials(fake_zotero):
    with pytest.raises(AuthError) as excinfo:
        WebBackend(LibrarySettings())
    assert "ZOTERO_LOCAL" in str(excinfo.value.hint)


def test_local_backend_defaults_the_library_id(fake_zotero):
    """Zotero's local API uses library id 0 as 'whatever library is open'."""
    LocalHttpBackend(LOCAL)
    assert _client(fake_zotero).library_id == "0"
    assert _client(fake_zotero).local is True


def test_local_backend_is_read_only(fake_zotero):
    backend = LocalHttpBackend(LOCAL)
    assert backend.writable is False
    assert backend.has_local_files is True


def test_web_backend_is_writable(fake_zotero):
    backend = _web(fake_zotero)
    assert backend.writable is True
    assert backend.library_ref().library_id == "12345"


def test_group_library_type_is_carried_through(fake_zotero):
    backend = WebBackend(
        LibrarySettings(library_id="9", api_key="k", library_type=LibraryType.GROUP)
    )
    assert backend.library_ref().library_type == "group"


# -- the parameter-accumulation bug ----------------------------------------


def test_parameters_do_not_leak_between_calls(fake_zotero):
    """pyzotero merges into url_params, so a stale filter silently narrows the next search."""
    backend = _web(fake_zotero)
    backend.get_items(ItemQuery(query="attention", item_type="book", limit=5))
    backend.get_items(ItemQuery(query="transformers", item_type=None, limit=5))

    _, _, second = _client(fake_zotero).calls[-1]
    assert second["q"] == "transformers"
    assert "itemType" not in second


def test_a_tag_filter_does_not_survive_into_the_next_call(fake_zotero):
    backend = _web(fake_zotero)
    backend.get_items(ItemQuery(query="a", tags=("urgent",), item_type=None))
    backend.get_items(ItemQuery(query="b", item_type=None))

    _, _, second = _client(fake_zotero).calls[-1]
    assert "tag" not in second


def test_the_fake_client_really_does_accumulate(fake_zotero):
    """Guards the test above: without the fix, the leak must be observable."""
    _web(fake_zotero)
    client = _client(fake_zotero)
    client.add_parameters(itemType="book")
    client.add_parameters(q="x")
    assert client.url_params["itemType"] == "book"


# -- query translation -----------------------------------------------------


def test_query_params_translate_the_whole_spec(fake_zotero):
    params = PyzoteroBackend._query_params(
        ItemQuery(
            query="attention",
            qmode="everything",
            item_type="book || bookSection",
            tags=("a", "b"),
            since=42,
            sort="dateAdded",
            direction="desc",
            include_trashed=True,
            offset=20,
            limit=10,
        )
    )
    assert params == {
        "start": 20,
        "limit": 10,
        "q": "attention",
        "qmode": "everything",
        "itemType": "book || bookSection",
        "tag": ["a", "b"],
        "since": 42,
        "sort": "dateAdded",
        "direction": "desc",
        "includeTrashed": 1,
    }


def test_qmode_is_omitted_without_a_query(fake_zotero):
    assert "qmode" not in PyzoteroBackend._query_params(ItemQuery(item_type=None))


def test_collection_scope_routes_to_collection_items(fake_zotero):
    backend = _web(fake_zotero)
    backend.get_items(ItemQuery(query="x", collection_key="COLL1234"))
    name, args, _ = _client(fake_zotero).calls[-1]
    assert name == "collection_items"
    assert args == ("COLL1234",)


def test_top_level_only_routes_to_top(fake_zotero):
    backend = _web(fake_zotero)
    backend.get_items(ItemQuery(top_level_only=True))
    assert _client(fake_zotero).calls[-1][0] == "top"


# -- totals ----------------------------------------------------------------


def test_total_results_header_is_read(fake_zotero):
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    client.items_result = [{"key": "A"}]
    client.total_results = "137"

    page = backend.get_items(ItemQuery())
    assert page.total == 137
    assert page.offset == 0


def test_a_missing_total_header_is_none_not_a_guess(fake_zotero):
    """Inventing a total by exhausting the collection is what this layer prevents."""
    backend = _web(fake_zotero)
    _client(fake_zotero).items_result = [{"key": "A"}]
    assert backend.get_items(ItemQuery()).total is None


def test_a_malformed_total_header_is_none(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).total_results = "lots"
    assert backend.get_items(ItemQuery()).total is None


# -- error translation -----------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ze.ResourceNotFoundError("x"), NotFound),
        (ze.UserNotAuthorisedError("x"), AuthError),
        (ze.MissingCredentialsError("x"), AuthError),
        (ze.PreConditionFailedError("x"), WriteConflict),
        (ze.PreConditionRequiredError("x"), WriteConflict),
        (ze.ConflictError("x"), WriteConflict),
        (ze.TooManyRequestsError("x"), RateLimited),
        (ze.TooManyRetriesError("x"), RateLimited),
        (ze.InvalidItemFieldsError("x"), InvalidInput),
        (ze.UnsupportedParamsError("x"), InvalidInput),
        (ze.CouldNotReachURLError("x"), BackendUnavailable),
        (ze.FileDoesNotExistError("x"), NotFound),
        (ze.HTTPError("x"), UpstreamError),
    ],
)
def test_pyzotero_errors_are_translated(raised, expected):
    assert isinstance(_translate(raised, context="items"), expected)


def test_a_non_pyzotero_exception_passes_through_untouched():
    """A bug in this package must not be relabelled as a Zotero problem."""
    original = KeyError("data")
    assert _translate(original, context="items") is original


def test_unreachable_zotero_hint_mentions_the_desktop_app():
    error = _translate(ze.CouldNotReachURLError("x"), context="items")
    assert "desktop application must be running" in error.hint


def test_get_item_returns_none_for_a_missing_key(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["item"] = ze.ResourceNotFoundError("nope")
    assert backend.get_item("ABCD2345") is None


def test_ping_reports_false_instead_of_raising(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["items"] = ze.CouldNotReachURLError("down")
    assert backend.ping() is False


def test_ping_reports_true_when_reachable(fake_zotero):
    assert _web(fake_zotero).ping() is True


# -- write outcomes --------------------------------------------------------


def test_batch_response_reports_per_item_failures():
    """A 200 does not mean every item landed; Zotero reports each index."""
    outcome = PyzoteroBackend._outcome_from_response(
        {
            "success": {"0": "AAAA1111", "2": "CCCC3333"},
            "failed": {"1": {"code": 400, "message": "Invalid field"}},
            "unchanged": {"3": "DDDD4444"},
        },
        keys=["AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444"],
    )
    assert set(outcome.succeeded) == {"AAAA1111", "CCCC3333"}
    assert outcome.failed == {"BBBB2222": "Invalid field"}
    assert outcome.unchanged == ["DDDD4444"]


def test_batch_response_without_keys_falls_back_to_the_index():
    outcome = PyzoteroBackend._outcome_from_response({"failed": {"1": {"message": "boom"}}})
    assert outcome.failed == {"1": "boom"}


def test_update_item_sends_the_version_for_optimistic_locking(fake_zotero):
    """Without the version Zotero cannot 412, and a concurrent edit is clobbered."""
    backend = _web(fake_zotero)
    backend.update_item("ABCD2345", {"title": "New"}, version=7)
    _, args, _ = _client(fake_zotero).calls[-1]
    assert args[0] == {"title": "New", "key": "ABCD2345", "version": 7}


def test_update_item_reports_the_next_version(fake_zotero):
    outcome = _web(fake_zotero).update_item("ABCD2345", {"title": "N"}, version=7)
    assert outcome.succeeded == {"ABCD2345": 8}


def test_delete_items_collects_failures_without_aborting(fake_zotero):
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    client.raises["delete_item"] = ze.ResourceNotFoundError("gone")

    outcome = backend.delete_items({"AAAA1111": 1, "BBBB2222": 2})
    assert outcome.succeeded == {}
    assert set(outcome.failed) == {"AAAA1111", "BBBB2222"}


def test_add_to_collection_reports_a_missing_item(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["item"] = ze.ResourceNotFoundError("gone")
    outcome = backend.add_to_collection("COLL1234", ["ABCD2345"])
    assert outcome.failed == {"ABCD2345": "item not found"}


# -- read-only refusals ----------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("create_items", ([{"itemType": "book"}],)),
        ("update_item", ("K", {"title": "x"})),
        ("delete_items", ({"K": 1},)),
        ("create_collection", ("Name",)),
        ("empty_trash", ()),
        ("trash_items", ({"K": 1},)),
        ("restore_items", ({"K": 1},)),
    ],
)
def test_a_read_only_backend_refuses_writes_clearly(fake_zotero, method, args):
    backend = LocalHttpBackend(LOCAL)
    kwargs = {"version": 1} if method == "update_item" else {}
    with pytest.raises(Unsupported) as excinfo:
        getattr(backend, method)(*args, **kwargs)
    assert "read-only" in str(excinfo.value)
    assert "ZOTERO_API_KEY" in str(excinfo.value.hint)


def test_linked_file_attachments_are_refused_with_a_reason(fake_zotero):
    backend = _web(fake_zotero)
    with pytest.raises(InvalidInput) as excinfo:
        backend.attach_file("P", __import__("pathlib").Path("/tmp/x.pdf"), link_mode="linked_file")
    assert "desktop application" in str(excinfo.value)


# -- content ---------------------------------------------------------------


def test_fulltext_returns_the_indexed_content(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).responses["fulltext_item"] = {"content": "body text"}
    assert backend.get_fulltext("ATT1") == "body text"


def test_fulltext_absent_is_none_not_an_error(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["fulltext_item"] = ze.ResourceNotFoundError("no index")
    assert backend.get_fulltext("ATT1") is None


def test_resolve_attachment_path_downloads_to_a_marked_temp_dir(fake_zotero):
    """The prefix is how the content layer knows a file is safe to delete."""
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    client.responses["file"] = b"%PDF-1.4"
    client.responses["item"] = {"key": "ATT1", "data": {"filename": "paper.pdf"}}

    path = backend.resolve_attachment_path("ATT1")
    assert path.read_bytes() == b"%PDF-1.4"
    assert path.name == "paper.pdf"
    assert path.parent.name.startswith("zotero_mcp_dl_")


def test_resolve_attachment_path_is_none_when_there_are_no_bytes(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["file"] = ze.FileDoesNotExistError("no file")
    assert backend.resolve_attachment_path("ATT1") is None


def test_attachment_filename_cannot_escape_the_temp_directory(fake_zotero):
    """A filename from library data is untrusted input to a path join."""
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    client.responses["file"] = b"x"
    client.responses["item"] = {"key": "ATT1", "data": {"filename": "../../evil.pdf"}}

    path = backend.resolve_attachment_path("ATT1")
    assert path.name == "evil.pdf"
    assert path.parent.name.startswith("zotero_mcp_dl_")


# -- library-level ---------------------------------------------------------


def test_list_libraries_includes_groups(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).responses["groups"] = [{"id": 98765, "data": {"name": "Lab library"}}]
    libraries = backend.list_libraries()
    assert [lib.library_type for lib in libraries] == ["user", "group"]
    assert libraries[1].name == "Lab library"


def test_list_libraries_survives_a_key_without_group_scope(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).raises["groups"] = ze.UserNotAuthorisedError("no scope")
    assert len(backend.list_libraries()) == 1


def test_tags_are_normalised_to_dicts(fake_zotero):
    backend = _web(fake_zotero)
    _client(fake_zotero).responses["tags"] = ["alpha", {"tag": "beta"}]
    page = backend.get_tags()
    assert page.items == [{"tag": "alpha"}, {"tag": "beta"}]


# -- trash -----------------------------------------------------------------


def test_trashing_is_a_versioned_update_not_a_delete(fake_zotero):
    """Zotero has no trash endpoint; trashing sets deleted=1 on the item."""
    backend = _web(fake_zotero)
    backend.trash_items({"ABCD2345": 7})
    name, args, _ = _client(fake_zotero).calls[-1]
    assert name == "update_item"
    assert args[0] == {"key": "ABCD2345", "version": 7, "deleted": 1}


def test_restoring_clears_the_deleted_flag(fake_zotero):
    backend = _web(fake_zotero)
    backend.restore_items({"ABCD2345": 7})
    assert _client(fake_zotero).calls[-1][1][0]["deleted"] == 0


def test_empty_trash_pages_rather_than_reading_the_whole_trash(fake_zotero):
    """A neglected trash can hold tens of thousands of items."""
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    pages = [
        [{"key": f"K{i:04d}", "version": 1} for i in range(50)],
        [{"key": "LAST0001", "version": 1}],
        [],
    ]

    def next_page(**params):
        return client._record("trash", (), params) or []

    client.responses["trash"] = pages[0]

    calls = {"n": 0}

    def trash(**params):
        client.calls.append(("trash", (), params))
        index = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        client.request = type(client.request)({})
        return pages[index]

    client.trash = trash
    outcome = backend.empty_trash()
    assert len(outcome.succeeded) == 51
    assert calls["n"] >= 3


def test_empty_trash_stops_when_no_item_in_a_page_can_be_deleted(fake_zotero):
    """Otherwise an undeletable page loops forever."""
    backend = _web(fake_zotero)
    client = _client(fake_zotero)
    client.responses["trash"] = [{"key": "STUCK001", "version": 1}]
    client.raises["delete_item"] = ze.UserNotAuthorisedError("no permission")

    outcome = backend.empty_trash()
    assert outcome.succeeded == {}
    assert "STUCK001" in outcome.failed
