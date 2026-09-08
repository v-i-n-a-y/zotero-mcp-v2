"""Embeddable text: metadata documents and chunking are pure and predictable."""

from zotero_mcp.content import chunk_text, extract_text, metadata_document


def test_metadata_document_orders_title_first_then_creators_and_abstract():
    doc = metadata_document(
        {
            "title": "On Rockets",
            "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
            "abstractNote": "<p>An <b>abstract</b>.</p>",
            "publicationTitle": "J. Prop.",
            "date": "2021-05-01",
            "tags": [{"tag": "propulsion"}, {"tag": "nozzles"}],
        }
    )
    lines = doc.split("\n")
    assert lines[0] == "On Rockets"
    assert "Lovelace" in lines[1]
    assert lines[2] == "An abstract."  # HTML stripped
    assert "J. Prop." in doc
    assert "2021" in doc
    assert "propulsion nozzles" in doc


def test_metadata_document_empty_item_is_empty():
    assert metadata_document({}) == ""


def test_chunk_short_text_is_single_chunk():
    assert chunk_text("hello world", size=100, overlap=10) == ["hello world"]


def test_chunk_empty_is_empty():
    assert chunk_text("   ", size=100, overlap=10) == []


def test_chunk_respects_size_and_overlaps():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = chunk_text(text, size=200, overlap=40)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # Consecutive chunks share content (overlap carries context across the seam).
    assert any(chunks[i].split()[-1] in chunks[i + 1] for i in range(len(chunks) - 1))
    # Nothing lost: every word appears in some chunk.
    joined = " ".join(chunks)
    assert all(f"word{i}" in joined for i in range(400))


def test_chunk_prefers_sentence_boundary():
    text = ("Sentence one is here. " * 20).strip()
    chunks = chunk_text(text, size=120, overlap=10)
    # Every chunk except possibly the last ends on a sentence boundary.
    assert all(c.endswith(".") for c in chunks[:-1])


def test_chunk_overlap_larger_than_size_is_tolerated():
    text = "abcdefghij" * 20
    chunks = chunk_text(text, size=50, overlap=100)
    assert chunks and all(len(c) <= 50 for c in chunks)


def test_extract_text_unsupported_type_is_empty():
    assert extract_text(b"\x00\x01", "application/octet-stream", "blob.bin") == ""


def test_extract_text_plain_and_html():
    assert extract_text(b"plain words", "text/plain", "a.txt") == "plain words"
    assert extract_text(b"<p>hi <b>there</b></p>", "text/html", "a.html") == "hi there"


def test_extract_text_bad_pdf_does_not_raise():
    # Garbage bytes labelled as PDF: must degrade to "" rather than raise.
    assert extract_text(b"not really a pdf", "application/pdf", "x.pdf") == ""
