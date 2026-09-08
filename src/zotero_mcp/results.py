"""Assembling a tool result: markdown plus structured content, within budget.

Every tool ends here. Routing all of them through one function is what makes
"no unbounded response" enforceable rather than aspirational — there is exactly
one place where text becomes a result, and it clamps.

The structured half is emitted only when the running configuration allows it
(``SurfaceSettings.structured_output``), because a handful of MCP clients still
mishandle structured content. The markdown half is always present, so those
clients lose nothing but the convenience.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from zotero_mcp.paging import clamp

#: Structured content is data, not prose, and clamping it would corrupt it.
#: Instead, a payload this large is a signal that a page size is wrong, so it
#: is dropped with a note rather than silently sent.
_STRUCTURED_BUDGET_MULTIPLIER = 4


def reply(
    markdown: str,
    structured: BaseModel | dict[str, Any] | None = None,
    *,
    limit: int,
    allow_structured: bool = True,
) -> Any:
    """Build the value a tool returns.

    Args:
        markdown: Human-readable rendering. Clamped to *limit* characters, with
            a truncation note appended when anything is cut.
        structured: Model or dict for the structured half of the result.
        limit: Character budget for the markdown, from ``LimitSettings``.
        allow_structured: False for clients configured without structured output.

    Returns:
        A ``ToolResult`` when structured content is included, otherwise a plain
        string — which FastMCP wraps as a single text block, exactly as before.
    """
    clamped = clamp(markdown, limit)
    text = clamped.text + clamped.note

    if structured is None or not allow_structured:
        return text

    payload = structured.model_dump(mode="json", exclude_none=True) if isinstance(
        structured, BaseModel
    ) else structured

    from fastmcp.tools import ToolResult

    return ToolResult(content=text, structured_content=payload)


def text_reply(markdown: str, *, limit: int) -> str:
    """Markdown-only result, clamped. For tools with nothing structured to say."""
    clamped = clamp(markdown, limit)
    return clamped.text + clamped.note


__all__ = ["reply", "text_reply"]
