# Copyright 2026 Vinay

"""The command line.

``doctor`` gets the most attention here, because it is the command a user
reaches for when nothing works, and a diagnostic that fails to run is worse
than none at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import FakeBackend
from zotero_mcp import cli
from zotero_mcp.errors import BackendUnavailable


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate the CLI from the machine's own Zotero configuration."""
    for name in (
        "ZOTERO_LOCAL",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_API_KEY",
        "ZOTERO_MCP_MODE",
        "ZOTERO_MCP_TOOLSETS",
        "ZOTERO_MCP_CONTACT_EMAIL",
        "ZOTERO_MCP_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def reachable(monkeypatch):
    """A backend that answers, without touching a real Zotero."""
    backend = FakeBackend()
    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", lambda config: backend)
    return backend


# -- argument handling ------------------------------------------------------


def test_the_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "zotero-mcp" in capsys.readouterr().out


def test_an_unknown_command_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


def test_a_failure_is_reported_as_a_message_not_a_traceback(monkeypatch, capsys, clean_env):
    def explode(config):
        raise BackendUnavailable("Zotero is not running.", hint="Start Zotero.")

    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", explode)
    monkeypatch.setattr("zotero_mcp.index.store.default_db_path", lambda config: Path("/nope"))
    assert cli.main(["index", "status"]) == 1
    err = capsys.readouterr().err
    assert "error: Zotero is not running." in err
    assert "hint: Start Zotero." in err


def test_an_interrupt_uses_the_conventional_exit_code(monkeypatch, clean_env):
    def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_serve", interrupt)
    assert cli.main(["serve"]) == 130


# -- doctor -----------------------------------------------------------------


def test_doctor_reports_a_reachable_library(capsys, clean_env, reachable, monkeypatch):
    monkeypatch.setattr("zotero_mcp.index.query.status", lambda: {"state": "unavailable"})
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "reachable       yes" in out
    assert "Everything looks reachable." in out


def test_doctor_reports_an_unreachable_library_as_a_problem(capsys, clean_env, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.backends.factory.build_backend", lambda config: FakeBackend(reachable=False)
    )
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "reachable       NO" in out
    assert "did not answer" in out


def test_doctor_reports_a_backend_that_could_not_be_built(capsys, clean_env, monkeypatch):
    def explode(config):
        raise BackendUnavailable("No credentials.", hint="Set ZOTERO_API_KEY.")

    monkeypatch.setattr("zotero_mcp.backends.factory.build_backend", explode)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "No credentials." in out
    assert "Set ZOTERO_API_KEY." in out


def test_doctor_can_answer_in_json(capsys, clean_env, reachable, monkeypatch):
    monkeypatch.setattr("zotero_mcp.index.query.status", lambda: {"state": "unavailable"})
    assert cli.main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reachable"] is True
    assert report["item_count"] == 4
    assert report["schema_version"]


def test_doctor_json_exits_nonzero_when_zotero_is_unreachable(capsys, clean_env, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.backends.factory.build_backend", lambda config: FakeBackend(reachable=False)
    )
    assert cli.main(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["reachable"] is False


# -- setup ------------------------------------------------------------------


def test_setup_prints_a_config_block_by_default(capsys, clean_env):
    assert cli.main(["setup"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mcpServers"]["zotero"]["args"] == ["serve"]


def test_setup_carries_the_environment_the_server_needs(capsys, clean_env, monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "12345")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    cli.main(["setup"])
    entry = json.loads(capsys.readouterr().out)["mcpServers"]["zotero"]
    assert entry["env"] == {"ZOTERO_LIBRARY_ID": "12345", "ZOTERO_API_KEY": "secret"}


def test_setup_writes_into_a_named_config_file(tmp_path, capsys, clean_env):
    path = tmp_path / "nested" / "client.json"
    assert cli.main(["setup", "--client", "claude-code", "--config-path", str(path)]) == 0
    written = json.loads(path.read_text())
    assert "zotero" in written["mcpServers"]
    assert "Added the 'zotero' server" in capsys.readouterr().out


def test_setup_preserves_other_servers_already_configured(tmp_path, capsys, clean_env):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": True}))
    cli.main(["setup", "--client", "claude-code", "--config-path", str(path)])
    written = json.loads(path.read_text())
    assert "other" in written["mcpServers"]
    assert written["keep"] is True


def test_setup_says_when_it_replaced_an_existing_entry(tmp_path, capsys, clean_env):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"mcpServers": {"zotero": {"command": "old"}}}))
    cli.main(["setup", "--client", "claude-code", "--config-path", str(path)])
    assert "Updated the 'zotero' server" in capsys.readouterr().out


def test_setup_refuses_to_overwrite_a_file_it_cannot_parse(tmp_path, capsys, clean_env):
    path = tmp_path / "client.json"
    path.write_text("{ not json")
    assert cli.main(["setup", "--client", "claude-code", "--config-path", str(path)]) == 1
    assert path.read_text() == "{ not json"
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "claude_desktop_config.json"),
        ("win32", "claude_desktop_config.json"),
        ("linux", "claude_desktop_config.json"),
    ],
)
def test_the_desktop_config_path_is_platform_specific(monkeypatch, platform, expected, clean_env):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("APPDATA", str(clean_env / "roaming"))
    assert cli._claude_desktop_config_path().name == expected


def test_setup_defaults_to_the_desktop_config_location(monkeypatch, tmp_path, clean_env, capsys):
    target = tmp_path / "desktop.json"
    monkeypatch.setattr(cli, "_claude_desktop_config_path", lambda: target)
    cli.main(["setup", "--client", "claude-desktop"])
    assert "zotero" in json.loads(target.read_text())["mcpServers"]


# -- index ------------------------------------------------------------------


def test_index_status_prints_the_state(capsys, clean_env, reachable, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.query.status",
        lambda: {"state": "ready", "detail": "here", "count": 12},
    )
    assert cli.main(["index", "status"]) == 0
    out = capsys.readouterr().out
    assert "state  ready" in out
    assert "items  12" in out


def test_clearing_an_index_that_is_not_there_says_so(capsys, clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "zotero_mcp.index.store.default_db_path", lambda config: tmp_path / "absent"
    )
    assert cli.main(["index", "clear"]) == 0
    assert "No index at" in capsys.readouterr().out


def test_clearing_removes_the_index_directory(capsys, clean_env, monkeypatch, tmp_path):
    path = tmp_path / "index"
    path.mkdir()
    (path / "chroma.sqlite3").write_text("data")
    monkeypatch.setattr("zotero_mcp.index.store.default_db_path", lambda config: path)
    assert cli.main(["index", "clear"]) == 0
    assert not path.exists()
    assert "Removed the index" in capsys.readouterr().out


def test_a_build_reports_what_it_indexed(capsys, clean_env, reachable, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.builder.update_index",
        lambda runtime, **kwargs: {"indexed": 5, "chunks": 9, "skipped": 2, "errors": []},
    )
    assert cli.main(["index", "build"]) == 0
    assert "indexed 5 items in 9 chunks" in capsys.readouterr().out


def test_a_build_with_failed_batches_exits_nonzero(capsys, clean_env, reachable, monkeypatch):
    monkeypatch.setattr(
        "zotero_mcp.index.builder.update_index",
        lambda runtime, **kwargs: {"indexed": 1, "chunks": 1, "skipped": 0, "errors": ["nope"]},
    )
    assert cli.main(["index", "update"]) == 1
    assert "rerun to resume" in capsys.readouterr().err


def test_the_build_passes_its_flags_through(clean_env, reachable, monkeypatch):
    seen: dict = {}

    def record(runtime, **kwargs):
        seen.update(kwargs)
        return {"indexed": 0, "chunks": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr("zotero_mcp.index.builder.update_index", record)
    cli.main(["index", "build", "--limit", "10", "--no-fulltext"])
    assert seen["limit"] == 10
    assert seen["rebuild"] is True
    assert seen["include_fulltext"] is False


# -- serve and tools --------------------------------------------------------


def test_serve_runs_over_stdio_by_default(monkeypatch, clean_env):
    calls: list = []
    monkeypatch.setattr(cli, "_prepare", lambda *a, **k: (FakeRunner(calls), None))
    assert cli.main(["serve"]) == 0
    assert calls == [((), {})]


def test_serve_over_http_passes_the_host_and_port(monkeypatch, clean_env):
    calls: list = []
    monkeypatch.setattr(cli, "_prepare", lambda *a, **k: (FakeRunner(calls), None))
    assert cli.main(["serve", "--transport", "http", "--port", "9999"]) == 0
    assert calls[0][1] == {"transport": "http", "host": "127.0.0.1", "port": 9999}


class FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_tools_lists_what_the_configuration_exposes(capsys, clean_env, monkeypatch):
    """Gating is stubbed out: it mutates the process-wide registry."""
    from zotero_mcp.config import ZoteroConfig

    class Registry:
        @staticmethod
        async def list_tools():
            return [type("T", (), {"name": "zotero_search"})()]

    monkeypatch.setattr(cli, "_prepare", lambda *a, **k: (Registry(), ZoteroConfig()))
    assert cli._tools() == 0
    out = capsys.readouterr().out
    assert "1 tools exposed" in out
    assert "zotero_search" in out


def test_preparing_the_server_applies_the_toolset_selection(monkeypatch, clean_env):
    seen: dict = {}

    def record(server, *, selection, compat):
        seen.update({"selection": selection, "compat": compat})

    monkeypatch.setattr("zotero_mcp.toolsets.apply", record)
    cli._prepare("scite", False)
    assert seen == {"selection": "scite", "compat": False}


def test_the_compat_flag_reaches_the_toolset_layer(monkeypatch, clean_env):
    seen: dict = {}
    monkeypatch.setattr(
        "zotero_mcp.toolsets.apply",
        lambda server, *, selection, compat: seen.update({"compat": compat}),
    )
    cli._prepare(None, True)
    assert seen["compat"] is True
