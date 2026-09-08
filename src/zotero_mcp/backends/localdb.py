# Copyright 2026 Vinay

"""Read-only access to ``zotero.sqlite``, for what the APIs do not expose.

Zotero's Web API and its local HTTP API both describe *libraries*. A handful
of things live outside that model, RSS feeds most obviously, and the only
place they exist is the desktop client's own database. This module reads that
database directly, and does nothing else: it is a narrow escape hatch, not a
third backend.

Three rules make reading a live database safe here:

* the connection is opened read-only through a URI, so nothing this process
  does can corrupt a library;
* a locked database (Zotero holds one while it runs) is read from a snapshot
  copy rather than waited on, because blocking a tool call on the user's own
  application is never the right trade;
* every query resolves field ids by name. The fork hardcoded ``fieldID = 1``
  for the title, which is true of most installs and silently wrong on the
  rest.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zotero_mcp.config import ZoteroConfig
from zotero_mcp.errors import NotFound, Unsupported

logger = logging.getLogger(__name__)


def zotero_data_dir() -> Path:
    """The desktop client's data directory, by its documented default."""
    if configured := os.environ.get("ZOTERO_DATA_DIR"):
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / "Zotero"
    return Path.home() / "Zotero"


def sqlite_path(config: ZoteroConfig) -> Path:
    """Where ``zotero.sqlite`` is, configured or derived."""
    if configured := config.library.sqlite_path:
        return Path(configured).expanduser()
    return zotero_data_dir() / "zotero.sqlite"


@contextmanager
def open_database(config: ZoteroConfig) -> Iterator[sqlite3.Connection]:
    """A read-only connection to the Zotero database.

    Raises:
        NotFound: There is no database at the resolved path.
        Unsupported: The database exists but could not be read at all.
    """
    path = sqlite_path(config)
    if not path.exists():
        raise NotFound(
            f"No Zotero database at {path}.",
            hint=(
                "This needs the Zotero desktop application installed on this machine. "
                "Set ZOTERO_DATA_DIR if it keeps its data somewhere else."
            ),
        )

    snapshot: Path | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=0", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            # Force a read now, so a lock surfaces here rather than mid-query.
            connection.execute("SELECT 1 FROM version LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            connection.close()
            raise
    except sqlite3.OperationalError as exc:
        logger.debug("Zotero database is locked (%s); reading a snapshot instead", exc)
        try:
            directory = Path(tempfile.mkdtemp(prefix="zotero_mcp_db_"))
            snapshot = directory / "zotero.sqlite"
            shutil.copy2(path, snapshot)
            connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except Exception as copy_error:
            raise Unsupported(
                f"Could not read {path}: {copy_error}",
                hint="Close Zotero and try again, or check the file's permissions.",
            ) from copy_error

    try:
        yield connection
    finally:
        connection.close()
        if snapshot is not None:
            shutil.rmtree(snapshot.parent, ignore_errors=True)


def _field_id(connection: sqlite3.Connection, name: str) -> int | None:
    row = connection.execute("SELECT fieldID FROM fields WHERE fieldName = ?", (name,)).fetchone()
    return int(row["fieldID"]) if row else None


def get_feeds(config: ZoteroConfig) -> list[dict[str, Any]]:
    """Every RSS subscription, with its item count."""
    with open_database(config) as connection:
        try:
            rows = connection.execute(
                """
                SELECT f.libraryID, f.name, f.url, f.lastCheck, f.lastUpdate,
                       f.lastCheckError, f.refreshInterval,
                       (SELECT COUNT(*) FROM feedItems fi
                        JOIN items i ON fi.itemID = i.itemID
                        WHERE i.libraryID = f.libraryID) AS itemCount
                FROM feeds f
                ORDER BY f.name
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # A Zotero old enough to have no feeds table has no feeds either.
            logger.debug("No feeds table in this Zotero database: %s", exc)
            return []
    return [dict(row) for row in rows]


def get_feed_items(
    config: ZoteroConfig, library_id: int, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Items belonging to one feed, newest first."""
    with open_database(config) as connection:
        title_field = _field_id(connection, "title")
        abstract_field = _field_id(connection, "abstractNote")
        url_field = _field_id(connection, "url")
        try:
            rows = connection.execute(
                """
                SELECT i.key, it.typeName AS itemType, i.dateAdded,
                       fi.readTime, fi.translatedTime,
                       title_val.value AS title,
                       abstract_val.value AS abstract,
                       url_val.value AS url,
                       GROUP_CONCAT(
                           CASE
                               WHEN c.firstName IS NOT NULL AND c.lastName IS NOT NULL
                               THEN c.lastName || ', ' || c.firstName
                               ELSE c.lastName
                           END, '; '
                       ) AS creators
                FROM feedItems fi
                JOIN items i ON fi.itemID = i.itemID
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                LEFT JOIN itemData title_data
                       ON i.itemID = title_data.itemID AND title_data.fieldID = ?
                LEFT JOIN itemDataValues title_val ON title_data.valueID = title_val.valueID
                LEFT JOIN itemData abstract_data
                       ON i.itemID = abstract_data.itemID AND abstract_data.fieldID = ?
                LEFT JOIN itemDataValues abstract_val ON abstract_data.valueID = abstract_val.valueID
                LEFT JOIN itemData url_data
                       ON i.itemID = url_data.itemID AND url_data.fieldID = ?
                LEFT JOIN itemDataValues url_val ON url_data.valueID = url_val.valueID
                LEFT JOIN itemCreators ic ON i.itemID = ic.itemID
                LEFT JOIN creators c ON ic.creatorID = c.creatorID
                WHERE i.libraryID = ?
                GROUP BY i.itemID
                ORDER BY i.dateAdded DESC
                LIMIT ?
                """,
                (title_field, abstract_field, url_field, library_id, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("No feeds table in this Zotero database: %s", exc)
            return []
    return [dict(row) for row in rows]


__all__ = ["get_feed_items", "get_feeds", "open_database", "sqlite_path", "zotero_data_dir"]
