"""The single seam where a tool's output becomes a result."""

from zotero_mcp.models import ItemSummary, ResultPage
from zotero_mcp.results import reply, text_reply

PAGE = ResultPage(
    items=[ItemSummary(key="ABCD2345", title="T", item_type="book")],
    returned=1,
)


def test_markdown_only_reply_is_a_plain_string():
    assert reply("hello", None, limit=100) == "hello"


def test_structured_reply_carries_both_halves():
    result = reply("# Heading", PAGE, limit=1000)
    assert result.structured_content["items"][0]["key"] == "ABCD2345"
    assert result.content


def test_structured_output_can_be_disabled_without_losing_the_markdown():
    result = reply("# Heading", PAGE, limit=1000, allow_structured=False)
    assert result == "# Heading"


def test_reply_clamps_the_markdown_and_says_so():
    result = reply("X" * 5000, None, limit=100)
    assert len(result) < 400
    assert "Truncated" in result


def test_clamping_applies_even_with_structured_content():
    """The budget is the budget; structured content does not buy extra prose."""
    result = reply("Y" * 5000, PAGE, limit=100)
    text = result.content if isinstance(result.content, str) else result.content[0].text
    assert "Truncated" in text


def test_none_fields_are_dropped_from_structured_content():
    """Emitting forty nulls per row is pure token cost with no information."""
    payload = reply("x", PAGE, limit=1000).structured_content
    assert "doi" not in payload["items"][0]
    assert payload["items"][0]["title"] == "T"


def test_dict_structured_content_is_passed_through():
    result = reply("x", {"custom": 1}, limit=1000)
    assert result.structured_content == {"custom": 1}


def test_text_reply_clamps():
    assert "Truncated" in text_reply("Z" * 5000, limit=100)
