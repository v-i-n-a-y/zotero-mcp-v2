# Copyright 2026 Vinay

"""One HTTP client for everything that is not Zotero.

Centralised so that timeouts, retries, the user agent and, most importantly,
the master off switch all have exactly one implementation. Users who chose
local mode for privacy reasons get a real guarantee from
``allow_external_services=false``: there is no second code path that quietly
calls out anyway.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from zotero_mcp.config import NetworkSettings
from zotero_mcp.errors import RateLimited, Unsupported, UpstreamError

logger = logging.getLogger(__name__)

#: Retried, because they are transient by definition.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def get(
    url: str,
    *,
    settings: NetworkSettings,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> Any:
    """GET *url*, with retries and honest failures.

    Returns parsed JSON when the response is JSON, otherwise the response text.

    Raises:
        Unsupported: External services are disabled by configuration.
        RateLimited: The service asked us to back off and kept asking.
        UpstreamError: Anything else, named so the caller can say which
            service failed rather than reporting a generic network problem.
    """
    if not settings.allow_external_services:
        raise Unsupported(
            "External lookups are disabled.",
            hint="Set ZOTERO_MCP_ALLOW_EXTERNAL=1 to enable metadata and open-access lookups.",
        )

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise Unsupported("HTTP lookups need requests.") from exc

    request_headers = {"User-Agent": _user_agent(settings), "Accept": accept}
    request_headers.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(max(1, settings.max_retries)):
        try:
            response = requests.get(url, headers=request_headers, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 (requests raises a family of these)
            last_error = exc
            _sleep(settings, attempt)
            continue

        if response.status_code in _RETRY_STATUS:
            last_error = UpstreamError(f"{url} returned HTTP {response.status_code}")
            _sleep(settings, attempt, response.headers.get("Retry-After"))
            continue

        if response.status_code == 404:
            return None
        if not response.ok:
            raise UpstreamError(f"{url} returned HTTP {response.status_code}")

        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError as exc:
                raise UpstreamError(f"{url} returned malformed JSON") from exc
        return response.text

    if isinstance(last_error, UpstreamError) and "429" in str(last_error):
        raise RateLimited(f"{url} is rate limiting us.", hint="Try again shortly.")
    raise UpstreamError(f"Could not reach {url}: {last_error}")


def download(url: str, *, settings: NetworkSettings, timeout: float = 60.0) -> bytes | None:
    """Fetch binary content, or None if it is not there."""
    if not settings.allow_external_services:
        raise Unsupported(
            "External downloads are disabled.",
            hint="Set ZOTERO_MCP_ALLOW_EXTERNAL=1 to enable open-access PDF retrieval.",
        )
    import requests

    try:
        response = requests.get(
            url,
            headers={"User-Agent": _user_agent(settings)},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Download failed for %s: %s", url, exc)
        return None
    if not response.ok or not response.content:
        return None
    return response.content


def _user_agent(settings: NetworkSettings) -> str:
    """Identify ourselves, with a contact address when one is configured.

    Crossref and Unpaywall route requests carrying a mailto to a faster, more
    reliable pool. Sending one is both polite and in the caller's interest.
    """
    from zotero_mcp._version import __version__

    agent = f"{settings.user_agent}/{__version__}"
    if settings.contact_email:
        agent += f" (mailto:{settings.contact_email})"
    return agent


def _sleep(settings: NetworkSettings, attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(min(float(retry_after), 10.0))
            return
        except ValueError:
            pass
    time.sleep(settings.backoff_seconds * (2**attempt))


__all__ = ["download", "get"]
