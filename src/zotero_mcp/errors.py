"""Typed failures, and the single seam that turns them into MCP tool errors.

The predecessor to this package returned every failure as a *successful* tool
result whose text happened to begin with ``"Error: "``. An MCP client cannot
tell that apart from a real answer: the ``isError`` flag is never set, so the
model sees a normal result and has to parse English to work out that nothing
happened. Retry logic, fallbacks and "did the write land?" checks all become
guesswork.

Here every failure is an exception carrying a machine-readable ``code``, a
human sentence, and — where one exists — a ``hint`` naming the concrete next
action. :func:`tool_errors` converts them at the tool boundary into
``fastmcp.exceptions.ToolError``, which FastMCP marshals with ``isError``
set, so the model is told plainly that the call failed and why.

Unexpected exceptions are deliberately *not* dressed up as
:class:`ZoteroMcpError`. They are logged with a traceback and re-raised as a
generic internal error, so a bug never masquerades as a well-understood
condition like "not found".
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class ZoteroMcpError(Exception):
    """Base class for every failure this server understands.

    Attributes:
        code: Stable, machine-readable slug (``"not_found"``, ``"auth"``, ...).
            Clients and tests match on this, never on the message text.
        message: One sentence describing what went wrong.
        hint: Optional concrete next action, phrased for the model that has to
            take it (a tool name to call, an env var to set).
        details: Optional structured context, surfaced verbatim to the caller.
    """

    code = "error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def render(self) -> str:
        """Format as a single string for transport to the client."""
        parts = [f"[{self.code}] {self.message}"]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        if self.details:
            rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
            parts.append(f"Details: {rendered}")
        return " ".join(parts)


class NotFound(ZoteroMcpError):
    """A referenced item, collection, attachment or library does not exist."""

    code = "not_found"


class InvalidInput(ZoteroMcpError):
    """Arguments were syntactically fine but semantically unusable."""

    code = "invalid_input"


class AuthError(ZoteroMcpError):
    """Credentials are missing, wrong, or lack the required permission."""

    code = "auth"


class BackendUnavailable(ZoteroMcpError):
    """The configured Zotero backend could not be reached.

    The overwhelmingly common cause is local mode with the Zotero desktop
    application closed, so the hint says so rather than making the user guess.
    """

    code = "backend_unavailable"


class WriteConflict(ZoteroMcpError):
    """The item changed underneath us; the caller's version is stale.

    Raised when Zotero rejects a write with HTTP 412. Recovery is always the
    same: re-read the item to pick up the current version, then re-apply.
    """

    code = "write_conflict"


class Unsupported(ZoteroMcpError):
    """The operation is real but unavailable in the current configuration.

    Typically an optional extra that is not installed, or a write attempted
    against a read-only local backend.
    """

    code = "unsupported"


class UpstreamError(ZoteroMcpError):
    """A third-party service (Crossref, Unpaywall, arXiv, Scite) failed."""

    code = "upstream"


class RateLimited(ZoteroMcpError):
    """Zotero or a third-party service asked us to back off."""

    code = "rate_limited"


class InternalError(ZoteroMcpError):
    """An unexpected exception escaped a tool. Always a bug in this package."""

    code = "internal"


def tool_errors(func: F) -> F:
    """Translate exceptions at the tool boundary into ``ToolError``.

    Apply to every registered tool, outermost after the ``@mcp.tool``
    decorator. Known failures keep their code and hint; anything else is
    logged with a traceback and reported as an internal error rather than
    leaking a raw stack trace to the model.
    """
    # Imported lazily so that this module stays importable (and testable)
    # without FastMCP present.
    from fastmcp.exceptions import ToolError

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except ZoteroMcpError as exc:
            raise ToolError(exc.render()) from exc
        except NotImplementedError as exc:
            raise ToolError(
                Unsupported(str(exc) or "Operation not supported by this backend").render()
            ) from exc
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the seam
            logger.exception("Unhandled error in tool %s", getattr(func, "__name__", "?"))
            raise ToolError(
                InternalError(
                    f"{type(exc).__name__}: {exc}",
                    hint="This is a bug in zotero-mcp; the server log has a traceback.",
                ).render()
            ) from exc

    return wrapper  # type: ignore[return-value]


__all__ = [
    "AuthError",
    "BackendUnavailable",
    "InternalError",
    "InvalidInput",
    "NotFound",
    "RateLimited",
    "Unsupported",
    "UpstreamError",
    "WriteConflict",
    "ZoteroMcpError",
    "tool_errors",
]
