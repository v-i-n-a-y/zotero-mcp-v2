# Copyright 2026 Vinay

"""Zotero's own item-type schema: valid fields, and base-field resolution.

Zotero stores several conceptually identical fields under type-specific keys.
A case's title is ``caseName``, a statute's is ``nameOfAct``, an email's is
``subject``; a case's date is ``dateDecided``, a statute's ``dateEnacted``. The
Web API writes the *actual* key, so a caller that says ``title=`` on a case has
to have it routed to ``caseName``, and a field's validity has to be judged
against the type's declared field set rather than against whichever keys happen
to be present on a fetched item.

The predecessors hand-maintained partial type maps in three separate modules,
which is why they mishandle legal, audiovisual and dataset item types. The
mapping is *data*, published by Zotero at ``/schema`` and used by the desktop
client itself, so this module carries it as data:

* a **vendored** copy (``data/zotero_schema.json``) ships with the package and
  is the floor. The server is always correct offline, which matters because
  local mode exists precisely for people who are not online;
* a **runtime refresh** (:func:`refresh`) does a TTL-gated conditional GET and
  caches the result under the user's cache directory, so item types added
  between releases are picked up without waiting for one.

Zotero has no user-defined item types, so the global schema is authoritative
for every library, personal or group.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_URL = "https://api.zotero.org/schema"
VENDORED_PATH = Path(__file__).parent / "data" / "zotero_schema.json"

#: Zotero's schema changes rarely and field renames are structurally frozen,
#: so a weekly conditional GET is generous.
REFRESH_TTL_SECONDS = 7 * 24 * 3600

#: After a failed refresh, wait a day before trying again. Without this, a
#: machine with no route to api.zotero.org, the offline local-only case this
#: server explicitly supports, retries on every startup and warns every time.
FAILED_REFRESH_BACKOFF_SECONDS = 24 * 3600

#: Fields every item type carries regardless of what the schema says.
UNIVERSAL_FIELDS = frozenset({"itemType", "tags", "collections", "relations", "key", "version"})

#: Item types that are children of a regular item and are never created
#: directly through the item-writing tools.
CHILD_ITEM_TYPES = frozenset({"attachment", "note", "annotation"})

_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    if override := os.environ.get("ZOTERO_MCP_SCHEMA_CACHE"):
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "zotero-mcp" / "schema.json"


def _valid(table: Any) -> bool:
    """Reject anything that is not recognisably a schema table.

    A truncated download or a half-written cache file must fall back to the
    vendored copy rather than silently producing a schema with no item types,
    which would make every field look invalid.
    """
    return (
        isinstance(table, dict)
        and isinstance(table.get("itemTypes"), dict)
        and bool(table["itemTypes"])
    )


def _load_table() -> dict[str, Any]:
    """Return the newest usable table: refreshed cache if valid, else vendored."""
    global _cache
    if _cache is not None:
        return _cache

    vendored: dict[str, Any] = {"version": 0, "itemTypes": {}, "creatorTypes": {}}
    try:
        candidate = json.loads(VENDORED_PATH.read_text(encoding="utf-8"))
        if _valid(candidate):
            vendored = candidate
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - packaging error
        logger.error("Vendored Zotero schema is unreadable: %s", exc)

    chosen = vendored
    with suppress(OSError, json.JSONDecodeError):
        cached = json.loads(_cache_path().read_text(encoding="utf-8"))
        # Only prefer the cache when it is both valid and genuinely newer;
        # a stale cache from an older release must not shadow the package.
        if _valid(cached) and cached.get("version", 0) >= vendored.get("version", 0):
            chosen = cached

    _cache = chosen
    return chosen


def reset_cache() -> None:
    """Drop the in-process memo. For tests, and after a successful refresh."""
    global _cache
    _cache = None
    valid_fields.cache_clear()
    _base_to_actual.cache_clear()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def schema_version() -> int:
    """Version number of the schema table currently in use."""
    return int(_load_table().get("version", 0))


def item_types() -> list[str]:
    """Every item type Zotero knows, sorted."""
    return sorted(_load_table().get("itemTypes", {}))


def creatable_item_types() -> list[str]:
    """Item types a caller may create directly (excludes attachment/note/annotation)."""
    return [t for t in item_types() if t not in CHILD_ITEM_TYPES]


def is_item_type(item_type: str) -> bool:
    """True if *item_type* is a real Zotero item type."""
    return item_type in _load_table().get("itemTypes", {})


@lru_cache(maxsize=64)
def valid_fields(item_type: str) -> frozenset[str]:
    """The field names *item_type* actually accepts.

    Empty for an unknown type, which callers must treat as "cannot validate"
    rather than "nothing is valid". See :func:`unknown_fields`.
    """
    return frozenset(_load_table().get("itemTypes", {}).get(item_type, {}))


def base_field(item_type: str, field: str) -> str:
    """Map an actual field to its base field (``caseName`` -> ``title``).

    Returns *field* unchanged when it has no base field or the type is unknown,
    since most fields are their own base.
    """
    return _load_table().get("itemTypes", {}).get(item_type, {}).get(field, field)


@lru_cache(maxsize=64)
def _base_to_actual(item_type: str) -> dict[str, str]:
    """Invert one type's field map: base field -> actual field."""
    mapping = _load_table().get("itemTypes", {}).get(item_type, {})
    inverted: dict[str, str] = {}
    for actual, base in mapping.items():
        # A field that *is* its own base must not shadow a distinct actual
        # field that maps onto it, and vice versa; prefer the explicit
        # base->actual direction where the two differ.
        if base != actual:
            inverted.setdefault(base, actual)
    return inverted


def resolve_field(item_type: str, field: str) -> str | None:
    """Return the actual field name to write for *field* on *item_type*.

    ``resolve_field("case", "title")`` is ``"caseName"``;
    ``resolve_field("case", "caseName")`` is ``"caseName"``;
    ``resolve_field("case", "publisher")`` is ``None``, since cases have no publisher.

    ``None`` for an unknown item type as well, so callers get one
    "cannot place this field" answer rather than two.
    """
    fields = valid_fields(item_type)
    if not fields:
        return None
    if field in fields:
        return field
    return _base_to_actual(item_type).get(field)


def resolve_fields(item_type: str, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Route a caller's generic field names onto *item_type*'s actual fields.

    Returns ``(resolved, unplaceable)``. Keys in ``UNIVERSAL_FIELDS`` pass
    through untouched. When the item type is unknown to the schema, everything
    passes through unchanged and ``unplaceable`` is empty: an out-of-date
    schema must not block a write that Zotero itself would accept.
    """
    if not valid_fields(item_type):
        return dict(values), []

    resolved: dict[str, Any] = {}
    unplaceable: list[str] = []
    for name, value in values.items():
        if name in UNIVERSAL_FIELDS:
            resolved[name] = value
            continue
        actual = resolve_field(item_type, name)
        if actual is None:
            unplaceable.append(name)
        else:
            resolved[actual] = value
    return resolved, unplaceable


def unknown_fields(item_type: str, names: list[str]) -> list[str]:
    """Which of *names* this item type cannot hold, under any name."""
    if not valid_fields(item_type):
        return []
    return [n for n in names if n not in UNIVERSAL_FIELDS and resolve_field(item_type, n) is None]


def creator_types(item_type: str) -> list[str]:
    """Creator types valid for *item_type*, or ``[]`` if not known locally.

    The vendored floor does not carry creator types, so an empty list means
    "cannot validate" and callers must accept whatever they were given and let
    Zotero adjudicate. A live :func:`refresh` fills this in.
    """
    return list(_load_table().get("creatorTypes", {}).get(item_type, []))


def suggest_field(item_type: str, field: str, limit: int = 3) -> list[str]:
    """Closest valid field names to *field*, for error hints.

    Uses stdlib fuzzy matching so a rejected write can say "did you mean
    ``dateDecided``?" instead of dumping all forty of a type's fields.
    """
    import difflib

    candidates = set(valid_fields(item_type)) | set(_base_to_actual(item_type))
    return difflib.get_close_matches(field, sorted(candidates), n=limit, cutoff=0.6)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _should_attempt(meta: dict[str, Any], now: float) -> bool:
    last_success = meta.get("_fetched_at", 0)
    last_failure = meta.get("_failed_at", 0)
    if now - last_failure < FAILED_REFRESH_BACKOFF_SECONDS:
        return False
    return now - last_success >= REFRESH_TTL_SECONDS


def refresh(*, force: bool = False, timeout: float = 10.0) -> bool:
    """Fetch a newer schema from Zotero if the TTL has expired.

    Returns True when the on-disk cache was updated. Never raises: a schema
    refresh is an optimisation, and failing it must not stop a server whose
    vendored floor is already correct.
    """
    path = _cache_path()
    now = time.time()

    meta: dict[str, Any] = {}
    with suppress(OSError, json.JSONDecodeError):
        meta = json.loads(path.read_text(encoding="utf-8"))

    if not force and not _should_attempt(meta, now):
        return False

    try:
        import requests

        headers = {"User-Agent": "zotero-mcp-next"}
        # Zotero serves the schema with an ETag; a conditional GET usually
        # costs a 304 and no body at all.
        if etag := meta.get("_etag"):
            headers["If-None-Match"] = etag

        response = requests.get(SCHEMA_URL, headers=headers, timeout=timeout)
        if response.status_code == 304:
            meta["_fetched_at"] = now
            _write_cache(path, meta)
            return False
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 (refresh is best-effort by design)
        logger.debug("Zotero schema refresh failed: %s", exc)
        meta["_failed_at"] = now
        _write_cache(path, meta)
        return False

    table = _trim(payload)
    if not _valid(table):
        logger.warning("Zotero schema response was not usable; keeping current table")
        meta["_failed_at"] = now
        _write_cache(path, meta)
        return False

    table["_fetched_at"] = now
    if etag := response.headers.get("ETag"):
        table["_etag"] = etag
    _write_cache(path, table)
    reset_cache()
    logger.info("Zotero schema refreshed to version %s", table.get("version"))
    return True


def _trim(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce Zotero's full schema document to the slice this package needs.

    The published document is well over a megabyte, almost all of it
    localisation and CSL mappings. Keeping only field and creator maps means
    the cache stays small enough to read on every cold start.
    """
    item_type_map: dict[str, dict[str, str]] = {}
    creator_map: dict[str, list[str]] = {}

    for entry in payload.get("itemTypes", []):
        name = entry.get("itemType")
        if not name:
            continue
        fields: dict[str, str] = {}
        for field in entry.get("fields", []):
            actual = field.get("field")
            if actual:
                fields[actual] = field.get("baseField", actual)
        item_type_map[name] = fields
        creators = [
            c.get("creatorType") for c in entry.get("creatorTypes", []) if c.get("creatorType")
        ]
        if creators:
            creator_map[name] = creators

    return {
        "version": payload.get("version", 0),
        "itemTypes": item_type_map,
        "creatorTypes": creator_map,
    }


def _write_cache(path: Path, table: dict[str, Any]) -> None:
    """Write the cache atomically, so a crash mid-write cannot corrupt it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(table), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - unwritable cache dir
        logger.debug("Could not write schema cache to %s: %s", path, exc)


__all__ = [
    "CHILD_ITEM_TYPES",
    "UNIVERSAL_FIELDS",
    "base_field",
    "creatable_item_types",
    "creator_types",
    "is_item_type",
    "item_types",
    "refresh",
    "reset_cache",
    "resolve_field",
    "resolve_fields",
    "schema_version",
    "suggest_field",
    "unknown_fields",
    "valid_fields",
]
