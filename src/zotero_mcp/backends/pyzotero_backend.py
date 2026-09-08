# Copyright 2026 Vinay

"""Backend over pyzotero, used for both the Web API and Zotero's local API.

The two differ only in transport and in whether writes are permitted, so they
share an implementation and are distinguished by :class:`WebBackend` and
:class:`LocalHttpBackend` below.

Two pyzotero behaviours shape everything here.

**Parameters accumulate.** ``add_parameters`` merges into ``self.url_params``
rather than replacing it, so a client carries the last call's ``itemType``,
``limit`` and ``q`` into the next one. Under FastMCP, synchronous tools run in a
thread pool, so two concurrent calls sharing a client interleave their
parameters and quietly return the wrong result set. Every request here goes
through :meth:`_request`, which holds the shared lock and clears ``url_params``
first, so the parameters sent are exactly the ones passed, always.

**Totals live on the response, not the payload.** ``Total-Results`` is an HTTP
header, so it has to be read off the client immediately after the call that set
it, while the lock is still held.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pyzotero import zotero
from pyzotero import zotero_errors as ze

from zotero_mcp.backends.base import ItemQuery, LibraryBackend, RawPage, WriteOutcome, api_lock
from zotero_mcp.config import LibrarySettings, ZoteroConfig
from zotero_mcp.errors import (
    AuthError,
    BackendUnavailable,
    InvalidInput,
    NotFound,
    RateLimited,
    UpstreamError,
    WriteConflict,
)
from zotero_mcp.models import LibraryRef

logger = logging.getLogger(__name__)


def _translate(exc: Exception, *, context: str) -> Exception:
    """Map a pyzotero exception onto this package's typed errors.

    Doing this once, here, is what lets every tool above simply let errors
    propagate: by the time one reaches a tool boundary it already carries a
    code and a hint.
    """
    if isinstance(exc, ze.ResourceNotFoundError):
        return NotFound(f"{context}: not found in this library.")
    if isinstance(exc, ze.UserNotAuthorisedError):
        return AuthError(
            f"{context}: the Zotero API key was rejected.",
            hint="Check ZOTERO_API_KEY, and that the key grants access to this library.",
        )
    if isinstance(exc, ze.MissingCredentialsError):
        return AuthError(
            f"{context}: no Zotero credentials configured.",
            hint="Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID, or ZOTERO_LOCAL=true.",
        )
    if isinstance(exc, ze.PreConditionFailedError | ze.ConflictError):
        return WriteConflict(
            f"{context}: the item changed in Zotero since it was read.",
            hint="Re-read the item to get its current version, then re-apply the change.",
        )
    if isinstance(exc, ze.PreConditionRequiredError):
        return WriteConflict(
            f"{context}: Zotero requires the item's current version for this write.",
            hint="Re-read the item and retry.",
        )
    if isinstance(exc, ze.TooManyRequestsError | ze.TooManyRetriesError):
        return RateLimited(f"{context}: Zotero asked us to slow down.", hint="Retry shortly.")
    if isinstance(exc, ze.InvalidItemFieldsError | ze.UnsupportedParamsError):
        return InvalidInput(f"{context}: Zotero rejected the request. {exc}")
    if isinstance(exc, ze.CouldNotReachURLError):
        return BackendUnavailable(
            f"{context}: could not reach Zotero.",
            hint=(
                "In local mode the Zotero desktop application must be running and "
                "'Allow other applications on this computer to communicate with Zotero' "
                "enabled in Settings → Advanced."
            ),
        )
    if isinstance(exc, ze.FileDoesNotExistError):
        return NotFound(f"{context}: the attachment has no stored file.")
    if isinstance(exc, ze.PyZoteroError):
        return UpstreamError(f"{context}: {type(exc).__name__}: {exc}")
    return exc


class PyzoteroBackend(LibraryBackend):
    """Shared implementation over a ``pyzotero.zotero.Zotero`` client."""

    name = "pyzotero"

    def __init__(self, settings: LibrarySettings, *, local: bool, writable: bool) -> None:
        self._settings = settings
        self._local = local
        self.writable = writable
        self.has_local_files = local

        library_id = settings.library_id or ("0" if local else None)
        if not local and not (library_id and settings.api_key):
            raise AuthError(
                "The Zotero Web API needs both a library id and an API key.",
                hint=(
                    "Set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY, or set ZOTERO_LOCAL=true "
                    "to read from the Zotero desktop application instead."
                ),
            )

        self._library_id = str(library_id)
        self._library_type = settings.library_type.value
        self._client = zotero.Zotero(
            library_id=self._library_id,
            library_type=self._library_type,
            api_key=settings.api_key,
            local=local,
        )

    # -- plumbing ----------------------------------------------------------
    def _request(self, method_name: str, *args: Any, **params: Any) -> Any:
        """Call a pyzotero method with exactly the parameters given.

        Clearing ``url_params`` first is the load-bearing part: without it the
        client carries the previous call's filters into this one.
        """
        with api_lock():
            self._client.url_params = None
            try:
                return getattr(self._client, method_name)(*args, **params)
            except Exception as exc:
                raise _translate(exc, context=method_name) from exc

    def _request_with_total(
        self, method_name: str, *args: Any, **params: Any
    ) -> tuple[Any, int | None]:
        """As :meth:`_request`, also reading the ``Total-Results`` header.

        The header must be read under the same lock as the call that produced
        it, or a concurrent request will have replaced it.
        """
        with api_lock():
            self._client.url_params = None
            try:
                result = getattr(self._client, method_name)(*args, **params)
            except Exception as exc:
                raise _translate(exc, context=method_name) from exc
            return result, self._read_total()

    def _read_total(self) -> int | None:
        request = getattr(self._client, "request", None)
        headers = getattr(request, "headers", None) or {}
        raw = headers.get("Total-Results")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _query_params(spec: ItemQuery) -> dict[str, Any]:
        """Translate an :class:`ItemQuery` into Zotero API parameters."""
        params: dict[str, Any] = {"start": spec.offset, "limit": spec.limit}
        if spec.query:
            params["q"] = spec.query
            params["qmode"] = spec.qmode
        if spec.item_type:
            params["itemType"] = spec.item_type
        if spec.tags:
            # Zotero ANDs a list of tag parameters and ORs within one string
            # using '||'; passing the list is the AND we want.
            params["tag"] = list(spec.tags)
        if spec.since is not None:
            params["since"] = spec.since
        if spec.sort:
            params["sort"] = spec.sort
        if spec.direction:
            params["direction"] = spec.direction
        if spec.include_trashed:
            params["includeTrashed"] = 1
        return params

    # -- identity ----------------------------------------------------------
    def library_ref(self) -> LibraryRef:
        return LibraryRef(library_id=self._library_id, library_type=self._library_type)  # type: ignore[arg-type]

    def ping(self) -> bool:
        try:
            self._request("items", limit=1)
            return True
        except Exception:  # noqa: BLE001 (unreachable is the answer, not an error)
            return False

    # -- items -------------------------------------------------------------
    def get_item(self, key: str) -> dict[str, Any] | None:
        try:
            return self._request("item", key)
        except NotFound:
            return None

    def get_items(self, spec: ItemQuery) -> RawPage:
        params = self._query_params(spec)
        if spec.collection_key:
            items, total = self._request_with_total(
                "collection_items", spec.collection_key, **params
            )
        elif spec.top_level_only:
            items, total = self._request_with_total("top", **params)
        else:
            items, total = self._request_with_total("items", **params)
        return RawPage(items=list(items or []), total=total, offset=spec.offset)

    def get_children(self, key: str, *, item_type: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 100}
        if item_type:
            params["itemType"] = item_type
        return list(self._request("children", key, **params) or [])

    def get_item_versions(self, *, since: int | None = None) -> dict[str, int]:
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        return dict(self._request("item_versions", **params) or {})

    def count_items(self) -> int | None:
        try:
            return int(self._request("count_items"))
        except Exception:  # noqa: BLE001 (a missing count is not a failed read)
            return None

    # -- collections -------------------------------------------------------
    def get_collection(self, key: str) -> dict[str, Any] | None:
        try:
            return self._request("collection", key)
        except NotFound:
            return None

    def get_collections(
        self, *, parent_key: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage:
        params = {"start": offset, "limit": limit}
        if parent_key:
            result, total = self._request_with_total("collections_sub", parent_key, **params)
        else:
            result, total = self._request_with_total("collections", **params)
        return RawPage(items=list(result or []), total=total, offset=offset)

    def get_all_collections(self) -> list[dict[str, Any]]:
        # all_collections() walks sub-collections recursively; the count is in
        # the hundreds even for large libraries, so this stays bounded.
        return list(self._request("all_collections") or [])

    # -- tags --------------------------------------------------------------
    def get_tags(
        self, *, filter_text: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage:
        params: dict[str, Any] = {"start": offset, "limit": limit}
        if filter_text:
            params["q"] = filter_text
        result, total = self._request_with_total("tags", **params)
        return RawPage(
            items=[{"tag": t} if isinstance(t, str) else t for t in result or []],
            total=total,
            offset=offset,
        )

    # -- trash -------------------------------------------------------------
    def get_trash(self, *, offset: int = 0, limit: int = 25) -> RawPage:
        result, total = self._request_with_total("trash", start=offset, limit=limit)
        return RawPage(items=list(result or []), total=total, offset=offset)

    # -- content -----------------------------------------------------------
    def get_fulltext(self, attachment_key: str) -> str | None:
        try:
            payload = self._request("fulltext_item", attachment_key)
        except (NotFound, UpstreamError):
            return None
        if isinstance(payload, dict):
            return payload.get("content") or None
        return None

    def get_attachment_bytes(self, attachment_key: str) -> bytes | None:
        try:
            return self._request("file", attachment_key)
        except (NotFound, UpstreamError):
            return None

    def resolve_attachment_path(self, attachment_key: str) -> Path | None:
        """Download an attachment to a temporary file and return its path.

        The directory is created with a recognisable prefix so that
        :mod:`zotero_mcp.content` can tell a file it downloaded (safe to
        remove) from one inside the user's Zotero storage (never remove).
        """
        data = self.get_attachment_bytes(attachment_key)
        if data is None:
            return None
        directory = Path(tempfile.mkdtemp(prefix="zotero_mcp_dl_"))
        item = self.get_item(attachment_key) or {}
        filename = (item.get("data") or {}).get("filename") or f"{attachment_key}.pdf"
        target = directory / os.path.basename(filename)
        target.write_bytes(data)
        return target

    # -- library-level -----------------------------------------------------
    def list_libraries(self) -> list[LibraryRef]:
        libraries = [
            LibraryRef(library_id=self._library_id, library_type="user", name="My Library")
        ]
        try:
            for group in self._request("groups") or []:
                data = group.get("data") or {}
                libraries.append(
                    LibraryRef(
                        library_id=str(group.get("id") or data.get("id") or ""),
                        library_type="group",
                        name=data.get("name") or "Group library",
                    )
                )
        except Exception as exc:  # noqa: BLE001 (listing groups is optional)
            # Group enumeration needs a key with group scope; a local backend
            # has none at all. Not being able to list groups is not a failure
            # of "which library am I on".
            logger.debug("Could not enumerate group libraries: %s", exc)
        return libraries

    def get_saved_searches(self) -> list[dict[str, Any]]:
        return list(self._request("searches") or [])

    def item_template(self, item_type: str) -> dict[str, Any]:
        return dict(self._request("item_template", item_type) or {})

    # -- writes ------------------------------------------------------------
    def _require_writable(self, action: str) -> None:
        """Refuse a write before attempting it.

        The local HTTP API is read-only, so reaching Zotero with a write would
        surface as a bare 405 with no explanation of what to do instead.
        """
        if not self.writable:
            raise self._refuse(action)

    @staticmethod
    def _outcome_from_response(response: Any, keys: list[str] | None = None) -> WriteOutcome:
        """Read Zotero's per-index success/failure report into a WriteOutcome.

        Zotero's batch write endpoints return ``{"success": {...}, "failed":
        {...}, "unchanged": {...}}`` keyed by the *submitted index*, not by item
        key. Treating a 200 as "everything worked", which the predecessors do
        in several places, silently loses per-item failures.
        """
        outcome = WriteOutcome()
        if not isinstance(response, dict):
            for key in keys or []:
                outcome.succeeded[key] = 0
            return outcome

        for _index, key in (response.get("success") or {}).items():
            outcome.succeeded[str(key)] = 0
        for index, detail in (response.get("failed") or {}).items():
            label = (
                keys[int(index)]
                if keys and str(index).isdigit() and int(index) < len(keys)
                else str(index)
            )
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            outcome.failed[label] = message or "rejected by Zotero"
        for _index, key in (response.get("unchanged") or {}).items():
            outcome.unchanged.append(str(key))
        return outcome

    def create_items(self, payloads: list[dict[str, Any]]) -> WriteOutcome:
        self._require_writable("create items")
        response = self._request("create_items", payloads)
        return self._outcome_from_response(response)

    def update_item(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        self._require_writable("update items")
        # pyzotero reads the version off the payload and sends it as
        # If-Unmodified-Since-Version, which is what turns a concurrent desktop
        # edit into a 412 instead of a silent overwrite.
        payload = {**patch, "key": key, "version": version}
        self._request("update_item", payload)
        return WriteOutcome(succeeded={key: version + 1})

    def delete_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        self._require_writable("delete items")
        outcome = WriteOutcome()
        for key, version in versions_by_key.items():
            try:
                self._request("delete_item", {"key": key, "version": version})
                outcome.succeeded[key] = version
            except Exception as exc:  # noqa: BLE001 (one failure must not abort the batch)
                outcome.failed[key] = str(exc)
        return outcome

    def _set_deleted_flag(self, versions_by_key: dict[str, int], deleted: int) -> WriteOutcome:
        """Move items to or from the trash.

        Zotero exposes no trash endpoint. Trashing is a normal item update that
        sets ``deleted``, which means it is versioned like any other write and
        a stale version produces a conflict rather than a silent no-op.
        """
        outcome = WriteOutcome()
        for key, version in versions_by_key.items():
            try:
                self._request("update_item", {"key": key, "version": version, "deleted": deleted})
                outcome.succeeded[key] = version + 1
            except Exception as exc:  # noqa: BLE001 (one failure must not abort the batch)
                outcome.failed[key] = str(exc)
        return outcome

    def trash_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        self._require_writable("trash items")
        return self._set_deleted_flag(versions_by_key, 1)

    def restore_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        self._require_writable("restore items")
        return self._set_deleted_flag(versions_by_key, 0)

    def empty_trash(self) -> WriteOutcome:
        """Permanently delete everything in the trash.

        Paged rather than read whole: a neglected trash can hold tens of
        thousands of items, and this must not depend on all of them fitting in
        memory at once. Each page is re-read from offset 0 because deleting
        shifts everything after it.
        """
        self._require_writable("empty the trash")
        outcome = WriteOutcome()
        while True:
            page = self.get_trash(offset=0, limit=50)
            if not page.items:
                break
            batch = {
                item.get("key"): item.get("version", 0) for item in page.items if item.get("key")
            }
            if not batch:
                break
            result = self.delete_items(batch)
            outcome.succeeded.update(result.succeeded)
            outcome.failed.update(result.failed)
            # Every item in the page failed, so another pass would loop forever.
            if not result.succeeded:
                break
        return outcome

    def create_collection(self, name: str, *, parent_key: str | None = None) -> WriteOutcome:
        self._require_writable("create collections")
        payload: dict[str, Any] = {"name": name}
        if parent_key:
            payload["parentCollection"] = parent_key
        response = self._request("create_collections", [payload])
        return self._outcome_from_response(response)

    def update_collection(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        self._require_writable("update collections")
        payload = {**patch, "key": key, "version": version}
        self._request("update_collection", payload)
        return WriteOutcome(succeeded={key: version + 1})

    def delete_collection(self, key: str, *, version: int) -> WriteOutcome:
        self._require_writable("delete collections")
        self._request("delete_collection", {"key": key, "version": version})
        return WriteOutcome(succeeded={key: version})

    def add_to_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        self._require_writable("add items to collections")
        outcome = WriteOutcome()
        for key in item_keys:
            item = self.get_item(key)
            if item is None:
                outcome.failed[key] = "item not found"
                continue
            try:
                self._request("addto_collection", collection_key, item)
                outcome.succeeded[key] = item.get("version", 0)
            except Exception as exc:  # noqa: BLE001 (one failure must not abort the batch)
                outcome.failed[key] = str(exc)
        return outcome

    def remove_from_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        self._require_writable("remove items from collections")
        outcome = WriteOutcome()
        for key in item_keys:
            item = self.get_item(key)
            if item is None:
                outcome.failed[key] = "item not found"
                continue
            try:
                self._request("deletefrom_collection", collection_key, item)
                outcome.succeeded[key] = item.get("version", 0)
            except Exception as exc:  # noqa: BLE001 (one failure must not abort the batch)
                outcome.failed[key] = str(exc)
        return outcome

    def attach_file(
        self,
        parent_key: str,
        path: Path,
        *,
        title: str | None = None,
        link_mode: str = "imported_file",
    ) -> WriteOutcome:
        self._require_writable("attach files")
        if link_mode == "linked_file":
            # Zotero's Web API cannot create linked files: the path is
            # meaningless to the server. Saying so beats a confusing 400.
            raise InvalidInput(
                "Linked-file attachments can only be created by the Zotero desktop application.",
                hint="Use link_mode='imported_file' to upload a copy instead.",
            )
        response = self._request("attachment_simple", [str(path)], parent_key)
        return self._outcome_from_response(response)

    def attach_link(self, parent_key: str, url: str, *, title: str | None = None) -> WriteOutcome:
        self._require_writable("attach links")
        template = self.item_template("attachment")
        template.update(
            {
                "linkMode": "linked_url",
                "url": url,
                "title": title or url,
                "parentItem": parent_key,
            }
        )
        response = self._request("create_items", [template])
        return self._outcome_from_response(response)


class WebBackend(PyzoteroBackend):
    """The Zotero Web API. Full read/write, needs credentials, needs network."""

    name = "web"

    def __init__(self, settings: LibrarySettings) -> None:
        super().__init__(settings, local=False, writable=True)


class LocalHttpBackend(PyzoteroBackend):
    """Zotero's local HTTP API on port 23119. Read-only, no credentials, no network."""

    name = "local-http"

    def __init__(self, settings: LibrarySettings) -> None:
        super().__init__(settings, local=True, writable=False)

    def ping(self) -> bool:
        # The desktop client being closed is by far the most common cause of a
        # broken local setup, so this probe is the one the health tool reports.
        try:
            self._request("items", limit=1)
            return True
        except Exception as exc:  # noqa: BLE001 (unreachable is the answer)
            logger.debug("Local Zotero API not reachable: %s", exc)
            return False


def build_pyzotero_backend(config: ZoteroConfig, *, local: bool) -> PyzoteroBackend:
    """Construct the appropriate pyzotero-backed backend for *config*."""
    return LocalHttpBackend(config.library) if local else WebBackend(config.library)


__all__ = [
    "LocalHttpBackend",
    "PyzoteroBackend",
    "WebBackend",
    "build_pyzotero_backend",
]
