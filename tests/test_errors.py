"""Error translation at the tool boundary."""

import pytest
from fastmcp.exceptions import ToolError

from zotero_mcp.errors import (
    InvalidInput,
    NotFound,
    Unsupported,
    WriteConflict,
    ZoteroMcpError,
    tool_errors,
)


def test_render_includes_code_message_and_hint():
    rendered = NotFound("No item ABCD2345", hint="Call zotero_search first.").render()
    assert rendered.startswith("[not_found] No item ABCD2345")
    assert "Hint: Call zotero_search first." in rendered


def test_render_includes_details():
    rendered = WriteConflict("Stale version", details={"item_key": "ABCD2345"}).render()
    assert "item_key='ABCD2345'" in rendered


def test_known_error_becomes_tool_error_preserving_code():
    @tool_errors
    def failing():
        raise InvalidInput("bad range", hint="Use pages='1-5'.")

    with pytest.raises(ToolError) as excinfo:
        failing()
    assert "[invalid_input] bad range" in str(excinfo.value)
    assert "Use pages='1-5'." in str(excinfo.value)


def test_not_implemented_maps_to_unsupported():
    @tool_errors
    def failing():
        raise NotImplementedError("Local backend cannot write")

    with pytest.raises(ToolError) as excinfo:
        failing()
    assert "[unsupported]" in str(excinfo.value)
    assert "Local backend cannot write" in str(excinfo.value)


def test_unexpected_exception_is_reported_as_internal_not_as_a_known_code():
    """A bug must never masquerade as a well-understood condition."""

    @tool_errors
    def failing():
        raise KeyError("data")

    with pytest.raises(ToolError) as excinfo:
        failing()
    message = str(excinfo.value)
    assert "[internal]" in message
    assert "KeyError" in message


def test_tool_error_passes_through_unwrapped():
    @tool_errors
    def failing():
        raise ToolError("already shaped")

    with pytest.raises(ToolError) as excinfo:
        failing()
    assert str(excinfo.value) == "already shaped"


def test_success_is_returned_unchanged():
    @tool_errors
    def ok(value):
        return value * 2

    assert ok(21) == 42


def test_decorator_preserves_metadata():
    @tool_errors
    def documented(a: int) -> int:
        """Docstring survives."""
        return a

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Docstring survives."


def test_every_error_subclass_has_a_distinct_code():
    subclasses = [
        cls for cls in ZoteroMcpError.__subclasses__()
    ]
    codes = [cls.code for cls in subclasses]
    assert len(codes) == len(set(codes)), f"duplicate codes among {codes}"
    assert Unsupported.code == "unsupported"
