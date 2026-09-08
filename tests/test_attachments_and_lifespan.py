# Copyright 2026 Vinay

"""Attaching files, fetching open-access PDFs, and server startup."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import result_data
from zotero_mcp.config import NetworkSettings, ZoteroConfig
from zotero_mcp.errors import InvalidInput, NotFound
from zotero_mcp.tools.write import _attach_local_file, _attach_open_access, zotero_add_item

SETTINGS = NetworkSettings(allow_external_services=True, max_retries=1, backoff_seconds=0.0)


# -- local files ------------------------------------------------------------


def test_a_relative_path_is_refused(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"title": "T"}, file_path="notes.pdf")
    assert "must be absolute" in str(excinfo.value)


def test_a_symlink_is_never_attached(fake_backend, tmp_path):
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    with pytest.raises(InvalidInput) as excinfo:
        _attach_local_file("ATTN2345", str(link))
    assert "symlink" in str(excinfo.value)


def test_a_path_with_no_file_behind_it_is_reported(fake_backend, tmp_path):
    with pytest.raises(NotFound) as excinfo:
        _attach_local_file("ATTN2345", str(tmp_path / "absent.pdf"))
    assert "No file at" in str(excinfo.value)


def test_a_real_file_is_attached_under_its_own_name(fake_backend, tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4")
    assert _attach_local_file("ATTN2345", str(path)) == "Attached paper.pdf."
    name, (parent, _, title, _) = fake_backend.writes[0]
    assert (name, parent, title) == ("attach_file", "ATTN2345", "paper.pdf")


def test_a_read_only_backend_explains_rather_than_failing_the_import(tmp_path, monkeypatch):
    from conftest import FakeBackend
    from zotero_mcp.runtime import Runtime, reset_runtime, set_runtime

    set_runtime(Runtime(config=ZoteroConfig(), backend=FakeBackend(writable=False)))
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        assert "Could not attach the file" in _attach_local_file("ATTN2345", str(path))
    finally:
        reset_runtime()


def test_a_rejected_upload_is_reported_not_swallowed(fake_backend, tmp_path):
    from zotero_mcp.backends.base import WriteOutcome

    fake_backend.attach_file = lambda *a, **k: WriteOutcome(failed={"x": "too large"})
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4")
    assert "too large" in _attach_local_file("ATTN2345", str(path))


# -- open access ------------------------------------------------------------


def test_no_identifier_means_no_lookup_was_attempted(fake_backend):
    message = _attach_open_access("ATTN2345", {}, None, SETTINGS)
    assert "no open-access lookup was attempted" in message


def test_nothing_found_is_said_plainly(fake_backend, monkeypatch):
    monkeypatch.setattr("zotero_mcp.external.openaccess.find_pdf", lambda **k: None)
    message = _attach_open_access("ATTN2345", {"DOI": "10.1000/x"}, None, SETTINGS)
    assert message == "No open-access PDF was found for this item."


def test_external_lookups_being_disabled_is_reported_as_the_reason(fake_backend):
    message = _attach_open_access(
        "ATTN2345", {"DOI": "10.1000/x"}, None, NetworkSettings(allow_external_services=False)
    )
    assert "disabled" in message


def test_a_landing_page_is_not_attached_as_a_pdf(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.external.openaccess.find_pdf", lambda **k: ("https://x/1.pdf", "Unpaywall")
    )
    monkeypatch.setattr("zotero_mcp.external.http.download", lambda url, settings: b"<html>")
    message = _attach_open_access("ATTN2345", {"DOI": "10.1000/x"}, None, SETTINGS)
    assert "could not be downloaded" in message
    assert fake_backend.writes == []


def test_a_real_pdf_is_attached_and_the_source_named(fake_backend, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.external.openaccess.find_pdf", lambda **k: ("https://x/1.pdf", "arXiv")
    )
    monkeypatch.setattr("zotero_mcp.external.http.download", lambda url, settings: b"%PDF-1.4 body")
    message = _attach_open_access("ATTN2345", {"DOI": "10.1000/x"}, None, SETTINGS)
    assert message == "Attached an open-access PDF from arXiv."
    assert fake_backend.writes[0][0] == "attach_file"


def test_the_temporary_download_is_cleaned_up(fake_backend, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        "zotero_mcp.external.openaccess.find_pdf", lambda **k: ("https://x/1.pdf", "arXiv")
    )
    monkeypatch.setattr("zotero_mcp.external.http.download", lambda url, settings: b"%PDF-1.4")

    def capture(path):
        seen.append(path)
        assert not path.exists() or path.is_file()

    monkeypatch.setattr("zotero_mcp.content.reader.cleanup_download", capture)
    _attach_open_access("ATTN2345", {"DOI": "10.1000/x"}, None, SETTINGS)
    assert seen and seen[0].name == "ATTN2345.pdf"


def test_zotero_rejecting_the_upload_is_reported(fake_backend, monkeypatch):
    from zotero_mcp.backends.base import WriteOutcome

    monkeypatch.setattr(
        "zotero_mcp.external.openaccess.find_pdf", lambda **k: ("https://x/1.pdf", "arXiv")
    )
    monkeypatch.setattr("zotero_mcp.external.http.download", lambda url, settings: b"%PDF-1.4")
    fake_backend.attach_file = lambda *a, **k: WriteOutcome(failed={"x": "no"})
    assert "Zotero rejected the upload" in _attach_open_access(
        "ATTN2345", {"DOI": "10.1000/x"}, None, SETTINGS
    )


def test_adding_an_item_with_a_file_attaches_it(fake_backend, tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4")
    data = result_data(
        zotero_add_item(item_type="book", fields={"title": "T"}, file_path=str(path))
    )
    assert "Attached paper.pdf." in data["message"]


def test_an_unknown_field_hint_suggests_a_near_miss(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"publishr": "x"})
    assert "publisher" in str(excinfo.value)


def test_an_unrecognisable_field_lists_the_valid_ones(fake_backend):
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"zzzzzzzz": "x"})
    assert "Valid fields for book" in str(excinfo.value)


def test_zotero_rejecting_a_new_item_is_surfaced(fake_backend):
    from zotero_mcp.backends.base import WriteOutcome

    fake_backend.create_items = lambda payloads: WriteOutcome(failed={"0": "invalid item type"})
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"title": "T"}, attach_pdf=False)
    assert "invalid item type" in str(excinfo.value)


def test_zotero_accepting_but_returning_no_key_is_an_error(fake_backend):
    from zotero_mcp.backends.base import WriteOutcome

    fake_backend.create_items = lambda payloads: WriteOutcome()
    with pytest.raises(ToolError) as excinfo:
        zotero_add_item(item_type="book", fields={"title": "T"}, attach_pdf=False)
    assert "no item key" in str(excinfo.value)


def test_a_long_field_value_is_shortened_in_the_preview(fake_backend):
    data = result_data(
        zotero_add_item(
            item_type="book", fields={"title": "T", "abstractNote": "x" * 500}, dry_run=True
        )
    )
    abstract = next(c for c in data["changes"] if c["field"] == "abstractNote")
    assert len(abstract["after"]) == 200
    assert abstract["after"].endswith("...")


# -- startup ----------------------------------------------------------------


@pytest.mark.anyio
async def test_the_lifespan_records_a_failed_startup_rather_than_refusing_to_boot(monkeypatch):
    from zotero_mcp import runtime as runtime_module
    from zotero_mcp.app import lifespan
    from zotero_mcp.errors import BackendUnavailable

    def explode(*a, **k):
        raise BackendUnavailable("Zotero is not running.")

    monkeypatch.setattr(runtime_module, "initialise", explode)
    monkeypatch.setattr("zotero_mcp.schema.refresh", lambda: False)

    async with lifespan(None) as state:
        assert isinstance(state["startup_error"], BackendUnavailable)
        assert runtime_module.startup_error() is not None


@pytest.mark.anyio
async def test_a_successful_startup_leaves_no_error(monkeypatch, fake_backend):
    from zotero_mcp import runtime as runtime_module
    from zotero_mcp.app import lifespan

    monkeypatch.setattr(runtime_module, "initialise", lambda: None)
    monkeypatch.setattr("zotero_mcp.schema.refresh", lambda: False)

    async with lifespan(None) as state:
        assert state["startup_error"] is None


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_logging_never_goes_to_stdout(monkeypatch, capsys):
    """Anything on stdout corrupts the JSON-RPC stream on a stdio transport."""
    import logging

    from zotero_mcp.app import _configure_logging

    monkeypatch.setenv("ZOTERO_MCP_LOG_LEVEL", "DEBUG")
    _configure_logging()
    logging.getLogger("zotero_mcp.test").warning("a message")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "a message" in captured.err
