# Copyright 2026 Vinay

"""Bounded content reads, and the cleanup rule that must never delete a library file."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from zotero_mcp.backends.base import LibraryBackend, RawPage
from zotero_mcp.config import LimitSettings
from zotero_mcp.content.extract import extract_pdf_pages, pdf_outline, pdf_page_count, tidy
from zotero_mcp.content.reader import (
    TEMP_PREFIX,
    choose_attachment,
    cleanup_download,
    read_content,
)
from zotero_mcp.errors import NotFound
from zotero_mcp.models import LibraryRef

fitz = pytest.importorskip("fitz")

LIMITS = LimitSettings(max_content_chars=20_000, default_pdf_pages=3, max_pdf_pages=10)


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """A twelve-page PDF whose pages are individually identifiable."""
    document = fitz.open()
    for number in range(1, 13):
        page = document.new_page()
        page.insert_text((72, 100), f"Page {number} content marker {number * 11}")
    path = tmp_path / "sample.pdf"
    document.save(str(path))
    document.close()
    return path


def _attachment(key="ATT12345", content_type="application/pdf", link_mode="imported_file"):
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "attachment",
            "title": "Paper PDF",
            "filename": "paper.pdf",
            "contentType": content_type,
            "linkMode": link_mode,
        },
    }


class StubBackend(LibraryBackend):
    """A backend that serves one item, one attachment and one file."""

    name = "stub"

    def __init__(self, *, children=None, path=None, fulltext=None, item=None):
        self._children = children if children is not None else [_attachment()]
        self._path = path
        self._fulltext = fulltext
        self._item = item

    def library_ref(self):
        return LibraryRef(library_id="1")

    def ping(self):
        return True

    def get_item(self, key):
        return self._item

    def get_items(self, spec):
        return RawPage()

    def get_children(self, key, *, item_type=None):
        return self._children

    def get_collection(self, key):
        return None

    def get_collections(self, **kwargs):
        return RawPage()

    def get_all_collections(self):
        return []

    def get_tags(self, **kwargs):
        return RawPage()

    def resolve_attachment_path(self, attachment_key):
        return self._path

    def get_fulltext(self, attachment_key):
        return self._fulltext


# -- extraction ------------------------------------------------------------


def test_pdf_page_count(sample_pdf):
    assert pdf_page_count(sample_pdf) == 12


def test_extract_pdf_pages_labels_every_page(sample_pdf):
    """Without labels there is no way to say which page a quoted sentence came from."""
    text, total = extract_pdf_pages(sample_pdf, [2, 3])
    assert total == 12
    assert "## Page 2" in text and "## Page 3" in text
    assert "marker 22" in text and "marker 33" in text
    assert "## Page 1" not in text


def test_extract_pdf_pages_ignores_pages_outside_the_document(sample_pdf):
    text, _ = extract_pdf_pages(sample_pdf, [11, 12, 99])
    assert "## Page 99" not in text
    assert "## Page 12" in text


def test_tidy_rejoins_hyphenated_line_breaks():
    """Otherwise a word that happened to wrap cannot be searched or quoted."""
    assert tidy("hyphen-\nated word") == "hyphenated word"


def test_tidy_collapses_runaway_whitespace():
    assert tidy("a    b\n\n\n\nc") == "a b\n\nc"


def test_outline_of_a_pdf_without_one_is_empty(sample_pdf):
    assert pdf_outline(sample_pdf) == []


# -- attachment choice -----------------------------------------------------


def test_a_pdf_is_preferred_over_an_html_snapshot():
    children = [
        _attachment("HTML0001", "text/html"),
        _attachment("PDF00001", "application/pdf"),
    ]
    assert choose_attachment(children)["key"] == "PDF00001"


def test_a_linked_url_with_no_file_sorts_last():
    children = [
        _attachment("LINK0001", "application/pdf", link_mode="linked_url"),
        _attachment("HTML0001", "text/html"),
    ]
    assert choose_attachment(children)["key"] == "HTML0001"


def test_an_explicit_attachment_key_always_wins():
    children = [_attachment("PDF00001"), _attachment("HTML0001", "text/html")]
    assert choose_attachment(children, prefer="HTML0001")["key"] == "HTML0001"


def test_choose_attachment_ignores_notes_and_annotations():
    children = [
        {"key": "NOTE0001", "data": {"itemType": "note"}},
        {"key": "ANN00001", "data": {"itemType": "annotation"}},
    ]
    assert choose_attachment(children) is None


# -- reading ---------------------------------------------------------------


def test_reading_defaults_to_the_opening_pages_not_the_whole_document(sample_pdf):
    """The predecessor returned an entire paper and warned about it afterwards."""
    chunk = read_content(StubBackend(path=sample_pdf), "ITEM1234", limits=LIMITS)
    assert chunk.first_page == 1
    assert chunk.last_page == 3
    assert chunk.total_pages == 12
    assert "marker 44" not in chunk.text


def test_reading_a_requested_range(sample_pdf):
    chunk = read_content(StubBackend(path=sample_pdf), "ITEM1234", limits=LIMITS, pages="5-7")
    assert (chunk.first_page, chunk.last_page) == (5, 7)
    assert "marker 55" in chunk.text
    assert "marker 44" not in chunk.text


def test_a_read_reports_how_to_continue(sample_pdf):
    chunk = read_content(StubBackend(path=sample_pdf), "ITEM1234", limits=LIMITS, pages="1-3")
    assert chunk.has_more is True
    assert chunk.next_pages == "4-6"


def test_the_last_pages_do_not_promise_more(sample_pdf):
    chunk = read_content(StubBackend(path=sample_pdf), "ITEM1234", limits=LIMITS, pages="11-12")
    assert chunk.has_more is False
    assert chunk.next_pages is None


def test_the_page_cap_bounds_a_greedy_request(sample_pdf):
    chunk = read_content(
        StubBackend(path=sample_pdf),
        "ITEM1234",
        limits=LimitSettings(max_pdf_pages=2, default_pdf_pages=2),
        pages="1-12",
    )
    assert (chunk.first_page, chunk.last_page) == (1, 2)


def test_the_character_budget_is_enforced_even_within_the_page_cap(sample_pdf):
    chunk = read_content(
        StubBackend(path=sample_pdf),
        "ITEM1234",
        limits=LimitSettings(max_content_chars=120, max_pdf_pages=12),
        pages="1-12",
    )
    assert chunk.truncated is True
    assert len(chunk.text) <= 120


def test_falling_back_to_the_zotero_index_when_the_file_is_missing():
    backend = StubBackend(path=None, fulltext="indexed body text")
    chunk = read_content(backend, "ITEM1234", limits=LIMITS)
    assert "indexed body text" in chunk.text
    assert chunk.source == "Zotero full-text index"


def test_the_index_fallback_says_page_ranges_could_not_be_applied():
    """Silently ignoring the requested range would misrepresent what was read."""
    backend = StubBackend(path=None, fulltext="indexed body text")
    chunk = read_content(backend, "ITEM1234", limits=LIMITS, pages="5-7")
    assert "page ranges could not be applied" in chunk.text


def test_no_attachment_and_no_index_explains_the_local_sync_case():
    backend = StubBackend(children=[], path=None, fulltext=None)
    with pytest.raises(NotFound) as excinfo:
        read_content(backend, "ITEM1234", limits=LIMITS)
    assert "no attachment" in str(excinfo.value)


def test_an_item_that_is_itself_an_attachment_can_be_read(sample_pdf):
    backend = StubBackend(children=[], path=sample_pdf, item=_attachment("ITEM1234"))
    chunk = read_content(backend, "ITEM1234", limits=LIMITS)
    assert chunk.total_pages == 12


# -- cleanup ---------------------------------------------------------------


def test_cleanup_removes_a_directory_this_process_created():
    directory = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    target = directory / "paper.pdf"
    target.write_bytes(b"x")

    cleanup_download(target)
    assert not directory.exists()


def test_cleanup_refuses_a_file_sitting_directly_in_the_temp_root():
    """On Linux the parent is /tmp, and a naive check would delete all of it."""
    root = Path(tempfile.gettempdir())
    target = root / "zotero_mcp_cleanup_probe.pdf"
    target.write_bytes(b"x")
    try:
        cleanup_download(target)
        assert root.exists()
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_cleanup_refuses_a_path_outside_the_temp_root(tmp_path):
    """A file resolved out of the user's Zotero storage is their only copy."""
    storage = tmp_path / "storage" / "ABCD2345"
    storage.mkdir(parents=True)
    target = storage / "paper.pdf"
    target.write_bytes(b"x")

    cleanup_download(target)
    assert target.exists()


def test_cleanup_refuses_a_temp_directory_without_our_prefix():
    directory = Path(tempfile.mkdtemp(prefix="someone_elses_"))
    target = directory / "paper.pdf"
    target.write_bytes(b"x")
    try:
        cleanup_download(target)
        assert target.exists()
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


def test_cleanup_tolerates_none():
    cleanup_download(None)


def test_a_downloaded_file_is_cleaned_up_after_reading(tmp_path, sample_pdf):
    """But the download, not the library's own copy."""
    directory = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    copy = directory / "paper.pdf"
    copy.write_bytes(sample_pdf.read_bytes())

    read_content(StubBackend(path=copy), "ITEM1234", limits=LIMITS)
    assert not directory.exists()


def test_a_library_file_survives_reading(sample_pdf):
    read_content(StubBackend(path=sample_pdf), "ITEM1234", limits=LIMITS)
    assert sample_pdf.exists()


# -- optional extractors ---------------------------------------------------


def test_generic_extraction_flattens_an_html_snapshot(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><body><p>First</p><p>Second</p></body></html>")

    from zotero_mcp.content.extract import extract_generic

    assert extract_generic(path, max_chars=1000) == "First\nSecond"


def test_generic_extraction_reads_plain_text(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("line one\n\n\n\nline two")

    from zotero_mcp.content.extract import extract_generic

    assert extract_generic(path, max_chars=1000) == "line one\n\nline two"


def test_generic_extraction_respects_the_budget(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("x" * 5000)

    from zotero_mcp.content.extract import extract_generic

    assert len(extract_generic(path, max_chars=100)) == 100


def test_a_missing_extractor_names_the_extra_to_install(monkeypatch, tmp_path):
    """An ImportError traceback from inside a tool tells the caller nothing."""
    from zotero_mcp.content import extract
    from zotero_mcp.errors import Unsupported

    monkeypatch.setattr(
        "builtins.__import__",
        lambda name, *a, **k: (
            (_ for _ in ()).throw(ImportError("no module named fitz"))
            if name == "fitz"
            else __import__(name, *a, **k)
        ),
    )

    with pytest.raises(Unsupported) as excinfo:
        extract.pdf_page_count(tmp_path / "missing.pdf")
    assert "zotero-mcp-next[pdf]" in excinfo.value.hint


def test_an_unopenable_pdf_is_invalid_input_not_a_crash(tmp_path):
    from zotero_mcp.content.extract import pdf_page_count
    from zotero_mcp.errors import InvalidInput

    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf at all")
    with pytest.raises(InvalidInput, match="as a PDF"):
        pdf_page_count(path)


def test_page_layout_reports_geometry_for_area_annotations(sample_pdf):
    """Area annotations need a coordinate space the caller cannot guess."""
    from zotero_mcp.content.extract import pdf_page_layout

    layout = pdf_page_layout(sample_pdf, 1)
    assert layout["page"] == 1
    assert layout["width"] > 0 and layout["height"] > 0
    assert any("marker" in block["text"] for block in layout["blocks"])


def test_page_layout_rejects_a_page_outside_the_document(sample_pdf):
    from zotero_mcp.content.extract import pdf_page_layout
    from zotero_mcp.errors import InvalidInput

    with pytest.raises(InvalidInput, match="1-12"):
        pdf_page_layout(sample_pdf, 99)


def test_available_extractors_reports_what_is_installed():
    from zotero_mcp.content.extract import available_extractors

    report = available_extractors()
    assert set(report) == {"pdf", "epub", "markitdown"}
    assert report["pdf"] is True


def test_read_outline_of_a_pdf_without_one(sample_pdf):
    from zotero_mcp.content.reader import read_outline

    entries, title = read_outline(StubBackend(path=sample_pdf), "ITEM1234")
    assert entries == []
    assert title == "Paper PDF"


def test_read_outline_without_a_reachable_file():
    from zotero_mcp.content.reader import read_outline

    with pytest.raises(NotFound, match="not available"):
        read_outline(StubBackend(path=None), "ITEM1234")


def test_describe_attachments_flags_a_linked_url_as_unavailable():
    from zotero_mcp.content.reader import describe_attachments

    refs = describe_attachments([_attachment("LINK0001", link_mode="linked_url")])
    assert refs[0].available is False
