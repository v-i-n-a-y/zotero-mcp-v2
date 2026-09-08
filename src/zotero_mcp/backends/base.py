# Copyright 2026 Vinay

"""The only interface through which this package talks to Zotero.

Every "works against the web API but not locally" bug in the servers this
replaces traces to one habit: asking ``is_local_mode()`` inside individual
tools, thirty times over, each answer slightly different. A tool here never
asks. It is handed a :class:`LibraryBackend` and calls methods on it; which
concrete backend that is was decided once, at startup.

Backends deal in *raw Zotero payloads*, not models. Mapping raw dicts to
models is pure and lives in :mod:`zotero_mcp.mapping`, so it can be tested
against literal dicts, and a backend can be tested for the requests it makes
without a rendering layer in the way.

Reads are declared on the base class; writes raise :class:`Unsupported` by
default, so a read-only backend is written by simply not implementing them and
a caller gets a clear "this backend cannot write" rather than an AttributeError.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from zotero_mcp.errors import Unsupported
from zotero_mcp.models import LibraryRef

#: Serialises access to a pyzotero client.
#:
#: pyzotero's ``add_parameters`` *merges* into ``self.url_params`` rather than
#: replacing it, so parameters accumulate on the client across calls. FastMCP
#: runs synchronous tools in a thread pool, which means two concurrent tool
#: calls sharing a client will interleave their query parameters: a search
#: silently inheriting another call's ``itemType`` or ``limit``. Reentrant
#: because tools legitimately nest (add-by-URL calls add-by-DOI).
_API_LOCK = threading.RLock()

#: Bounded so a wedged request cannot make every other tool hang with no
#: explanation; the caller gets a real timeout error instead.
_LOCK_TIMEOUT_SECONDS = 120.0


@contextmanager
def api_lock() -> Iterator[None]:
    """Hold the shared Zotero API lock, with a bounded wait."""
    acquired = _API_LOCK.acquire(timeout=_LOCK_TIMEOUT_SECONDS)
    if not acquired:  # pragma: no cover - only under genuine contention
        from zotero_mcp.errors import BackendUnavailable

        raise BackendUnavailable(
            "Timed out waiting for the Zotero client; another operation is stuck.",
            hint="Retry shortly. If it persists, restart the MCP server.",
        )
    try:
        yield
    finally:
        _API_LOCK.release()


SortField = Literal[
    "dateAdded", "dateModified", "title", "creator", "date", "itemType", "publisher"
]


@dataclass(frozen=True)
class ItemQuery:
    """A request for a page of items.

    One object rather than fifteen keyword arguments repeated across four
    backends, so adding a filter means touching one definition. Frozen and
    hashable-by-value, which makes it usable directly as a paging cursor's
    query fingerprint.
    """

    query: str | None = None
    #: ``titleCreatorYear`` searches metadata; ``everything`` includes
    #: full-text content and is markedly slower.
    qmode: Literal["titleCreatorYear", "everything"] = "titleCreatorYear"
    #: Zotero's itemType filter syntax, including negation: ``-attachment``,
    #: ``book || bookSection``.
    item_type: str | None = "-attachment"
    tags: tuple[str, ...] = ()
    collection_key: str | None = None
    #: Restrict to items modified since this library version.
    since: int | None = None
    sort: SortField | None = None
    direction: Literal["asc", "desc"] | None = None
    #: Top-level items only, excluding child notes and attachments.
    top_level_only: bool = False
    include_trashed: bool = False
    offset: int = 0
    limit: int = 25

    def as_fingerprint(self) -> dict[str, Any]:
        """The parts that must match for a paging cursor to stay valid.

        Deliberately excludes ``offset``: paging is precisely the act of
        changing it.
        """
        return {
            "query": self.query,
            "qmode": self.qmode,
            "item_type": self.item_type,
            "tags": list(self.tags),
            "collection_key": self.collection_key,
            "since": self.since,
            "sort": self.sort,
            "direction": self.direction,
            "top_level_only": self.top_level_only,
            "include_trashed": self.include_trashed,
        }


@dataclass
class RawPage:
    """A page of raw Zotero payloads, with the library-wide total if known.

    ``total`` is ``None`` when the backend genuinely cannot say. Inventing one
    by exhausting the collection is the behaviour this whole layer exists to
    avoid, so no backend may fabricate it.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    offset: int = 0

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class WriteOutcome:
    """What a write did, per object.

    Zotero's batch endpoints report success and failure per index rather than
    failing the whole request, so a partial result is normal and must be
    representable.
    """

    succeeded: dict[str, int] = field(default_factory=dict)
    """Key to resulting library version."""

    failed: dict[str, str] = field(default_factory=dict)
    """Key (or submitted index, when no key exists yet) to failure message."""

    unchanged: list[str] = field(default_factory=list)


class LibraryBackend(ABC):
    """Read (and optionally write) access to one Zotero library."""

    #: Short identifier used in diagnostics and health output.
    name: str = "backend"

    #: Whether the write methods below do anything.
    writable: bool = False

    #: True when attachment files can be resolved without a network download.
    has_local_files: bool = False

    # -- identity ----------------------------------------------------------
    @abstractmethod
    def library_ref(self) -> LibraryRef:
        """Which library this backend is bound to."""

    @abstractmethod
    def ping(self) -> bool:
        """Cheap reachability probe. Must not raise."""

    # -- items -------------------------------------------------------------
    @abstractmethod
    def get_item(self, key: str) -> dict[str, Any] | None:
        """One item by key, or None if it does not exist."""

    @abstractmethod
    def get_items(self, spec: ItemQuery) -> RawPage:
        """One page of items matching *spec*."""

    @abstractmethod
    def get_children(self, key: str, *, item_type: str | None = None) -> list[dict[str, Any]]:
        """Child notes, attachments and annotations of an item."""

    def get_item_versions(self, *, since: int | None = None) -> dict[str, int]:
        """Key-to-version map for the whole library, for incremental sync."""
        raise Unsupported(f"{self.name} cannot report item versions.")

    def count_items(self) -> int | None:
        """Total item count, or None if the backend cannot say cheaply."""
        return None

    # -- collections -------------------------------------------------------
    @abstractmethod
    def get_collection(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_collections(
        self, *, parent_key: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage:
        """Collections, optionally restricted to the children of *parent_key*."""

    @abstractmethod
    def get_all_collections(self) -> list[dict[str, Any]]:
        """Every collection, for building the hierarchy.

        The one deliberate unbounded read in this interface: a full path tree
        cannot be built from a page, and collection counts are small (hundreds,
        not the hundreds of thousands an item listing can reach).
        """

    # -- tags --------------------------------------------------------------
    @abstractmethod
    def get_tags(
        self, *, filter_text: str | None = None, offset: int = 0, limit: int = 100
    ) -> RawPage: ...

    # -- trash -------------------------------------------------------------
    def get_trash(self, *, offset: int = 0, limit: int = 25) -> RawPage:
        raise Unsupported(f"{self.name} cannot read the trash.")

    # -- content -----------------------------------------------------------
    def get_fulltext(self, attachment_key: str) -> str | None:
        """Text from Zotero's own full-text index, if it has any."""
        return None

    def resolve_attachment_path(self, attachment_key: str) -> Path | None:
        """A readable path to an attachment's bytes, or None.

        Local backends return the file in Zotero's storage directory. Network
        backends download to a temporary file. Callers must therefore never
        delete a path returned from here, since it may be the user's only copy.
        """
        return None

    def get_attachment_bytes(self, attachment_key: str) -> bytes | None:
        return None

    # -- library-level -----------------------------------------------------
    def list_libraries(self) -> list[LibraryRef]:
        """Personal and group libraries reachable with these credentials."""
        return [self.library_ref()]

    def get_saved_searches(self) -> list[dict[str, Any]]:
        raise Unsupported(f"{self.name} cannot read saved searches.")

    def item_template(self, item_type: str) -> dict[str, Any]:
        """A blank item of *item_type* with every field Zotero expects."""
        raise Unsupported(f"{self.name} cannot supply item templates.")

    # -- writes ------------------------------------------------------------
    # Default implementations refuse rather than being absent, so a caller sees
    # "this backend cannot write" instead of AttributeError.

    def _refuse(self, action: str) -> Unsupported:
        return Unsupported(
            f"{self.name} is read-only; cannot {action}.",
            hint=(
                "Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID to enable writes "
                "(local reads keep working; the server switches to hybrid mode)."
            ),
        )

    def create_items(self, payloads: list[dict[str, Any]]) -> WriteOutcome:
        raise self._refuse("create items")

    def update_item(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        raise self._refuse("update items")

    def delete_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        raise self._refuse("delete items")

    def trash_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        raise self._refuse("trash items")

    def restore_items(self, versions_by_key: dict[str, int]) -> WriteOutcome:
        raise self._refuse("restore items")

    def empty_trash(self) -> WriteOutcome:
        raise self._refuse("empty the trash")

    def create_collection(self, name: str, *, parent_key: str | None = None) -> WriteOutcome:
        raise self._refuse("create collections")

    def update_collection(self, key: str, patch: dict[str, Any], *, version: int) -> WriteOutcome:
        raise self._refuse("update collections")

    def delete_collection(self, key: str, *, version: int) -> WriteOutcome:
        raise self._refuse("delete collections")

    def add_to_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        raise self._refuse("add items to collections")

    def remove_from_collection(self, collection_key: str, item_keys: list[str]) -> WriteOutcome:
        raise self._refuse("remove items from collections")

    def attach_file(
        self,
        parent_key: str,
        path: Path,
        *,
        title: str | None = None,
        link_mode: str = "imported_file",
    ) -> WriteOutcome:
        raise self._refuse("attach files")

    def attach_link(self, parent_key: str, url: str, *, title: str | None = None) -> WriteOutcome:
        raise self._refuse("attach links")


__all__ = [
    "ItemQuery",
    "LibraryBackend",
    "RawPage",
    "SortField",
    "WriteOutcome",
    "api_lock",
]
