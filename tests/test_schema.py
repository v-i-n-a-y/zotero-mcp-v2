# Copyright 2026 Vinay

"""Zotero base-field resolution against the vendored schema."""

import json

import pytest

from zotero_mcp import schema


@pytest.fixture(autouse=True)
def _fresh_schema_cache(monkeypatch, tmp_path):
    """Isolate from any real refreshed cache in the user's home directory."""
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(tmp_path / "schema.json"))
    schema.reset_cache()
    yield
    schema.reset_cache()


def test_vendored_schema_is_present_and_substantial():
    assert schema.schema_version() > 0
    assert len(schema.item_types()) >= 30
    assert "journalArticle" in schema.item_types()


@pytest.mark.parametrize(
    ("item_type", "generic", "actual"),
    [
        ("case", "title", "caseName"),
        ("case", "date", "dateDecided"),
        ("statute", "title", "nameOfAct"),
        ("email", "title", "subject"),
        ("statute", "date", "dateEnacted"),
        ("journalArticle", "title", "title"),
        ("journalArticle", "publicationTitle", "publicationTitle"),
    ],
)
def test_resolve_field_routes_generic_names(item_type, generic, actual):
    assert schema.resolve_field(item_type, generic) == actual


def test_resolve_field_is_idempotent_on_actual_names():
    assert schema.resolve_field("case", "caseName") == "caseName"


def test_resolve_field_rejects_fields_a_type_cannot_hold():
    assert schema.resolve_field("case", "publisher") is None


def test_resolve_field_on_unknown_type_returns_none():
    assert schema.resolve_field("notARealType", "title") is None


def test_base_field_maps_the_other_direction():
    assert schema.base_field("case", "caseName") == "title"
    assert schema.base_field("case", "dateDecided") == "date"
    # Most fields are their own base.
    assert schema.base_field("journalArticle", "volume") == "volume"


def test_resolve_fields_splits_placeable_from_unplaceable():
    resolved, unplaceable = schema.resolve_fields(
        "case", {"title": "Roe v. Wade", "date": "1973", "publisher": "nope", "tags": []}
    )
    assert resolved == {"caseName": "Roe v. Wade", "dateDecided": "1973", "tags": []}
    assert unplaceable == ["publisher"]


def test_resolve_fields_passes_everything_through_for_unknown_types():
    """An out-of-date schema must not block a write Zotero would accept."""
    values = {"title": "x", "somethingNew": "y"}
    resolved, unplaceable = schema.resolve_fields("futureType", values)
    assert resolved == values
    assert unplaceable == []


def test_universal_fields_are_never_rejected():
    assert schema.unknown_fields("case", ["tags", "collections", "relations"]) == []


def test_child_item_types_are_not_creatable():
    creatable = schema.creatable_item_types()
    assert "journalArticle" in creatable
    for child in ("attachment", "note", "annotation"):
        assert child not in creatable


def test_suggest_field_helps_with_a_typo():
    assert "dateDecided" in schema.suggest_field("case", "dateDecide")


def test_a_newer_cache_overrides_the_vendored_table(tmp_path, monkeypatch):
    cache = tmp_path / "schema.json"
    cache.write_text(
        json.dumps(
            {
                "version": 99_999,
                "itemTypes": {"journalArticle": {"title": "title", "novelField": "novelField"}},
                "creatorTypes": {"journalArticle": ["author", "reviewedAuthor"]},
            }
        )
    )
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(cache))
    schema.reset_cache()

    assert schema.schema_version() == 99_999
    assert schema.resolve_field("journalArticle", "novelField") == "novelField"
    assert schema.creator_types("journalArticle") == ["author", "reviewedAuthor"]


def test_an_older_cache_does_not_shadow_the_package(tmp_path, monkeypatch):
    cache = tmp_path / "schema.json"
    cache.write_text(json.dumps({"version": 1, "itemTypes": {"onlyType": {}}}))
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(cache))
    schema.reset_cache()

    assert schema.schema_version() >= 42
    assert "journalArticle" in schema.item_types()


def test_a_corrupt_cache_falls_back_to_the_vendored_table(tmp_path, monkeypatch):
    cache = tmp_path / "schema.json"
    cache.write_text("{ truncated")
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(cache))
    schema.reset_cache()

    assert "journalArticle" in schema.item_types()


def test_an_empty_cache_is_not_treated_as_a_valid_schema(tmp_path, monkeypatch):
    """A schema with no item types would make every field look invalid."""
    cache = tmp_path / "schema.json"
    cache.write_text(json.dumps({"version": 99_999, "itemTypes": {}}))
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(cache))
    schema.reset_cache()

    assert "journalArticle" in schema.item_types()


def test_trim_reduces_a_full_schema_document():
    trimmed = schema._trim(
        {
            "version": 7,
            "itemTypes": [
                {
                    "itemType": "case",
                    "fields": [
                        {"field": "caseName", "baseField": "title"},
                        {"field": "court"},
                    ],
                    "creatorTypes": [{"creatorType": "author"}],
                }
            ],
        }
    )
    assert trimmed["version"] == 7
    assert trimmed["itemTypes"]["case"] == {"caseName": "title", "court": "court"}
    assert trimmed["creatorTypes"]["case"] == ["author"]


def test_refresh_backs_off_after_a_failure(tmp_path, monkeypatch):
    """An offline machine must not retry, and warn, on every single startup."""
    import time

    cache = tmp_path / "schema.json"
    cache.write_text(json.dumps({"_failed_at": time.time(), "version": 1, "itemTypes": {"a": {}}}))
    monkeypatch.setenv("ZOTERO_MCP_SCHEMA_CACHE", str(cache))

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("refresh attempted during back-off window")

    monkeypatch.setattr("requests.get", explode)
    assert schema.refresh() is False
