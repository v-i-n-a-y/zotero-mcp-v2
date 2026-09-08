# Copyright 2026 Vinay

"""Hybrid composition, backend selection, and the process runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from zotero_mcp.backends.base import ItemQuery, LibraryBackend, RawPage, WriteOutcome
from zotero_mcp.backends.factory import build_backend
from zotero_mcp.backends.hybrid import HybridBackend
from zotero_mcp.backends.pyzotero_backend import LocalHttpBackend, WebBackend
from zotero_mcp.config import LibraryMode, LibrarySettings, ZoteroConfig
from zotero_mcp.errors import AuthError, BackendUnavailable, InternalError
from zotero_mcp.models import LibraryRef
from zotero_mcp.runtime import Runtime, get_runtime, initialise, reset_runtime, set_runtime


class RecordingBackend(LibraryBackend):
    """Records which calls it received, so routing can be asserted."""

    def __init__(self, name, *, writable=False, reachable=True, files=False, data=None):
        self.name = name
        self.writable = writable
        self.has_local_files = files
        self._reachable = reachable
        self._data = data or {}
        self.calls: list[str] = []

    def _note(self, call):
        self.calls.append(call)

    def library_ref(self):
        return LibraryRef(library_id=self.name)

    def ping(self):
        self._note("ping")
        return self._reachable

    def get_item(self, key):
        self._note("get_item")
        return self._data.get("item")

    def get_items(self, spec):
        self._note("get_items")
        return RawPage()

    def get_children(self, key, *, item_type=None):
        self._note("get_children")
        return []

    def get_collection(self, key):
        self._note("get_collection")
        return None

    def get_collections(self, **kwargs):
        self._note("get_collections")
        return RawPage()

    def get_all_collections(self):
        self._note("get_all_collections")
        return []

    def get_tags(self, **kwargs):
        self._note("get_tags")
        return RawPage()

    def get_trash(self, **kwargs):
        self._note("get_trash")
        return RawPage()

    def get_fulltext(self, attachment_key):
        self._note("get_fulltext")
        return self._data.get("fulltext")

    def resolve_attachment_path(self, attachment_key):
        self._note("resolve_attachment_path")
        return self._data.get("path")

    def get_attachment_bytes(self, attachment_key):
        self._note("get_attachment_bytes")
        return self._data.get("bytes")

    def list_libraries(self):
        self._note("list_libraries")
        return [self.library_ref()]

    def item_template(self, item_type):
        self._note("item_template")
        return {}

    def create_items(self, payloads):
        self._note("create_items")
        return WriteOutcome(succeeded={"NEW00001": 1})

    def update_item(self, key, patch, *, version):
        self._note("update_item")
        return WriteOutcome(succeeded={key: version + 1})

    def delete_items(self, versions_by_key):
        self._note("delete_items")
        return WriteOutcome()

    def trash_items(self, versions_by_key):
        self._note("trash_items")
        return WriteOutcome()

    def restore_items(self, versions_by_key):
        self._note("restore_items")
        return WriteOutcome()

    def empty_trash(self):
        self._note("empty_trash")
        return WriteOutcome()

    def create_collection(self, name, *, parent_key=None):
        self._note("create_collection")
        return WriteOutcome()

    def update_collection(self, key, patch, *, version):
        self._note("update_collection")
        return WriteOutcome()

    def delete_collection(self, key, *, version):
        self._note("delete_collection")
        return WriteOutcome()

    def add_to_collection(self, collection_key, item_keys):
        self._note("add_to_collection")
        return WriteOutcome()

    def remove_from_collection(self, collection_key, item_keys):
        self._note("remove_from_collection")
        return WriteOutcome()

    def attach_file(self, parent_key, path, *, title=None, link_mode="imported_file"):
        self._note("attach_file")
        return WriteOutcome()

    def attach_link(self, parent_key, url, *, title=None):
        self._note("attach_link")
        return WriteOutcome()


def _hybrid(reader_data=None, writer_data=None):
    reader = RecordingBackend("reader", files=True, data=reader_data)
    writer = RecordingBackend("writer", writable=True, data=writer_data)
    return HybridBackend(reader, writer), reader, writer


# -- hybrid routing --------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("get_item", ("K",)),
        ("get_items", (ItemQuery(),)),
        ("get_collections", ()),
        ("get_all_collections", ()),
        ("get_tags", ()),
        ("get_trash", ()),
        ("get_collection", ("K",)),
    ],
)
def test_reads_go_to_the_reader(call, args):
    hybrid, reader, writer = _hybrid()
    getattr(hybrid, call)(*args)
    assert call in reader.calls
    assert writer.calls == []


@pytest.mark.parametrize(
    ("call", "args", "kwargs"),
    [
        ("create_items", ([{"itemType": "book"}],), {}),
        ("update_item", ("K", {"title": "x"}), {"version": 1}),
        ("delete_items", ({"K": 1},), {}),
        ("trash_items", ({"K": 1},), {}),
        ("restore_items", ({"K": 1},), {}),
        ("empty_trash", (), {}),
        ("create_collection", ("Name",), {}),
        ("update_collection", ("K", {"name": "n"}), {"version": 1}),
        ("delete_collection", ("K",), {"version": 1}),
        ("add_to_collection", ("C", ["K"]), {}),
        ("remove_from_collection", ("C", ["K"]), {}),
        ("attach_link", ("P", "https://example.com"), {}),
    ],
)
def test_writes_go_to_the_writer(call, args, kwargs):
    hybrid, reader, writer = _hybrid()
    getattr(hybrid, call)(*args, **kwargs)
    assert call in writer.calls
    assert reader.calls == []


def test_attach_file_goes_to_the_writer():
    hybrid, _, writer = _hybrid()
    hybrid.attach_file("P", Path("/tmp/x.pdf"))
    assert "attach_file" in writer.calls


def test_hybrid_is_writable_and_reports_local_files():
    hybrid, _, _ = _hybrid()
    assert hybrid.writable is True
    assert hybrid.has_local_files is True
    assert hybrid.name == "hybrid(reader+writer)"


def test_the_writer_is_authoritative_for_library_identity():
    """A local reader may only know library id 0."""
    hybrid, _, _ = _hybrid()
    assert hybrid.library_ref().library_id == "writer"


def test_item_template_comes_from_the_writer():
    """Templates describe what the write endpoint will accept."""
    hybrid, _, writer = _hybrid()
    hybrid.item_template("book")
    assert "item_template" in writer.calls


def test_get_item_for_write_reads_the_authoritative_version():
    hybrid, reader, writer = _hybrid()
    hybrid.get_item_for_write("K")
    assert "get_item" in writer.calls
    assert reader.calls == []


def test_fulltext_falls_back_to_the_web_index():
    """An attachment indexed on another machine is on the server, not here."""
    hybrid, reader, _ = _hybrid(writer_data={"fulltext": "server text"})
    assert hybrid.get_fulltext("ATT1") == "server text"
    assert "get_fulltext" in reader.calls


def test_fulltext_prefers_the_local_index_when_it_has_one():
    hybrid, _, writer = _hybrid(
        reader_data={"fulltext": "local text"}, writer_data={"fulltext": "server text"}
    )
    assert hybrid.get_fulltext("ATT1") == "local text"
    assert writer.calls == []


def test_a_missing_local_file_falls_back_to_downloading_it():
    hybrid, _, _ = _hybrid(writer_data={"path": Path("/tmp/downloaded.pdf")})
    assert hybrid.resolve_attachment_path("ATT1") == Path("/tmp/downloaded.pdf")


def test_a_present_local_file_is_used_without_a_download():
    hybrid, _, writer = _hybrid(reader_data={"path": Path("/storage/paper.pdf")})
    assert hybrid.resolve_attachment_path("ATT1") == Path("/storage/paper.pdf")
    assert writer.calls == []


def test_attachment_bytes_fall_back_to_the_writer():
    hybrid, _, _ = _hybrid(writer_data={"bytes": b"%PDF"})
    assert hybrid.get_attachment_bytes("ATT1") == b"%PDF"


def test_ping_probes_the_reader_and_ping_writer_the_writer():
    """Health output needs to say which half is broken."""
    hybrid, reader, writer = _hybrid()
    hybrid.ping()
    hybrid.ping_writer()
    assert reader.calls == ["ping"]
    assert writer.calls == ["ping"]


def test_saved_searches_fall_back_to_the_writer():
    """Neither half implements them, so the fallback surfaces the writer's refusal."""
    from zotero_mcp.errors import Unsupported

    hybrid, _, _ = _hybrid()
    with pytest.raises(Unsupported):
        hybrid.get_saved_searches()


def test_list_libraries_comes_from_the_writer():
    hybrid, _, writer = _hybrid()
    hybrid.list_libraries()
    assert "list_libraries" in writer.calls


# -- selection -------------------------------------------------------------


def _config(mode: LibraryMode, *, credentials: bool) -> ZoteroConfig:
    library = LibrarySettings(
        mode=mode,
        library_id="12345" if credentials else None,
        api_key="secret" if credentials else None,
    )
    return ZoteroConfig(library=library)


@pytest.fixture
def local_reachable(monkeypatch):
    """Control whether the local Zotero API answers."""

    def setter(reachable: bool):
        monkeypatch.setattr(LocalHttpBackend, "ping", lambda self: reachable)

    return setter


def test_explicit_web_mode_does_not_probe(fake_zotero, local_reachable):
    local_reachable(False)
    assert isinstance(build_backend(_config(LibraryMode.WEB, credentials=True)), WebBackend)


def test_explicit_local_mode_fails_loudly_when_zotero_is_closed(fake_zotero, local_reachable):
    local_reachable(False)
    with pytest.raises(BackendUnavailable) as excinfo:
        build_backend(_config(LibraryMode.LOCAL, credentials=False))
    assert "Allow other applications" in excinfo.value.hint


def test_explicit_local_mode_works_when_zotero_is_running(fake_zotero, local_reachable):
    local_reachable(True)
    backend = build_backend(_config(LibraryMode.LOCAL, credentials=False))
    assert isinstance(backend, LocalHttpBackend)


def test_hybrid_mode_without_credentials_names_what_is_missing(fake_zotero, local_reachable):
    local_reachable(True)
    with pytest.raises(AuthError) as excinfo:
        build_backend(_config(LibraryMode.HYBRID, credentials=False))
    assert "ZOTERO_API_KEY" in excinfo.value.hint


def test_auto_prefers_hybrid_when_both_halves_are_available(fake_zotero, local_reachable):
    local_reachable(True)
    backend = build_backend(_config(LibraryMode.AUTO, credentials=True))
    assert isinstance(backend, HybridBackend)


def test_auto_falls_back_to_web_when_zotero_is_closed(fake_zotero, local_reachable):
    local_reachable(False)
    backend = build_backend(_config(LibraryMode.AUTO, credentials=True))
    assert isinstance(backend, WebBackend)


def test_auto_falls_back_to_local_without_credentials(fake_zotero, local_reachable):
    local_reachable(True)
    backend = build_backend(_config(LibraryMode.AUTO, credentials=False))
    assert isinstance(backend, LocalHttpBackend)


def test_auto_with_nothing_available_explains_both_routes(fake_zotero, local_reachable):
    local_reachable(False)
    with pytest.raises(BackendUnavailable) as excinfo:
        build_backend(_config(LibraryMode.AUTO, credentials=False))
    assert "ZOTERO_LIBRARY_ID" in excinfo.value.hint
    assert "start zotero" in excinfo.value.hint.lower()


# -- runtime ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_runtime():
    reset_runtime()
    yield
    reset_runtime()


def test_using_the_runtime_before_startup_is_reported_as_a_bug():
    with pytest.raises(InternalError) as excinfo:
        get_runtime()
    assert "bug" in excinfo.value.hint


def test_set_and_get_runtime():
    runtime = Runtime(config=ZoteroConfig(), backend=RecordingBackend("stub"))
    set_runtime(runtime)
    assert get_runtime() is runtime


def test_runtime_exposes_limits_and_structured_flag():
    runtime = Runtime(config=ZoteroConfig(), backend=RecordingBackend("stub"))
    assert runtime.limits.default_page_size == 20
    assert runtime.structured is True


def test_with_backend_returns_a_copy_bound_to_another_library():
    runtime = Runtime(config=ZoteroConfig(), backend=RecordingBackend("first"))
    other = RecordingBackend("second")
    switched = runtime.with_backend(other, LibrarySettings(library_id="999"))

    assert switched.backend is other
    assert switched.config.library.library_id == "999"
    assert runtime.backend.name == "first"


def test_initialise_builds_and_installs_the_runtime(fake_zotero, local_reachable):
    local_reachable(True)
    runtime = initialise(_config(LibraryMode.LOCAL, credentials=False))
    assert get_runtime() is runtime
    assert isinstance(runtime.backend, LocalHttpBackend)
