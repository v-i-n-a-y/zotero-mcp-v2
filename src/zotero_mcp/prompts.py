# Copyright 2026 Vinay

"""MCP prompts: workflows a user can invoke by name.

Hosts surface these as slash commands. Each returns an instruction that chains
the tools in a sensible order, which is worth having because the ordering is
not obvious: search before reading, read by range before quoting, and cite item
keys so the user can find what was used.

Nothing here touches Zotero at import time, so these load in any environment.
"""

from __future__ import annotations

from zotero_mcp.app import mcp


@mcp.prompt(
    name="literature_review",
    description="Review what the library holds on a topic, grounded in real items.",
)
def literature_review(topic: str, depth: str = "standard") -> str:
    """Guide a grounded review of *topic*."""
    steps = [
        f"Conduct a literature review of what this Zotero library holds on: **{topic}**.",
        "",
        "Work through these steps. Cite every claim with the item key it came from.",
        "",
        f"1. `zotero_search(query='{topic}', mode='semantic', limit=15)` to find the most "
        "relevant items. If that reports no index, fall back to `mode='auto'`.",
        "2. Group the results into themes. For each theme, name the key items and "
        "summarise their contribution in one or two sentences.",
    ]
    if depth in {"standard", "deep"}:
        steps += [
            "3. For the two or three most central items, call "
            "`zotero_read(item_key=..., pages='1-3')` and quote the actual claim rather "
            "than paraphrasing the abstract.",
            "4. Call `zotero_get_annotations(item_key=...)` on those items: the user's own "
            "highlights say what they already thought was important.",
        ]
    if depth == "deep":
        steps += [
            f"5. `zotero_coverage(topic='{topic}')` to judge whether the library is thin "
            "here, and say plainly what is missing rather than implying the library is "
            "complete.",
            "6. `zotero_find_related(item_key=...)` on the central items to surface work "
            "the search missed.",
        ]
    steps += [
        "",
        "Finish with: the themes, the strongest evidence for each, disagreements between "
        "sources, and what the library does not cover.",
    ]
    return "\n".join(steps)


@mcp.prompt(
    name="summarise_paper",
    description="Read one paper properly and summarise it with page-level citations.",
)
def summarise_paper(item_key: str) -> str:
    """Guide a careful read of one item."""
    return "\n".join(
        [
            f"Summarise the paper with Zotero key `{item_key}`.",
            "",
            f"1. `zotero_get_item(keys='{item_key}')` for the metadata and what is attached.",
            f"2. `zotero_read(item_key='{item_key}', what='outline')` to see its structure. "
            "If it has no outline, skip to the next step.",
            f"3. `zotero_read(item_key='{item_key}', pages='1-5')`, then continue with the "
            "range the response hands back, until you have the argument. Do not try to read "
            "the whole paper in one call.",
            f"4. `zotero_get_annotations(item_key='{item_key}')` for the user's own "
            "highlights, and say where your reading agrees or disagrees with them.",
            "",
            "Write: the question it asks, what it did, what it found, what it claims, and "
            "the limitations it admits. Give a page number for every specific claim.",
        ]
    )


@mcp.prompt(
    name="tidy_library",
    description="Find duplicates, untagged items and uncollected items worth cleaning up.",
)
def tidy_library(collection: str = "") -> str:
    """Guide a library hygiene pass."""
    scope = f" within collection `{collection}`" if collection else ""
    return "\n".join(
        [
            f"Do a library hygiene pass{scope}. Propose changes; do not apply any of them "
            "without asking first.",
            "",
            "1. `zotero_duplicates(action='find')` and report each group, saying which copy "
            "looks worth keeping and why.",
            "2. `zotero_library(action='uncollected')` for items filed nowhere, and suggest "
            "a collection for each based on its subject.",
            "3. `zotero_tags(action='list')` and point out near-duplicate tags, such as "
            "singular and plural forms of the same word.",
            "",
            "Present everything as a numbered list of proposed changes with the exact tool "
            "call for each, so the user can approve them one at a time. Every write tool "
            "here previews by default; leave dry_run alone until they say to apply.",
        ]
    )


@mcp.prompt(
    name="find_support",
    description="Find items in the library that support or contradict a specific claim.",
)
def find_support(claim: str) -> str:
    """Guide a search for evidence for and against a claim."""
    return "\n".join(
        [
            f"Find what this library says about the claim: **{claim}**",
            "",
            f"1. `zotero_search(query='{claim}', mode='semantic', limit=15)`.",
            "2. `zotero_search` again with `mode='fulltext'` and the two or three most "
            "distinctive terms from the claim, since semantic and substring search miss "
            "different things.",
            "3. For each promising item, `zotero_read` the relevant pages and quote the "
            "passage exactly, with its page number.",
            "",
            "Report three groups: passages that support the claim, passages that "
            "contradict or qualify it, and items that look relevant but turned out not to "
            "address it. If nothing in the library speaks to the claim, say so rather than "
            "stretching a weak match.",
        ]
    )


__all__ = ["find_support", "literature_review", "summarise_paper", "tidy_library"]
