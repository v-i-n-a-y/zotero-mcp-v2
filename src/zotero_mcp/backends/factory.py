# Copyright 2026 Vinay

"""Choosing a backend, once, at startup.

:data:`LibraryMode.AUTO` probes; every other mode is taken at face value. The
probe is deliberately cheap and deliberately non-fatal: a server that refuses
to start because Zotero happens to be closed is worse than one that starts,
reports the problem through its health tool, and works the moment Zotero opens.
"""

from __future__ import annotations

import logging

from zotero_mcp.backends.base import LibraryBackend
from zotero_mcp.backends.hybrid import HybridBackend
from zotero_mcp.backends.pyzotero_backend import LocalHttpBackend, WebBackend
from zotero_mcp.config import LibraryMode, ZoteroConfig
from zotero_mcp.errors import AuthError, BackendUnavailable

logger = logging.getLogger(__name__)


def _has_web_credentials(config: ZoteroConfig) -> bool:
    return bool(config.library.library_id and config.library.api_key)


def build_backend(config: ZoteroConfig) -> LibraryBackend:
    """Construct the backend named (or implied) by *config*.

    Raises:
        AuthError: Web or hybrid mode was asked for without credentials.
        BackendUnavailable: Local mode was asked for and Zotero is not running.
    """
    mode = config.library.mode

    if mode is LibraryMode.WEB:
        return WebBackend(config.library)

    if mode is LibraryMode.LOCAL:
        backend = LocalHttpBackend(config.library)
        if not backend.ping():
            raise BackendUnavailable(
                "Local mode is configured but the Zotero desktop application is not responding.",
                hint=(
                    "Start Zotero, and enable Settings -> Advanced -> "
                    "'Allow other applications on this computer to communicate with Zotero'."
                ),
            )
        return backend

    if mode is LibraryMode.HYBRID:
        if not _has_web_credentials(config):
            raise AuthError(
                "Hybrid mode needs Web API credentials for its write half.",
                hint="Set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY, or use ZOTERO_MCP_MODE=local.",
            )
        return HybridBackend(LocalHttpBackend(config.library), WebBackend(config.library))

    return _auto(config)


def _auto(config: ZoteroConfig) -> LibraryBackend:
    """Resolve AUTO by probing, preferring the arrangement that can do most.

    Order of preference: hybrid (local reads plus working writes), then
    web-only, then local-only. Local-only comes last because a read-only
    server is the most limited outcome, not because local reads are worse.
    """
    local = LocalHttpBackend(config.library)
    local_up = local.ping()
    has_credentials = _has_web_credentials(config)

    if local_up and has_credentials:
        logger.info("Auto-detected hybrid mode: local reads, Web API writes.")
        return HybridBackend(local, WebBackend(config.library))

    if has_credentials:
        logger.info("Auto-detected web mode: Zotero desktop is not responding.")
        return WebBackend(config.library)

    if local_up:
        logger.info("Auto-detected local mode: no Web API credentials configured.")
        return local

    raise BackendUnavailable(
        "No way to reach Zotero: the desktop application is not responding and "
        "no Web API credentials are configured.",
        hint=(
            "Either start Zotero (and enable its local API in Settings -> Advanced), "
            "or set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY for the Web API."
        ),
    )


__all__ = ["build_backend"]
