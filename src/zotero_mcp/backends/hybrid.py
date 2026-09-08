# Copyright 2026 Vinay

"""Reads from the fastest local source, writes through the Web API.

This is what ``ZOTERO_LOCAL=true`` plus an API key has always meant in
practice, and the predecessors expressed it by scattering "if local mode, use
the other client" branches through individual tools. Here it is one object:
reads go to the local backend, writes go to the web backend, and no tool
above ever learns that there are two.

The one rule that matters is that reads and writes must agree about item
versions. Zotero's local API and Web API report the same ``version`` for an
item as long as the library is synced; when it is not, a write built on a
locally read version is rejected with a conflict rather than clobbering the
server's copy. That is the correct outcome, and :meth:`get_item_for_write`
exists so a tool can deliberately re-read through the write path when it
wants the authoritative version.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zotero_mcp.backends.base import ItemQuery, LibraryBackend, RawPage, WriteOutcome
from zotero_mcp.models import LibraryRef

logger = logging.getLogger(__name__)


class HybridBackend(LibraryBackend):
    """Compose a read backend and a write backend into one interface."""

    name = "hybrid"
    writable = True

    def __init__(self, reader: LibraryBackend, writer: LibraryBackend) -> None:
        self._reader = reader
        self._writer = writer
        self.has_local_files = reader.has_local_files
        self.name = f"hybrid({reader.name}+{writer.name})"

    # -- identity ----------------------------------------------------------
    def library_ref(self) -> LibraryRef:
        # The writer is authoritative: it is the one that knows the real
        # library id, where a local reader may only know "0".
        return self._writer.library_ref()

    def ping(self) -> bool:
        return self._reader.ping()

    def ping_writer(self) -> bool:
        """Separate probe, so health output can say which half is broken."""
        return self._writer.ping()

    # -- reads -------------------------------------------------------------
    def get_item(self, key: str) -> dict[str, Any] | None:
        return self._reader.get_item(key)

    def get_item_for_write(self, key: str) -> dict[str, Any] | None:
        """Read an item through the *write* backend, for its authoritative version."""
        return self._writer.get_item(key)

    def get_items(self, spec: ItemQuery) -> RawPage:
        return self._reader.get_items(spec)

    def get_children(self, key: str, *, item_type: str | None = None) -> list[dict[str, Any]]:
        return self._reader.get_children(key, item_type=item_type)

    def get_item_versions(self, *, since: int | None = None) -> dict[str, int]:
        return self._reader.get_item_versions(since=since)

    def count_items(self) -> int | None:
        return self._reader.count_items()

    def get_collection(self, key: str) -> dict[str, Any] | None:
        return self._reader.get_collection(key)

    def get_collections(
        self, *, parent_key: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage:
        return self._reader.get_collections(parent_key=parent_key, offset=offset, limit=limit)

    def get_all_collections(self) -> list[dict[str, Any]]:
        return self._reader.get_all_collections()

    def get_tags(
        self, *, filter_text: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage:
        return self._reader.get_tags(filter_text=filter_text, offset=offset, limit=limit)

    def get_trash(self, *, offset: int = 0, limit: int = 25) -> RawPage:
        return self._reader.get_trash(offset=offset, limit=limit)

    def get_fulltext(self, attachment_key: str) -> str | None:
        text = self._reader.get_fulltext(attachment_key)
        if text:
            return text
        # Zotero indexes full text per client, so an attachment indexed on
        # another machine is present on the server but absent locally.
        return self._writer.get_fulltext(attachment_key)

    def resolve_attachment_path(self, attachment_key: str) -> Path | None:
        path = self._reader.resolve_attachment_path(attachment_key)
        if path is not None:
            return path
        # Falling back to a download covers the file-not-synced-here case,
        # which is otherwise an unexplained "cannot read this paper".
        return self._writer.resolve_attachment_path(attachment_key)

    def get_attachment_bytes(self, attachment_key: str) -> bytes | None:
        data = self._reader.get_attachment_bytes(attachment_key)
        return data if data is not None else self._writer.get_attachment_bytes(attachment_key)

    def list_libraries(self) -> list[LibraryRef]:
        return self._writer.list_libraries()

    def get_saved_searches(self) -> list[dict[str, Any]]:
        try:
            return self._reader.get_saved_searches()
        except Exception:  # noqa: BLE001 (a read-only backend may not offer them)
            return self._writer.get_saved_searches()

    def item_template(self, item_type: str) -> dict[str, Any]:
        return self._writer.item_template(item_type)

    # -- writes ------------------------------------------------------------
    def create_items(self, payloads: list[dict[str, Any]]) -> WriteOutcome:
        return self._writer.create_items(payloads)

    def update_item(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        return self._writer.update_item(key, patch, version=version)

    def delete_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        return self._writer.delete_items(versions_by_key)

    def trash_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        return self._writer.trash_items(versions_by_key)

    def restore_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        return self._writer.restore_items(versions_by_key)

    def empty_trash(self) -> WriteOutcome:
        return self._writer.empty_trash()

    def create_collection(self, name: str, *, parent_key: str | None = None) -> WriteOutcome:
        return self._writer.create_collection(name, parent_key=parent_key)

    def update_collection(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        return self._writer.update_collection(key, patch, version=version)

    def delete_collection(self, key: str, *, version: int) -> WriteOutcome:
        return self._writer.delete_collection(key, version=version)

    def add_to_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        return self._writer.add_to_collection(collection_key, item_keys)

    def remove_from_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        return self._writer.remove_from_collection(collection_key, item_keys)

    def attach_file(
        self,
        parent_key: str,
        path: Path,
        *,
        title: str | None = None,
        link_mode: str = "imported_file",
    ) -> WriteOutcome:
        return self._writer.attach_file(parent_key, path, title=title, link_mode=link_mode)

    def attach_link(self, parent_key: str, url: str, *, title: str | None = None) -> WriteOutcome:
        return self._writer.attach_link(parent_key, url, title=title)


__all__ = ["HybridBackend"]
