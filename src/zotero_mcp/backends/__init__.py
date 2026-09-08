"""The Zotero backend: one thin wrapper over pyzotero.

Everything that actually touches Zotero — the local desktop HTTP API or the web
API — goes through :class:`ZoteroBackend`. The tools never import pyzotero
directly, so the network seam is in one place: one class to mock in tests, one
place that translates pyzotero's exceptions into this package's typed errors.

The backend returns raw Zotero payloads (the dicts pyzotero hands back) and
leaves mapping to models to :mod:`zotero_mcp.mapping`. That keeps this module
about *reachability and transport* and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from zotero_mcp.config import LibraryMode, ZoteroConfig
from zotero_mcp.errors import (
    AuthError,
    BackendUnavailable,
    NotFound,
    Unsupported,
    WriteConflict,
)
from zotero_mcp.models import LibraryRef

logger = logging.getLogger(__name__)


def _use_local(mode: LibraryMode) -> bool:
    """Whether to talk to the Zotero desktop client rather than the web API.

    HYBRID reads locally (fast, offline) and would write over the web; this
    read-only surface only needs the local half. AUTO defaults to local, which
    is the right guess for a user who has the desktop client open and is the
    only mode that works without credentials.
    """
    return mode in (LibraryMode.LOCAL, LibraryMode.HYBRID, LibraryMode.AUTO)


class ZoteroBackend:
    """Read access to one Zotero library."""

    def __init__(self, config: ZoteroConfig) -> None:
        self.config = config
        self.local = _use_local(config.library.mode)
        self.library_id = config.library.library_id or "0"
        self.library_type = config.library.library_type.value
        self._client: Any = None
        self._write_client: Any = None

    # -- connection ---------------------------------------------------------

    @property
    def client(self) -> Any:
        """The lazily constructed pyzotero client.

        Built on first use so that importing the package, listing tools, or
        printing ``--version`` never requires pyzotero or a reachable library.
        """
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> Any:
        try:
            from pyzotero import zotero
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise BackendUnavailable(
                "pyzotero is not installed",
                hint="Reinstall the server: it declares pyzotero as a dependency.",
            ) from exc

        if not self.local and not self.config.library.api_key:
            raise AuthError(
                "Web mode needs an API key",
                hint="Set ZOTERO_API_KEY, or set ZOTERO_LOCAL=true to read the desktop client.",
            )

        return zotero.Zotero(
            library_id=self.library_id,
            library_type=self.library_type,
            api_key=self.config.library.api_key,
            local=self.local,
        )

    # -- write connection ---------------------------------------------------

    @property
    def can_write(self) -> bool:
        """Whether this configuration can mutate the library.

        Writes always go over the web API and need an API key and a real
        numeric library id. Local mode is read-only, and the local ``"0"``
        library placeholder cannot be written to.
        """
        return bool(
            self.config.library.api_key
            and self.library_id
            and self.library_id != "0"
            and self.config.library.mode is not LibraryMode.LOCAL
        )

    @property
    def write_client(self) -> Any:
        """The web-API pyzotero client used for mutations.

        Distinct from :attr:`client`: in hybrid mode reads come from the local
        desktop API but writes must reach api.zotero.org, so a mutation can
        never accidentally run against the read-only local endpoint.
        """
        if not self.can_write:
            if not self.config.library.api_key:
                raise Unsupported(
                    "This server is read-only: no API key is configured.",
                    hint="Set ZOTERO_API_KEY (and a numeric ZOTERO_LIBRARY_ID) to enable writes.",
                )
            raise Unsupported(
                "Writes need a numeric library id; the local '0' placeholder cannot be written.",
                hint="Set ZOTERO_LIBRARY_ID to your real library id.",
            )
        if self._write_client is None:
            from pyzotero import zotero

            self._write_client = zotero.Zotero(
                library_id=self.library_id,
                library_type=self.library_type,
                api_key=self.config.library.api_key,
                local=False,
            )
        return self._write_client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a pyzotero method, translating its failures to typed errors."""
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except BackendUnavailable:
            raise
        except Exception as exc:  # classified into a typed error just below
            raise self._translate(exc) from exc

    def _translate(self, exc: Exception) -> Exception:
        name = type(exc).__name__
        text = str(exc)
        lowered = text.lower()
        if "preconditionfailed" in name.lower() or "412" in text:
            return WriteConflict(
                "The item changed in Zotero since it was read.",
                hint="Re-read the item with get_item to pick up the current version, then retry.",
            )
        if "resourcenotfound" in name.lower() or "404" in text or "does not exist" in lowered:
            return NotFound(
                "The requested item, collection or attachment does not exist.",
                hint="Check the key; it must come from a search or listing in this library.",
            )
        if name in {"UserNotAuthorised", "MissingCredentials"} or "403" in text or "401" in text:
            return AuthError(
                text or "Zotero rejected the credentials",
                hint="Check ZOTERO_API_KEY and that the key can read this library.",
            )
        # Connection refused/timeouts against the local API are the classic
        # "Zotero desktop is closed" case.
        if self.local and (
            "Connection" in name
            or "Connection" in text
            or "refused" in text.lower()
            or "Max retries" in text
        ):
            return BackendUnavailable(
                "The Zotero desktop client is not answering",
                hint="Open Zotero (it serves the local API on port 23119), then retry.",
            )
        return BackendUnavailable(f"{name}: {text}")

    # -- reads --------------------------------------------------------------

    def library_ref(self) -> LibraryRef:
        return LibraryRef(library_id=self.library_id, library_type=self.library_type)

    def search(
        self, query: str | None, *, item_type: str | None, limit: int, start: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "start": start}
        if query:
            params["q"] = query
            params["qmode"] = "everything"
        if item_type:
            params["itemType"] = item_type
        # top() excludes child notes/attachments, which is what a search result
        # list should show; a raw items() call would surface orphan children.
        return (
            self._call("top", **params)
            if not query and not item_type
            else self._call("items", **params)
        )

    def item(self, key: str) -> dict[str, Any]:
        return self._call("item", key)

    def children(self, key: str) -> list[dict[str, Any]]:
        return self._call("children", key)

    def collections(self) -> list[dict[str, Any]]:
        return self._call("everything", self._call("collections"))

    def collection_items(
        self, collection_key: str, *, limit: int, start: int
    ) -> list[dict[str, Any]]:
        return self._call("collection_items_top", collection_key, limit=limit, start=start)

    def tags(self, *, limit: int) -> list[str]:
        return self._call("tags", limit=limit)

    def all_tags(self) -> list[str]:
        """Every tag in the library, paged through to completion.

        Used for the true tag count in stats, where a single capped page would
        report a floor as if it were the total.
        """
        return self._call("everything", self._call("tags"))

    def all_items(self) -> list[dict[str, Any]]:
        """Every item in the library, children included, paged to completion.

        One sweep (``everything(items())``) rather than a per-item children
        walk: the indexer groups children by ``parentItem`` itself, turning
        thousands of round-trips into one paginated fetch.
        """
        return self._call("everything", self._call("items"))

    def fulltext(self, attachment_key: str) -> str:
        """Zotero's own extracted fulltext for an attachment, or "" if none.

        Reusing the text Zotero already indexed avoids re-parsing a PDF the
        desktop client has parsed already; the indexer only falls back to
        downloading and extracting when this is empty.
        """
        try:
            result = self._call("fulltext_item", attachment_key)
        except NotFound:
            return ""
        if isinstance(result, dict):
            return str(result.get("content") or "")
        return str(result or "")

    def file_bytes(self, attachment_key: str) -> bytes:
        """Raw bytes of an attachment file (for local extraction fallback)."""
        return self._call("file", attachment_key)

    def count(self) -> int:
        try:
            return int(self._call("count_items"))
        except Exception:  # noqa: BLE001 - count is best-effort for stats
            return int(self._call("num_items"))

    # -- writes -------------------------------------------------------------

    def _wcall(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.write_client, method)(*args, **kwargs)
        except (Unsupported, WriteConflict, NotFound, AuthError):
            raise
        except Exception as exc:  # translated into a typed write error
            raise self._translate(exc) from exc

    def item_template(self, item_type: str) -> dict[str, Any]:
        return dict(self._wcall("item_template", item_type))

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create items; returns pyzotero's {successful, success, failed} report."""
        return self._wcall("create_items", items)

    def update_item(self, item: dict[str, Any]) -> int | None:
        """Update an item, returning its new version.

        pyzotero sends ``If-Unmodified-Since-Version`` from ``item['version']``
        and returns only a bool, so the authoritative post-write version is read
        from the response's ``Last-Modified-Version`` header rather than guessed.
        """
        self._wcall("update_item", item)
        return self._last_write_version()

    def _last_write_version(self) -> int | None:
        req = getattr(self.write_client, "request", None)
        if req is None:
            return None
        raw = req.headers.get("Last-Modified-Version") or req.headers.get("last-modified-version")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def trash_item(self, item: dict[str, Any]) -> None:
        """Move an item to Zotero's Trash — recoverable, not a permanent delete.

        pyzotero's ``delete_item`` destroys the item outright; a PATCH of
        ``{"deleted": 1}`` guarded by the version instead moves it to the Trash,
        which the user can restore from the desktop client.
        """
        from pyzotero.zotero import build_url

        zot = self.write_client
        url = build_url(
            zot.endpoint,
            f"/{zot.library_type}/{zot.library_id}/items/{item['key']}",
        )
        import json as _json

        try:
            resp = zot.client.patch(
                url=url,
                headers={"If-Unmodified-Since-Version": str(item["version"])},
                content=_json.dumps({"deleted": 1}),
            )
        except Exception as exc:  # network/transport failure
            raise self._translate(exc) from exc
        if resp.status_code == 412:
            raise WriteConflict(
                "The item changed in Zotero since it was read.",
                hint="Re-read it with get_item, then retry the delete.",
            )
        if resp.status_code not in (200, 204):
            raise BackendUnavailable(
                f"Zotero rejected the delete (HTTP {resp.status_code}): {resp.text[:200]}"
            )

    def add_to_collection(self, collection_key: str, item: dict[str, Any]) -> Any:
        return self._wcall("addto_collection", collection_key, item)

    def remove_from_collection(self, collection_key: str, item: dict[str, Any]) -> Any:
        return self._wcall("deletefrom_collection", collection_key, item)

    def write_item(self, key: str) -> dict[str, Any]:
        """Fetch an item through the *write* client, so its version matches writes.

        A version read from the local backend can lag the web API; reading the
        item we are about to mutate through the same client we mutate with keeps
        optimistic locking honest.
        """
        return self._wcall("item", key)

    def ping(self) -> bool:
        """Cheapest possible reachability check."""
        self._call("key_info") if not self.local else self._call("num_items")
        return True


def make_backend(config: ZoteroConfig) -> ZoteroBackend:
    return ZoteroBackend(config)


__all__ = ["ZoteroBackend", "make_backend"]
