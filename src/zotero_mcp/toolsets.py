# Copyright 2026 Vinay

"""Which tools ship, and why the default set is small.

Every registered tool is sent to the client on every request, so the tool
surface is a fixed tax on each session's context window before a single
question is asked. The server this replaces registered fifty-seven tools
unconditionally, several of them near-duplicates competing for the model's
attention, and gave the user no way to turn any of them off.

Here the consolidated core is always present, and everything else is a named
group. Anything not listed below is core, so merging or renaming a core tool
never requires touching this file.

Selection is through ``ZOTERO_MCP_TOOLSETS``:

===========================  ==================================================
Value                        Effect
===========================  ==================================================
unset                        Core plus :data:`DEFAULT_ON`
``all``                      Every group
``none``                     Core only
``scite,duplicates``         Core plus the named groups
``all,-scite``               Every group except the negated ones
===========================  ==================================================

Names are case-insensitive and may be separated by commas or whitespace.
:func:`validate` fails loudly if a name here ever drifts from the real
registry, because FastMCP silently ignores an unknown name and the drift would
otherwise be invisible until a user noticed a missing tool.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

TOOLSETS_ENV_VAR = "ZOTERO_MCP_TOOLSETS"

#: Optional groups. Keep each one a whole capability: a user turning it on
#: should get something they can finish a task with, not a fragment.
TOOLSETS: dict[str, frozenset[str]] = {
    # Library hygiene. Valuable, but maintenance rather than research.
    "duplicates": frozenset({"zotero_duplicates"}),
    # Corpus-level exploration built on the semantic index.
    "discovery": frozenset({"zotero_find_related", "zotero_coverage"}),
    # Semantic index administration. The same work is available from the CLI,
    # so agent access is a convenience rather than the only route.
    "search-admin": frozenset({"zotero_index"}),
    # Scite.ai citation tallies and retraction notices. Calls out to scite.ai
    # and wants the optional [scite] extra.
    "scite": frozenset({"scite_enrich_item", "scite_enrich_search", "scite_check_retractions"}),
    # The ChatGPT deep-research connector contract, which requires tools named
    # exactly "search" and "fetch". Those names are far too generic to expose
    # over stdio, where they collide with every other MCP server installed, so
    # this group is off unless asked for.
    "chatgpt-connector": frozenset({"search", "fetch"}),
    # Every pre-1.0 tool name, as thin aliases. Enabled by ZOTERO_MCP_COMPAT
    # as well as by name.
    "compat": frozenset(),
}

#: Enabled when ``ZOTERO_MCP_TOOLSETS`` is unset. Duplicates and index
#: administration pair closely enough with ordinary work to be on by default;
#: the connector contract and the compatibility aliases do not.
DEFAULT_ON = frozenset({"duplicates", "search-admin", "discovery"})


def parse(value: str | None) -> set[str]:
    """Resolve a toolset specification to the set of enabled group names."""
    if value is None:
        return set(DEFAULT_ON)

    tokens = [t for t in value.replace(",", " ").split() if t]
    if not tokens:
        return set(DEFAULT_ON)

    enabled: set[str] = set()
    negated: set[str] = set()
    for token in tokens:
        name = token.strip().lower()
        if name == "all":
            enabled |= set(TOOLSETS)
        elif name == "none":
            enabled.clear()
        elif name.startswith("-"):
            negated.add(name[1:])
        elif name in TOOLSETS:
            enabled.add(name)
        else:
            logger.warning(
                "Unknown toolset %r; known groups are: %s", name, ", ".join(sorted(TOOLSETS))
            )
    return enabled - negated


def enabled_tools(groups: Iterable[str]) -> set[str]:
    """Tool names belonging to the given groups."""
    names: set[str] = set()
    for group in groups:
        names |= TOOLSETS.get(group, frozenset())
    return names


def optional_tools() -> set[str]:
    """Every tool that belongs to some optional group."""
    return {name for members in TOOLSETS.values() for name in members}


def apply(server: FastMCP, *, selection: str | None = None, compat: bool = False) -> set[str]:
    """Disable every optional tool whose group is not enabled.

    Returns the set of enabled group names, for logging and health output.
    """
    groups = parse(selection if selection is not None else os.environ.get(TOOLSETS_ENV_VAR))
    if compat:
        groups.add("compat")

    keep = enabled_tools(groups)
    for name in sorted(optional_tools() - keep):
        _remove(server, name)

    logger.info("Toolsets enabled: %s", ", ".join(sorted(groups)) or "none")
    return groups


def _remove(server: FastMCP, name: str) -> None:
    """Unregister one tool, across the FastMCP versions that move the method.

    FastMCP 4 keeps the registry on ``local_provider``; earlier versions expose
    ``remove_tool`` on the server itself. Trying both means the gating works on
    whichever version the user actually installed, rather than silently doing
    nothing on one of them.
    """
    for target in (getattr(server, "local_provider", None), server):
        remove = getattr(target, "remove_tool", None)
        if remove is None:
            continue
        try:
            remove(name)
            return
        except Exception:  # noqa: BLE001 (a tool that never registered is fine)
            logger.debug("Toolset gating: %s was not registered", name)
            return
    logger.warning("Cannot disable %s: this FastMCP has no tool removal API", name)


def validate(registered: Iterable[str]) -> list[str]:
    """Names listed here that do not exist in the registry.

    Exercised by the test suite. FastMCP ignores an unknown name silently, so
    without this check a typo would quietly leave a tool always enabled.
    """
    known = set(registered)
    return sorted(name for name in optional_tools() if name not in known)


__all__ = [
    "DEFAULT_ON",
    "TOOLSETS",
    "TOOLSETS_ENV_VAR",
    "apply",
    "enabled_tools",
    "optional_tools",
    "parse",
    "validate",
]
