# Copyright 2026 Vinay

"""Command line entry point.

Four things a user needs outside a chat session: start the server, wire it into
a client, build the semantic index, and find out why it is not working.
``doctor`` matters most: nearly every support question about a Zotero MCP
server is "it says it cannot connect", and answering that from the terminal,
with the same code path the server uses, is far faster than reading a client's
log.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from zotero_mcp._version import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotero-mcp",
        description="A Model Context Protocol server for Zotero.",
    )
    parser.add_argument("--version", action="version", version=f"zotero-mcp {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="stdio for a desktop client; http to expose it over a port.",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--toolsets",
        help="Optional tool groups, e.g. 'all', 'none', 'scite,duplicates', 'all,-scite'.",
    )
    serve.add_argument(
        "--compat",
        action="store_true",
        help="Also register every pre-1.0 tool name as an alias.",
    )

    doctor = subparsers.add_parser("doctor", help="Check that Zotero is reachable.")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output.")

    setup = subparsers.add_parser("setup", help="Write MCP client configuration.")
    setup.add_argument(
        "--client",
        choices=["claude-desktop", "claude-code", "print"],
        default="print",
        help="Which client to configure. 'print' just shows the JSON.",
    )
    setup.add_argument("--config-path", help="Override the client config file location.")

    index = subparsers.add_parser("index", help="Manage the semantic search index.")
    index.add_argument("action", choices=["build", "update", "status", "clear"], help="What to do.")
    index.add_argument("--limit", type=int, help="Most items to process.")
    index.add_argument(
        "--no-fulltext", action="store_true", help="Index metadata only, not attachment text."
    )

    subparsers.add_parser("tools", help="List the tools this configuration exposes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    try:
        if command == "serve":
            return _serve(args)
        if command == "doctor":
            return _doctor(args)
        if command == "setup":
            return _setup(args)
        if command == "index":
            return _index(args)
        if command == "tools":
            return _tools()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `zotero-mcp tools | head` closes the pipe on us. That is the user
        # getting what they asked for, not an error to report. Point stdout at
        # devnull so the interpreter's shutdown flush does not raise it again.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as exc:  # noqa: BLE001 (the CLI's job is to report, not to trace)
        message = getattr(exc, "message", None) or str(exc)
        print(f"error: {message}", file=sys.stderr)
        if hint := getattr(exc, "hint", None):
            print(f"hint: {hint}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def _prepare(selection: str | None = None, compat: bool = False):
    """Import the app, register tools, and apply toolset gating."""
    import zotero_mcp.tools
    from zotero_mcp import toolsets
    from zotero_mcp.app import mcp
    from zotero_mcp.config import load_config

    config = load_config()
    enable_compat = compat or config.surface.compat
    if enable_compat:
        import zotero_mcp.compat  # noqa: F401 (registers the legacy aliases)

    from zotero_mcp import prompts, resources  # noqa: F401 (registers both)

    toolsets.apply(
        mcp,
        selection=selection if selection is not None else config.surface.toolsets,
        compat=enable_compat,
    )
    return mcp, config


def _serve(args: argparse.Namespace) -> int:
    mcp, _ = _prepare(args.toolsets, args.compat)
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """Report on configuration and reachability without starting a server."""
    from zotero_mcp import schema
    from zotero_mcp.config import load_config
    from zotero_mcp.content.extract import available_extractors

    config = load_config()
    report: dict[str, Any] = {
        "version": __version__,
        "config_file": config.source_path,
        "mode": config.library.mode.value,
        "library_id": config.library.library_id,
        "api_key_set": bool(config.library.api_key),
        "schema_version": schema.schema_version(),
        "extractors": available_extractors(),
        "backend": None,
        "reachable": False,
        "problems": [],
    }

    try:
        from zotero_mcp.backends.factory import build_backend

        backend = build_backend(config)
        report["backend"] = backend.name
        report["reachable"] = backend.ping()
        if report["reachable"]:
            library = backend.library_ref()
            report["library"] = {
                "id": library.library_id,
                "type": library.library_type,
            }
            report["item_count"] = backend.count_items()
        else:
            report["problems"].append("The backend was built but Zotero did not answer.")
    except Exception as exc:  # noqa: BLE001 (this command exists to report failures)
        report["problems"].append(getattr(exc, "message", None) or str(exc))
        if hint := getattr(exc, "hint", None):
            report["problems"].append(hint)

    from zotero_mcp.index import query as index_query
    from zotero_mcp.runtime import Runtime, set_runtime

    if report["reachable"]:
        set_runtime(Runtime(config=config, backend=backend))
        report["index"] = index_query.status()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["reachable"] else 1

    print(f"zotero-mcp {__version__}")
    print(f"  config file     {report['config_file'] or '(none, using defaults)'}")
    print(f"  mode            {report['mode']}")
    print(f"  library id      {report['library_id'] or '(not set)'}")
    print(f"  api key         {'set' if report['api_key_set'] else 'not set'}")
    print(f"  backend         {report['backend'] or '(could not build)'}")
    print(f"  reachable       {'yes' if report['reachable'] else 'NO'}")
    if count := report.get("item_count"):
        print(f"  items           {count:,}")
    print(f"  zotero schema   v{report['schema_version']}")
    extractors = ", ".join(k for k, v in report["extractors"].items() if v) or "none"
    print(f"  extractors      {extractors}")
    if index := report.get("index"):
        print(f"  semantic index  {index['state']}")

    if report["problems"]:
        print("\nProblems:")
        for problem in report["problems"]:
            print(f"  - {problem}")
        return 1

    print("\nEverything looks reachable.")
    return 0


def _index(args: argparse.Namespace) -> int:
    from zotero_mcp.backends.factory import build_backend
    from zotero_mcp.config import load_config
    from zotero_mcp.index import query as index_query
    from zotero_mcp.index.store import default_db_path
    from zotero_mcp.runtime import Runtime, set_runtime

    config = load_config()

    if args.action == "clear":
        import shutil

        path = default_db_path(config)
        if not path.exists():
            print(f"No index at {path}.")
            return 0
        shutil.rmtree(path)
        print(f"Removed the index at {path}.")
        return 0

    runtime = Runtime(config=config, backend=build_backend(config))
    set_runtime(runtime)

    if args.action == "status":
        state = index_query.status()
        print(f"state  {state['state']}")
        print(f"detail {state.get('detail', '')}")
        if state.get("count") is not None:
            print(f"items  {state['count']:,}")
        return 0

    from zotero_mcp.index.builder import update_index

    def progress(done: int, total: int | None) -> None:
        # Progress goes to stderr, so piping the command's output stays clean.
        suffix = f"/{total}" if total else ""
        print(f"\r  indexed {done}{suffix}...", end="", file=sys.stderr, flush=True)

    stats = update_index(
        runtime,
        limit=args.limit,
        rebuild=args.action == "build",
        include_fulltext=not args.no_fulltext,
        progress=progress,
    )
    print(file=sys.stderr)
    print(
        f"indexed {stats['indexed']:,} items in {stats['chunks']:,} chunks "
        f"({stats['skipped']:,} already current)"
    )
    if stats.get("errors"):
        print(f"{len(stats['errors'])} batch(es) failed; rerun to resume.", file=sys.stderr)
        return 1
    return 0


def _client_config() -> dict[str, Any]:
    """The MCP server entry a client needs.

    The absolute path to this interpreter's script is used rather than a bare
    ``zotero-mcp``: desktop clients do not inherit a shell PATH, and "command
    not found" with no further detail is the most common setup failure.
    """
    executable = Path(sys.executable).parent / "zotero-mcp"
    command = str(executable) if executable.exists() else "zotero-mcp"

    env = {
        name: os.environ[name]
        for name in (
            "ZOTERO_LOCAL",
            "ZOTERO_LIBRARY_ID",
            "ZOTERO_API_KEY",
            "ZOTERO_MCP_MODE",
            "ZOTERO_MCP_TOOLSETS",
            "ZOTERO_MCP_CONTACT_EMAIL",
        )
        if os.environ.get(name)
    }
    return {"command": command, "args": ["serve"], **({"env": env} if env else {})}


def _setup(args: argparse.Namespace) -> int:
    entry = _client_config()

    if args.client == "print":
        print(json.dumps({"mcpServers": {"zotero": entry}}, indent=2))
        return 0

    if args.config_path:
        path = Path(args.config_path).expanduser()
    elif args.client == "claude-desktop":
        path = _claude_desktop_config_path()
    else:
        path = Path.home() / ".claude.json"

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"error: {path} is not valid JSON; refusing to overwrite it.", file=sys.stderr)
            return 1

    servers = existing.setdefault("mcpServers", {})
    replaced = "zotero" in servers
    servers["zotero"] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    print(f"{'Updated' if replaced else 'Added'} the 'zotero' server in {path}.")
    print("Restart the client to pick it up.")
    return 0


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _tools() -> int:
    import asyncio

    mcp, config = _prepare()

    async def collect():
        return sorted(tool.name for tool in await mcp.list_tools())

    names = asyncio.run(collect())
    print(f"{len(names)} tools exposed by this configuration:\n")
    for name in names:
        print(f"  {name}")
    print(f"\ntoolsets: {config.surface.toolsets or '(default)'}")
    print(f"compat aliases: {'on' if config.surface.compat else 'off'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
