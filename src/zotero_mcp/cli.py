"""Command-line entrypoint.

The console script declared in ``pyproject.toml`` (``zotero-mcp``) lands here.
Its most important job is ``serve``: start the MCP server over stdio, which is
how an MCP client (Claude Code, Claude Desktop) launches and speaks to it.

One rule dominates this module: on the stdio transport, **stdout belongs to the
protocol**. A single stray ``print`` corrupts the JSON-RPC stream and the
client drops the connection. So all diagnostics go to stderr, and logging is
configured to stderr before anything else happens.
"""

from __future__ import annotations

import argparse
import logging
import sys

from zotero_mcp import __version__


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        stream=sys.stderr,  # never stdout: it carries the MCP protocol
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    from zotero_mcp.config import load_config
    from zotero_mcp.server import build_server

    config = load_config(args.config)
    server = build_server(config)
    # FastMCP's stdio transport owns the process from here until the client
    # disconnects. Blocking call by design.
    server.run(transport="stdio")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    from zotero_mcp.backends import make_backend
    from zotero_mcp.config import load_config

    config = load_config(args.config)
    backend = make_backend(config)
    where = "local desktop API" if backend.local else "web API"
    print(f"Backend:     {where}")
    print(f"Library:     {backend.library_type}/{backend.library_id}")
    print(f"Config from: {config.source_path or 'defaults + environment'}")
    try:
        backend.ping()
    except Exception as exc:  # noqa: BLE001 - this command's whole job is to report it
        print(f"Reachable:   NO — {exc}", file=sys.stderr)
        return 1
    print("Reachable:   yes")
    return 0


def _cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    from zotero_mcp.backends import make_backend
    from zotero_mcp.config import load_config
    from zotero_mcp.index import SemanticIndex, semantic_available

    if not semantic_available():
        print(
            "Semantic search needs the optional dependencies.\n"
            "Install with: uv tool install --force '.[semantic]'",
            file=sys.stderr,
        )
        return 1

    config = load_config(args.config)
    index = SemanticIndex(config)

    if args.index_command == "status":
        state, count = index.status()
        print(f"Index:    {state}")
        print(f"Items:    {count:,}")
        print(f"Store:    {index.db_path}")
        print(f"Model:    {index.fingerprint}")
        return 0

    if args.index_command == "clear":
        index.clear()
        print("Index cleared.")
        return 0

    # build
    backend = make_backend(config)
    print(f"Building index at {index.db_path} with {index.fingerprint} …", file=sys.stderr)
    seen = {"n": 0}

    def progress(_key: str, stats: object) -> None:
        seen["n"] += 1
        if seen["n"] % 25 == 0:
            print(f"  … {seen['n']} items processed", file=sys.stderr)

    stats = index.build(backend, progress=progress)
    print(
        f"Done. added={stats.added} updated={stats.updated} unchanged={stats.skipped} "
        f"removed={stats.removed} chunks={stats.chunks} with_fulltext={stats.fulltext_items} "
        f"fulltext_cache_hits={stats.fulltext_cache_hits}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotero-mcp",
        description="A Model Context Protocol server for Zotero.",
    )
    parser.add_argument("--version", action="version", version=f"zotero-mcp {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (repeat for debug). Logs go to stderr.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a config JSON file (overrides ZOTERO_MCP_CONFIG).",
    )

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the MCP server over stdio.")
    p_serve.set_defaults(func=_cmd_serve)

    p_health = sub.add_parser("health", help="Check the configured library is reachable.")
    p_health.set_defaults(func=_cmd_health)

    p_version = sub.add_parser("version", help="Print the version and exit.")
    p_version.set_defaults(func=_cmd_version)

    p_index = sub.add_parser("index", help="Manage the semantic search index.")
    p_index.set_defaults(func=_cmd_index)
    index_sub = p_index.add_subparsers(dest="index_command")
    index_sub.add_parser("build", help="Build or incrementally update the index.")
    index_sub.add_parser("status", help="Show index state and size.")
    index_sub.add_parser("clear", help="Delete the index.")
    p_index.set_defaults(index_command="build")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if not getattr(args, "command", None):
        # No subcommand: default to serving, since that is what an MCP client
        # invokes. A human gets the help text via --help.
        args.func = _cmd_serve
        args.config = getattr(args, "config", None)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # top-level guard: report, don't traceback at users
        from zotero_mcp.errors import ZoteroMcpError

        if isinstance(exc, ZoteroMcpError):
            print(exc.render(), file=sys.stderr)
        else:
            logging.getLogger(__name__).exception("Fatal error")
            print(f"Fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
