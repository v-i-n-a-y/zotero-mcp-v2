"""One typed configuration object, loaded once.

The predecessor read ``~/.config/zotero-mcp/config.json`` from eight different
modules across thirty-one call sites, each with its own ad-hoc ``json.load``
and its own idea of the defaults. That makes the tools untestable without a
filesystem and means a key's meaning depends on which module happened to read
it.

Here, configuration is resolved exactly once into a frozen
:class:`ZoteroConfig`. Precedence, highest first:

1. explicit arguments to :func:`load_config` (used by the CLI and tests),
2. environment variables,
3. the JSON config file,
4. the defaults declared on the dataclasses below.

Every setting is declared in one place with a type and a docstring, and the
whole tree is validated by pydantic on load, so a typo in the JSON file
produces a clear message at startup rather than a surprising ``None`` deep
inside a tool.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"

#: Zotero's desktop client serves its read-only HTTP API here.
LOCAL_API_BASE = "http://localhost:23119/api"


class LibraryMode(str, Enum):
    """How the server talks to Zotero.

    ``AUTO`` resolves at startup to ``LOCAL`` when the desktop client answers
    and credentials are absent, to ``HYBRID`` when both are available, and to
    ``WEB`` otherwise. Naming the mode explicitly disables that probing, which
    matters for reproducible behaviour in tests and CI.
    """

    AUTO = "auto"
    LOCAL = "local"
    WEB = "web"
    HYBRID = "hybrid"


class LibraryType(str, Enum):
    """Whether the active library is a personal or a group library."""

    USER = "user"
    GROUP = "group"


@dataclass(frozen=True)
class LibrarySettings:
    """Which Zotero library to talk to, and how."""

    #: Numeric library id. Local mode defaults it to "0", the desktop client's
    #: stand-in for "whatever library this Zotero has open".
    library_id: str | None = None
    library_type: LibraryType = LibraryType.USER
    api_key: str | None = None
    mode: LibraryMode = LibraryMode.AUTO
    #: Base URL of the local HTTP API. Overridable because Zotero 7 beta
    #: builds and some packaged installs move the port.
    local_api_base: str = LOCAL_API_BASE
    #: Path to zotero.sqlite. Empty means "derive it from the Zotero data
    #: directory". Used by the direct-SQLite read fast path.
    sqlite_path: str | None = None
    #: Zotero storage directory, for resolving attachment files on disk.
    storage_path: str | None = None


@dataclass(frozen=True)
class LimitSettings:
    """Ceilings that keep any single tool response bounded.

    These exist because an unbounded response is the single most damaging
    thing a research MCP server can do: one call that returns a whole PDF can
    consume the entire remaining context and end the session's usefulness.
    Every read tool in this package clamps against these values and reports
    truncation explicitly rather than silently emitting more.
    """

    #: Hard ceiling on the characters any one tool may return. Roughly
    #: 6k tokens at ~4 chars/token — enough for a rich answer, small enough
    #: that a bad call is survivable.
    max_response_chars: int = 24_000
    #: Ceiling for tools whose whole purpose is bulk text (page reads).
    max_content_chars: int = 60_000
    default_page_size: int = 20
    max_page_size: int = 100
    #: Pages returned by a single ranged PDF read when no range is given.
    default_pdf_pages: int = 5
    max_pdf_pages: int = 40
    #: Characters of each item's abstract shown in a search result list.
    abstract_preview_chars: int = 320
    #: Wall-clock budget for the search fallback cascade.
    search_timeout_seconds: float = 45.0
    #: Per-request timeout for the Zotero API and third-party services.
    http_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class SemanticSettings:
    """Vector search over the library. Entirely optional.

    The index is built by the CLI (``zotero-mcp index build``) and only *read*
    by the server, so a broken or absent index degrades semantic search to a
    clear error instead of taking the server down with it.
    """

    enabled: bool = True
    #: "default" (local sentence-transformers), "openai", or "gemini".
    embedding_provider: str = "default"
    embedding_model: str | None = None
    #: Where ChromaDB persists. Empty means the default under the config dir.
    db_path: str | None = None
    #: "manual", "startup", "daily", or "weekly".
    update_schedule: str = "manual"
    chunk_chars: int = 2_000
    chunk_overlap_chars: int = 200
    #: Include attachment fulltext in the index, not just metadata.
    index_fulltext: bool = True


@dataclass(frozen=True)
class NetworkSettings:
    """Outbound HTTP behaviour for Zotero and third-party enrichment."""

    user_agent: str = "zotero-mcp-next"
    max_retries: int = 3
    backoff_seconds: float = 1.0
    #: Contact address sent to Unpaywall and Crossref, which require one for
    #: their polite pools. Without it those services are skipped rather than
    #: called anonymously against their terms of use.
    contact_email: str | None = None
    #: Master switch for every call that leaves the machine for something
    #: other than Zotero itself. Off means metadata enrichment and OA PDF
    #: retrieval are unavailable, which is the right default for users who
    #: chose local mode for privacy reasons.
    allow_external_services: bool = True


@dataclass(frozen=True)
class SurfaceSettings:
    """Which tools this server exposes.

    ``toolsets`` follows the syntax documented in :mod:`zotero_mcp.toolsets`
    (``all``, ``none``, ``scite,feeds``, ``all,-scite``). ``compat`` restores
    the pre-1.0 tool names as thin aliases for installations whose MCP client
    config or saved prompts reference them.
    """

    toolsets: str | None = None
    compat: bool = False
    #: Emit structured content alongside markdown. Disable only for clients
    #: that mishandle structured tool results.
    structured_output: bool = True
    #: Require dry_run=False to be passed explicitly before anything is
    #: deleted, merged or emptied.
    confirm_destructive: bool = True


@dataclass(frozen=True)
class ZoteroConfig:
    """The complete, resolved configuration for one server process."""

    library: LibrarySettings = field(default_factory=LibrarySettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    semantic: SemanticSettings = field(default_factory=SemanticSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    surface: SurfaceSettings = field(default_factory=SurfaceSettings)
    #: Where this configuration came from, for diagnostics.
    source_path: str | None = None

    def with_library(self, **changes: Any) -> ZoteroConfig:
        """Return a copy with library settings replaced (used by switch_library)."""
        return replace(self, library=replace(self.library, **changes))


# ---------------------------------------------------------------------------
# Environment mapping
# ---------------------------------------------------------------------------


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return None


def _env_overrides() -> dict[str, dict[str, Any]]:
    """Collect environment settings as a sparse nested dict.

    ``ZOTERO_LIBRARY_ID``, ``ZOTERO_API_KEY`` and ``ZOTERO_LOCAL`` keep the
    names the wider Zotero-MCP ecosystem already uses, so existing client
    configurations keep working untouched. Everything this package adds is
    namespaced under ``ZOTERO_MCP_``.
    """
    library: dict[str, Any] = {}
    if v := os.environ.get("ZOTERO_LIBRARY_ID"):
        library["library_id"] = v
    if v := os.environ.get("ZOTERO_LIBRARY_TYPE"):
        library["library_type"] = v
    if v := os.environ.get("ZOTERO_API_KEY"):
        library["api_key"] = v
    if v := os.environ.get("ZOTERO_MCP_MODE"):
        library["mode"] = v
    elif _env_bool("ZOTERO_LOCAL"):
        # The legacy switch. Prefer hybrid when write credentials also exist,
        # which is what a local-mode user with an API key actually wants:
        # fast local reads, working writes.
        library["mode"] = "hybrid" if os.environ.get("ZOTERO_API_KEY") else "local"
    if v := os.environ.get("ZOTERO_LOCAL_API_BASE"):
        library["local_api_base"] = v
    if v := os.environ.get("ZOTERO_SQLITE_PATH"):
        library["sqlite_path"] = v
    if v := os.environ.get("ZOTERO_STORAGE_PATH"):
        library["storage_path"] = v

    limits: dict[str, Any] = {}
    for env_name, key in (
        ("ZOTERO_MCP_MAX_RESPONSE_CHARS", "max_response_chars"),
        ("ZOTERO_MCP_MAX_CONTENT_CHARS", "max_content_chars"),
        ("ZOTERO_MCP_PAGE_SIZE", "default_page_size"),
        ("ZOTERO_MCP_MAX_PDF_PAGES", "max_pdf_pages"),
    ):
        if (n := _env_int(env_name)) is not None:
            limits[key] = n

    semantic: dict[str, Any] = {}
    if (b := _env_bool("ZOTERO_MCP_SEMANTIC")) is not None:
        semantic["enabled"] = b
    if v := os.environ.get("ZOTERO_MCP_EMBEDDING_PROVIDER"):
        semantic["embedding_provider"] = v
    if v := os.environ.get("ZOTERO_MCP_EMBEDDING_MODEL"):
        semantic["embedding_model"] = v
    if v := os.environ.get("ZOTERO_MCP_DB_PATH"):
        semantic["db_path"] = v

    network: dict[str, Any] = {}
    if v := os.environ.get("ZOTERO_MCP_CONTACT_EMAIL"):
        network["contact_email"] = v
    if (b := _env_bool("ZOTERO_MCP_ALLOW_EXTERNAL")) is not None:
        network["allow_external_services"] = b

    surface: dict[str, Any] = {}
    if v := os.environ.get("ZOTERO_MCP_TOOLSETS"):
        surface["toolsets"] = v
    if (b := _env_bool("ZOTERO_MCP_COMPAT")) is not None:
        surface["compat"] = b
    if (b := _env_bool("ZOTERO_MCP_STRUCTURED_OUTPUT")) is not None:
        surface["structured_output"] = b

    return {
        k: v
        for k, v in {
            "library": library,
            "limits": limits,
            "semantic": semantic,
            "network": network,
            "surface": surface,
        }.items()
        if v
    }


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read and lightly migrate the JSON config file.

    A malformed file is a warning, not a fatal error: the server is far more
    useful running on defaults than refusing to start because one key is
    mistyped, and the warning names the file so the user can fix it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable config at %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring config at %s: top level is not an object", path)
        return {}
    return _migrate_legacy(raw)


def _migrate_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate the pre-1.0 config layout into this one.

    The old file nested everything the semantic indexer needed under
    ``semantic_search``, including extraction limits that now live under
    ``limits``. Users upgrading should not have to hand-edit a config file, so
    the old keys are read and mapped; unknown keys are dropped by the
    validation step below with a warning.
    """
    if "semantic_search" not in raw:
        return raw

    migrated = {k: v for k, v in raw.items() if k != "semantic_search"}
    old = raw.get("semantic_search") or {}
    if not isinstance(old, dict):
        return migrated

    semantic = dict(migrated.get("semantic") or {})
    for old_key, new_key in (
        ("embedding_model", "embedding_provider"),
        ("chroma_db_path", "db_path"),
        ("update_frequency", "update_schedule"),
    ):
        if old_key in old and new_key not in semantic:
            semantic[old_key if new_key is None else new_key] = old[old_key]
    if semantic:
        migrated["semantic"] = semantic

    library = dict(migrated.get("library") or {})
    if (db := old.get("zotero_db_path")) and "sqlite_path" not in library:
        library["sqlite_path"] = db
    if library:
        migrated["library"] = library

    extraction = old.get("extraction") or {}
    if isinstance(extraction, dict):
        limits = dict(migrated.get("limits") or {})
        if (n := extraction.get("fulltext_display_max_pages")) and "max_pdf_pages" not in limits:
            limits["max_pdf_pages"] = n
        if limits:
            migrated["limits"] = limits

    logger.info("Migrated legacy 'semantic_search' config section")
    return migrated


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge *overlay* onto *base*, one level into each section."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


_ADAPTER = TypeAdapter(ZoteroConfig)


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> ZoteroConfig:
    """Resolve configuration from file, environment and explicit overrides.

    Args:
        path: Config file to read. Defaults to ``~/.config/zotero-mcp/config.json``;
            ``ZOTERO_MCP_CONFIG`` overrides that.
        overrides: Highest-precedence sparse nested dict, as produced by CLI flags.
        use_env: Read environment variables. Tests set this false for isolation.

    Raises:
        InvalidInput: The merged configuration failed validation, naming the
            offending field.
    """
    from zotero_mcp.errors import InvalidInput

    config_path = Path(path or os.environ.get("ZOTERO_MCP_CONFIG") or DEFAULT_CONFIG_PATH)
    data = _read_config_file(config_path)
    if use_env:
        data = _deep_merge(data, _env_overrides())
    if overrides:
        data = _deep_merge(data, overrides)
    data["source_path"] = str(config_path) if config_path.exists() else None

    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first["loc"])
        raise InvalidInput(
            f"Invalid configuration at '{location}': {first['msg']}",
            hint=f"Fix {config_path} or the corresponding environment variable.",
        ) from exc


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "LibraryMode",
    "LibrarySettings",
    "LibraryType",
    "LimitSettings",
    "NetworkSettings",
    "SemanticSettings",
    "SurfaceSettings",
    "ZoteroConfig",
    "load_config",
]
