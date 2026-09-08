"""Configuration precedence, migration and validation."""

import json

import pytest

from zotero_mcp.config import LibraryMode, LibraryType, load_config
from zotero_mcp.errors import InvalidInput


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every variable this package reads, so tests do not inherit them."""
    for name in list(__import__("os").environ):
        if name.startswith(("ZOTERO_", "ZOTERO_MCP_")):
            monkeypatch.delenv(name, raising=False)


def test_defaults_without_file_or_env(tmp_path, clean_env):
    config = load_config(tmp_path / "absent.json")
    assert config.library.mode is LibraryMode.AUTO
    assert config.limits.default_page_size == 20
    assert config.surface.compat is False
    assert config.source_path is None


def test_env_overrides_file(tmp_path, clean_env, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"limits": {"default_page_size": 5}}))
    monkeypatch.setenv("ZOTERO_MCP_PAGE_SIZE", "42")

    config = load_config(path)
    assert config.limits.default_page_size == 42
    assert config.source_path == str(path)


def test_explicit_overrides_beat_env(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_PAGE_SIZE", "42")
    config = load_config(tmp_path / "absent.json", overrides={"limits": {"default_page_size": 9}})
    assert config.limits.default_page_size == 9


def test_use_env_false_isolates_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_PAGE_SIZE", "42")
    config = load_config(tmp_path / "absent.json", use_env=False)
    assert config.limits.default_page_size == 20


def test_zotero_local_with_api_key_resolves_to_hybrid(tmp_path, clean_env, monkeypatch):
    """Local reads plus web writes is what a local user with a key actually wants."""
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    assert load_config(tmp_path / "absent.json").library.mode is LibraryMode.HYBRID


def test_zotero_local_without_api_key_resolves_to_local(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_LOCAL", "yes")
    assert load_config(tmp_path / "absent.json").library.mode is LibraryMode.LOCAL


def test_explicit_mode_beats_zotero_local(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    monkeypatch.setenv("ZOTERO_MCP_MODE", "web")
    assert load_config(tmp_path / "absent.json").library.mode is LibraryMode.WEB


def test_library_type_coerces_to_enum(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "group")
    assert load_config(tmp_path / "absent.json").library.library_type is LibraryType.GROUP


def test_legacy_semantic_search_section_is_migrated(tmp_path, clean_env):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "semantic_search": {
                    "embedding_model": "openai",
                    "zotero_db_path": "/data/zotero.sqlite",
                    "extraction": {"fulltext_display_max_pages": 12},
                }
            }
        )
    )
    config = load_config(path)
    assert config.semantic.embedding_provider == "openai"
    assert config.library.sqlite_path == "/data/zotero.sqlite"
    assert config.limits.max_pdf_pages == 12


def test_unreadable_file_falls_back_to_defaults(tmp_path, clean_env):
    """A broken config must not stop the server from starting."""
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    assert load_config(path).limits.default_page_size == 20


def test_non_object_config_is_ignored(tmp_path, clean_env):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]")
    assert load_config(path).limits.default_page_size == 20


def test_invalid_value_names_the_field(tmp_path, clean_env):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"limits": {"default_page_size": "lots"}}))
    with pytest.raises(InvalidInput) as excinfo:
        load_config(path)
    assert "limits.default_page_size" in str(excinfo.value)


def test_non_integer_env_is_ignored_not_fatal(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_PAGE_SIZE", "many")
    assert load_config(tmp_path / "absent.json").limits.default_page_size == 20


def test_with_library_returns_a_copy(tmp_path, clean_env):
    config = load_config(tmp_path / "absent.json")
    updated = config.with_library(library_id="999")
    assert updated.library.library_id == "999"
    assert config.library.library_id is None
