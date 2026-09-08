# Copyright 2026 Vinay

"""Reading ``zotero.sqlite`` directly, for what the APIs do not expose.

The fixture builds a database with the same shape as Zotero's own, because
the point of these tests is that the queries survive contact with that
schema: field ids resolved by name, feeds joined to their items, and a
locked database read anyway.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from zotero_mcp.backends import localdb
from zotero_mcp.config import ZoteroConfig
from zotero_mcp.errors import NotFound, Unsupported

#: Field ids Zotero itself does not guarantee. The fork assumed the title was
#: always 1; here it deliberately is not, so a hardcoded id would read the
#: wrong column.
FIELDS = {"url": 1, "abstractNote": 13, "title": 110}


def build_database(path, *, with_feeds=True):
    """A miniature zotero.sqlite: enough tables for the queries under test."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE version (schema TEXT PRIMARY KEY, version INT NOT NULL);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT UNIQUE);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INT, libraryID INT,
            key TEXT, dateAdded TEXT
        );
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemCreators (itemID INT, creatorID INT, orderIndex INT);
        """
    )
    connection.execute("INSERT INTO version VALUES ('userdata', 120)")
    for name, field_id in FIELDS.items():
        connection.execute("INSERT INTO fields VALUES (?, ?)", (field_id, name))
    connection.execute("INSERT INTO itemTypes VALUES (2, 'journalArticle')")

    if with_feeds:
        connection.executescript(
            """
            CREATE TABLE feeds (
                libraryID INTEGER PRIMARY KEY, name TEXT, url TEXT, lastUpdate TEXT,
                lastCheck TEXT, lastCheckError TEXT, cleanupReadAfter INT,
                cleanupUnreadAfter INT, refreshInterval INT
            );
            CREATE TABLE feedItems (
                itemID INTEGER PRIMARY KEY, guid TEXT, readTime TEXT, translatedTime TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO feeds VALUES (7, 'Nature', 'https://nature.com/rss', "
            "'2026-01-02', '2026-01-03', NULL, 3, 30, 60)"
        )
        connection.execute(
            "INSERT INTO feeds VALUES (8, 'Broken', 'https://gone.example/rss', "
            "NULL, '2026-01-03', 'HTTP 404', 3, 30, 60)"
        )
        rows = [
            (1, 7, "FEEDAAAA", "2026-01-03 10:00:00", "Attention is all you need", None),
            (2, 7, "FEEDBBBB", "2026-01-04 10:00:00", "A later paper", "2026-01-04 11:00:00"),
        ]
        for item_id, library_id, key, added, title, read in rows:
            connection.execute(
                "INSERT INTO items VALUES (?, 2, ?, ?, ?)", (item_id, library_id, key, added)
            )
            connection.execute("INSERT INTO itemDataValues VALUES (?, ?)", (item_id, title))
            connection.execute(
                "INSERT INTO itemData VALUES (?, ?, ?)", (item_id, FIELDS["title"], item_id)
            )
            connection.execute("INSERT INTO feedItems VALUES (?, ?, ?, NULL)", (item_id, key, read))
        connection.execute("INSERT INTO itemDataValues VALUES (90, 'On attention.')")
        connection.execute("INSERT INTO itemData VALUES (1, ?, 90)", (FIELDS["abstractNote"],))
        connection.execute("INSERT INTO itemDataValues VALUES (91, 'https://example.org/a')")
        connection.execute("INSERT INTO itemData VALUES (1, ?, 91)", (FIELDS["url"],))
        connection.execute("INSERT INTO creators VALUES (1, 'Ashish', 'Vaswani')")
        connection.execute("INSERT INTO itemCreators VALUES (1, 1, 0)")

    connection.commit()
    connection.close()
    return path


@pytest.fixture
def database(tmp_path):
    """A configuration pointing at a miniature Zotero database."""
    path = build_database(tmp_path / "zotero.sqlite")
    config = ZoteroConfig()
    return replace(config, library=replace(config.library, sqlite_path=str(path)))


def test_the_data_directory_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOTERO_DATA_DIR", str(tmp_path / "elsewhere"))
    assert localdb.zotero_data_dir() == tmp_path / "elsewhere"


def test_the_data_directory_defaults_to_the_home_folder(monkeypatch):
    monkeypatch.delenv("ZOTERO_DATA_DIR", raising=False)
    assert localdb.zotero_data_dir().name == "Zotero"


def test_a_configured_path_wins_over_the_default(database):
    assert localdb.sqlite_path(database).name == "zotero.sqlite"


def test_a_missing_database_says_where_it_looked(tmp_path):
    config = ZoteroConfig()
    config = replace(
        config, library=replace(config.library, sqlite_path=str(tmp_path / "nothing.sqlite"))
    )
    with pytest.raises(NotFound) as excinfo, localdb.open_database(config):
        pass
    assert "nothing.sqlite" in str(excinfo.value)


def test_feeds_come_back_with_their_item_counts(database):
    feeds = localdb.get_feeds(database)
    assert [feed["name"] for feed in feeds] == ["Broken", "Nature"]
    nature = next(feed for feed in feeds if feed["name"] == "Nature")
    assert nature["libraryID"] == 7
    assert nature["itemCount"] == 2
    assert nature["lastCheckError"] is None


def test_a_feed_that_failed_keeps_its_error(database):
    broken = next(feed for feed in localdb.get_feeds(database) if feed["name"] == "Broken")
    assert broken["lastCheckError"] == "HTTP 404"
    assert broken["itemCount"] == 0


def test_feed_items_are_newest_first_and_carry_their_metadata(database):
    items = localdb.get_feed_items(database, 7)
    assert [item["key"] for item in items] == ["FEEDBBBB", "FEEDAAAA"]
    first = items[-1]
    assert first["title"] == "Attention is all you need"
    assert first["abstract"] == "On attention."
    assert first["url"] == "https://example.org/a"
    assert first["creators"] == "Vaswani, Ashish"
    assert first["readTime"] is None


def test_feed_items_respect_the_limit(database):
    assert len(localdb.get_feed_items(database, 7, limit=1)) == 1


def test_another_feed_does_not_see_these_items(database):
    assert localdb.get_feed_items(database, 8) == []


def test_the_title_is_found_by_name_not_by_a_hardcoded_id(database):
    """The fork read fieldID 1. Here that id is the url, so it would misread."""
    with localdb.open_database(database) as connection:
        assert localdb._field_id(connection, "title") == FIELDS["title"]
        assert localdb._field_id(connection, "nosuchfield") is None
    assert localdb.get_feed_items(database, 7)[-1]["title"] != "https://example.org/a"


def test_a_zotero_without_feeds_reports_none_rather_than_failing(tmp_path):
    path = build_database(tmp_path / "zotero.sqlite", with_feeds=False)
    config = ZoteroConfig()
    config = replace(config, library=replace(config.library, sqlite_path=str(path)))
    assert localdb.get_feeds(config) == []
    assert localdb.get_feed_items(config, 7) == []


def test_a_locked_database_is_read_from_a_snapshot(database, monkeypatch):
    """Zotero holds a lock while it runs; waiting on it is never the answer."""
    real_connect = sqlite3.connect
    attempts = []

    def connect(target, **kwargs):
        attempts.append(target)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(target, **kwargs)

    monkeypatch.setattr(localdb.sqlite3, "connect", connect)
    feeds = localdb.get_feeds(database)
    assert [feed["name"] for feed in feeds] == ["Broken", "Nature"]
    assert len(attempts) == 2
    assert "zotero_mcp_db_" in attempts[1]


def test_a_snapshot_that_cannot_be_taken_is_reported_honestly(database, monkeypatch):
    def refuse(target, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(localdb.sqlite3, "connect", refuse)
    monkeypatch.setattr(
        localdb.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("no space"))
    )
    with pytest.raises(Unsupported) as excinfo:
        localdb.get_feeds(database)
    assert "no space" in str(excinfo.value)


def test_the_snapshot_is_cleaned_up_afterwards(database, monkeypatch):
    real_connect = sqlite3.connect
    taken = []

    def connect(target, **kwargs):
        if not taken:
            taken.append(target)
            raise sqlite3.OperationalError("database is locked")
        taken.append(target)
        return real_connect(target, **kwargs)

    monkeypatch.setattr(localdb.sqlite3, "connect", connect)
    localdb.get_feeds(database)
    snapshot = taken[1].removeprefix("file:").split("?")[0]
    assert not localdb.Path(snapshot).exists()
