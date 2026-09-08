# Copyright 2026 Vinay

"""Page-range parsing, the vocabulary of a bounded document read."""

import pytest

from zotero_mcp.content.ranges import default_pages, describe, next_range, parse_pages
from zotero_mcp.errors import InvalidInput


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1-5", [1, 2, 3, 4, 5]),
        ("3", [3]),
        (3, [3]),
        ("1-3,7", [1, 2, 3, 7]),
        ("7,1-3", [1, 2, 3, 7]),
        ("2-4,3-5", [2, 3, 4, 5]),
        ("1 - 3", [1, 2, 3]),
        ("1–3", [1, 2, 3]),
        ("4-4", [4]),
        (None, []),
        ("", []),
    ],
)
def test_parse_pages(spec, expected):
    assert parse_pages(spec) == expected


def test_parse_pages_drops_pages_past_the_end_rather_than_failing():
    """A caller paging forward should not need to know where the document ends."""
    assert parse_pages("8-20", total=10) == [8, 9, 10]
    assert parse_pages("50-60", total=10) == []


def test_parse_pages_applies_the_cap():
    assert len(parse_pages("1-500", cap=40)) == 40


@pytest.mark.parametrize("spec", ["abc", "1-", "-5", "1..3", "x-y", "1-2-3"])
def test_unparseable_specs_are_rejected_with_an_example(spec):
    with pytest.raises(InvalidInput) as excinfo:
        parse_pages(spec)
    assert "pages='1-5'" in str(excinfo.value.hint)


def test_a_backwards_range_suggests_the_right_one():
    with pytest.raises(InvalidInput) as excinfo:
        parse_pages("9-3")
    assert "'3-9'" in excinfo.value.hint


def test_page_zero_is_rejected():
    with pytest.raises(InvalidInput, match="start at 1"):
        parse_pages("0-3")


def test_default_pages_respects_a_short_document():
    assert default_pages(3, size=5) == [1, 2, 3]
    assert default_pages(None, size=5) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([1, 2, 3], "1-3"),
        ([1, 3, 4, 9], "1,3-4,9"),
        ([7], "7"),
        ([], ""),
    ],
)
def test_describe_round_trips_to_range_syntax(pages, expected):
    assert describe(pages) == expected
    if pages:
        assert parse_pages(expected) == pages


def test_next_range_continues_from_the_end():
    assert next_range([1, 2, 3, 4, 5], total=20, size=5) == "6-10"


def test_next_range_is_clipped_at_the_document_end():
    assert next_range([1, 2, 3, 4, 5], total=7, size=5) == "6-7"


def test_next_range_is_none_at_the_end():
    assert next_range([6, 7], total=7, size=5) is None
    assert next_range([], total=7, size=5) is None


def test_next_range_without_a_known_total_keeps_going():
    assert next_range([1, 2], total=None, size=3) == "3-5"
