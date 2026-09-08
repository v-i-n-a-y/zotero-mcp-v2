# Copyright 2026 Vinay

"""Cursors, page assembly and text clamping."""

import pytest

from zotero_mcp.errors import InvalidInput
from zotero_mcp.paging import (
    Page,
    clamp,
    clamp_with_note,
    decode_cursor,
    encode_cursor,
    normalize_page_size,
)

QUERY = {"q": "attention", "item_type": "journalArticle"}


def test_cursor_round_trips():
    assert decode_cursor(encode_cursor(40, QUERY), QUERY) == 40


def test_cursor_is_opaque_and_url_safe():
    cursor = encode_cursor(40, QUERY)
    assert "40" not in cursor
    assert all(c.isalnum() or c in "-_" for c in cursor)


def test_cursor_is_bound_to_its_query():
    """Replaying a cursor against a different search must not silently work."""
    cursor = encode_cursor(40, QUERY)
    with pytest.raises(InvalidInput, match="does not belong"):
        decode_cursor(cursor, {**QUERY, "q": "transformers"})


@pytest.mark.parametrize("bad", ["", "!!!", "zzzz", "eyJ2IjoxfQ"])
def test_malformed_cursor_is_rejected_clearly(bad):
    with pytest.raises(InvalidInput):
        decode_cursor(bad, QUERY)


def test_page_issues_a_cursor_only_when_more_remain():
    full = Page.build(list(range(20)), offset=0, page_size=20, query=QUERY, total=50)
    assert full.has_more
    assert decode_cursor(full.next_cursor, QUERY) == 20

    last = Page.build(list(range(10)), offset=40, page_size=20, query=QUERY, total=50)
    assert not last.has_more
    assert last.next_cursor is None


def test_page_infers_has_more_from_a_full_page_when_total_is_unknown():
    assert Page.build(list(range(20)), offset=0, page_size=20, query=QUERY).has_more
    assert not Page.build(list(range(3)), offset=0, page_size=20, query=QUERY).has_more


def test_empty_page_never_issues_a_cursor():
    assert Page.build([], offset=0, page_size=20, query=QUERY, total=0).next_cursor is None


def test_shown_range_is_one_based():
    page = Page.build(list(range(20)), offset=40, page_size=20, query=QUERY, total=100)
    assert page.shown_range == (41, 60)
    assert Page.build([], offset=0, page_size=20, query=QUERY).shown_range == (0, 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 20),
        ("", 20),
        (5, 5),
        ("5", 5),
        ("5.0", 5),
        (0, 1),
        (-3, 1),  # clamped up to the minimum
        (5000, 100),
        ("5000", 100),  # clamped down to the maximum
        ("all", 100),
        ("MAX", 100),
        ("banana", 20),
        ([], 20),  # unusable input falls back to the default
    ],
)
def test_normalize_page_size(value, expected):
    assert normalize_page_size(value, default=20, maximum=100) == expected


def test_clamp_leaves_short_text_alone():
    result = clamp("hello", 100)
    assert result.text == "hello"
    assert not result.truncated
    assert result.note == ""


def test_clamp_prefers_a_paragraph_boundary():
    text = "A" * 90 + "\n\n" + "B" * 90
    result = clamp(text, 100)
    assert result.truncated
    assert result.text == "A" * 90
    assert "B" not in result.text


def test_clamp_falls_back_to_a_space():
    text = " ".join(["word"] * 100)
    result = clamp(text, 100)
    assert result.truncated
    assert not result.text.endswith("wor")  # never mid-word
    assert len(result.text) <= 100


def test_clamp_cuts_hard_when_no_boundary_exists():
    result = clamp("X" * 500, 100)
    assert result.truncated
    assert len(result.text) == 100


def test_clamp_note_reports_real_numbers():
    result = clamp("Y" * 1000, 100)
    assert "100 of 1,000 characters" in result.note
    assert "(10%)" in result.note


def test_clamp_with_note_appends_the_note():
    out = clamp_with_note("Z" * 1000, 100)
    assert out.startswith("Z" * 100)
    assert "Truncated" in out
