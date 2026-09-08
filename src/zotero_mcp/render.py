"""The markdown half of every tool result.

Rendering lives apart from the models on purpose. The models are the contract
with the calling model; this is presentation, and presentation changes far more
often than a contract should. Keeping them separate means a formatting tweak
cannot break a caller that depends on a field.

Two conventions run through everything here:

* **Every rendered object shows its key.** The key is what the next call needs.
  Burying it, or omitting it because it is "internal", forces the model to
  guess — and it will.
* **Nothing here decides how much to emit.** Budgets belong to the caller,
  which knows the tool's limit; these functions render what they are given.
"""

from __future__ import annotations

from zotero_mcp.models import (
    Annotation,
    CollectionPage,
    ContentChunk,
    Creator,
    DuplicateGroup,
    HealthReport,
    ItemDetail,
    ItemSummary,
    LibraryStats,
    Note,
    OutlineEntry,
    ResultPage,
    TagCount,
    WriteResult,
)

#: Beyond this many creators, name the first and elide the rest. Full author
#: lists on a physics paper can run to thousands and carry no information for
#: the reader.
_CREATOR_ELISION_THRESHOLD = 3


def creator_summary(creators: list[Creator]) -> str:
    """Human-readable author string: 'Smith', 'Smith & Jones', 'Smith et al.'."""
    authors = [c for c in creators if c.creator_type in {"author", "presenter", "artist", "director"}]
    people = authors or creators
    if not people:
        return ""
    names = [c.last_name or c.name or c.first_name or "" for c in people]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    if len(names) < _CREATOR_ELISION_THRESHOLD + 1:
        return f"{', '.join(names[:-1])} & {names[-1]}"
    return f"{names[0]} et al."


def _bullet(summary: ItemSummary, index: int | None = None) -> str:
    """One search-result row: identity first, then provenance, then preview."""
    marker = f"{index}. " if index is not None else "- "
    head = f"{marker}**{summary.title or 'Untitled'}**"

    facts: list[str] = []
    byline = summary.creator_summary or creator_summary(summary.creators)
    if byline:
        facts.append(byline)
    if summary.year:
        facts.append(summary.year)
    if summary.publication:
        facts.append(f"*{summary.publication}*")
    facts.append(summary.item_type)
    lines = [f"{head}  \n  {' · '.join(facts)}"]

    refs = [f"`{summary.key}`"]
    if summary.citation_key:
        refs.append(f"@{summary.citation_key}")
    if summary.doi:
        refs.append(f"doi:{summary.doi}")
    if summary.has_pdf:
        refs.append("PDF")
    if summary.score is not None:
        refs.append(f"score {summary.score:.2f}")
    lines.append(f"  {' · '.join(refs)}")

    if summary.matched_text:
        lines.append(f"  > {summary.matched_text.strip()}")
    elif summary.abstract_preview:
        lines.append(f"  {summary.abstract_preview.strip()}")
    if summary.tags:
        lines.append("  " + " ".join(f"`{t}`" for t in summary.tags[:8]))
    return "\n".join(lines)


def render_result_page(page: ResultPage, *, heading: str, numbered: bool = True) -> str:
    """A page of search results, with an honest count and a way to continue."""
    if not page.items:
        parts = [f"# {heading}", "", "No matching items."]
        if page.diagnostics and page.diagnostics.suggestion:
            parts += ["", page.diagnostics.suggestion]
        if page.diagnostics and page.diagnostics.attempts:
            parts += ["", f"*Tried: {', '.join(page.diagnostics.attempts)}.*"]
        return "\n".join(parts)

    first = page.offset + 1
    last = page.offset + len(page.items)
    if page.total is not None and page.total > len(page.items):
        count = f"Showing {first}–{last} of {page.total}"
    elif page.offset:
        count = f"Showing {first}–{last}"
    else:
        count = f"{len(page.items)} result{'s' if len(page.items) != 1 else ''}"

    lines = [f"# {heading}", "", f"*{count}*", ""]
    for offset, item in enumerate(page.items):
        lines.append(_bullet(item, first + offset if numbered else None))
        lines.append("")

    if page.next_cursor:
        lines.append(f"*More results available — pass `cursor=\"{page.next_cursor}\"` to continue.*")
    if page.diagnostics:
        diag = page.diagnostics
        if diag.timed_out:
            lines.append("*Search hit its time budget; these are the best results found so far.*")
        elif diag.strategy not in {"direct", ""}:
            lines.append(f"*Matched via {diag.strategy}.*")
        if diag.suggestion:
            lines.append(f"*{diag.suggestion}*")
    return "\n".join(lines).rstrip()


def render_item_detail(detail: ItemDetail) -> str:
    """Full metadata for one item.

    Known fields are laid out first; everything else the item carries is listed
    afterwards rather than dropped, so an unusual item type still renders
    completely instead of losing exactly the fields that make it unusual.
    """
    lines = [f"# {detail.title or 'Untitled'}", ""]

    facts = [f"**Type:** {detail.item_type}", f"**Key:** `{detail.key}`"]
    if detail.version is not None:
        facts.append(f"**Version:** {detail.version}")
    if detail.date:
        facts.append(f"**Date:** {detail.date}")
    lines.append("  \n".join(facts))

    if detail.creators:
        by_role: dict[str, list[str]] = {}
        for creator in detail.creators:
            by_role.setdefault(creator.creator_type, []).append(creator.display)
        for role, names in by_role.items():
            label = role[0].upper() + role[1:]
            lines.append(f"**{label}s:** {', '.join(names)}" if len(names) > 1
                         else f"**{label}:** {names[0]}")

    identifiers = []
    if detail.doi:
        identifiers.append(f"**DOI:** {detail.doi}")
    if detail.url:
        identifiers.append(f"**URL:** {detail.url}")
    if detail.citation_key:
        identifiers.append(f"**Citation key:** `@{detail.citation_key}`")
    if identifiers:
        lines.append("  \n".join(identifiers))

    if detail.tags:
        lines.append("**Tags:** " + " ".join(f"`{t}`" for t in detail.tags))
    if detail.collections:
        names = ", ".join(f"{c.name} (`{c.key}`)" for c in detail.collections)
        lines.append(f"**Collections:** {names}")

    if detail.abstract:
        lines += ["", "## Abstract", "", detail.abstract]

    # Remaining fields, minus the ones already rendered above.
    rendered = {"title", "abstractNote", "DOI", "url", "date", "creators", "tags",
                "collections", "relations", "key", "version", "itemType", "extra",
                "dateAdded", "dateModified"}
    extra_fields = {k: v for k, v in detail.fields.items() if k not in rendered and v}
    if extra_fields:
        lines += ["", "## Fields", ""]
        lines += [f"- **{k}:** {v}" for k, v in sorted(extra_fields.items())]

    if detail.attachments:
        lines += ["", "## Attachments", ""]
        for att in detail.attachments:
            bits = [f"`{att.key}`", att.title or "Untitled"]
            if att.content_type:
                bits.append(att.content_type)
            if att.page_count:
                bits.append(f"{att.page_count} pages")
            if att.available is False:
                bits.append("**file not available**")
            lines.append(f"- {' · '.join(bits)}")

    counts = []
    if detail.note_count:
        counts.append(f"{detail.note_count} note{'s' if detail.note_count != 1 else ''}")
    if detail.annotation_count:
        counts.append(f"{detail.annotation_count} annotation"
                      f"{'s' if detail.annotation_count != 1 else ''}")
    if counts:
        lines += ["", f"*Also has: {', '.join(counts)}.*"]

    if detail.related_keys:
        lines += ["", "## Related items", ""]
        lines += [f"- `{k}`" for k in detail.related_keys]

    if detail.fields.get("extra"):
        lines += ["", "## Extra", "", str(detail.fields["extra"])]

    return "\n".join(lines)


def render_content_chunk(chunk: ContentChunk) -> str:
    """A ranged document read, always stating the range and how to continue."""
    header = f"# {chunk.title or chunk.item_key}"
    scope = []
    if chunk.first_page and chunk.last_page:
        if chunk.first_page == chunk.last_page:
            scope.append(f"page {chunk.first_page}")
        else:
            scope.append(f"pages {chunk.first_page}–{chunk.last_page}")
    if chunk.total_pages:
        scope.append(f"of {chunk.total_pages}")
    if chunk.source:
        scope.append(f"via {chunk.source}")

    lines = [header, ""]
    if scope:
        lines += [f"*{' '.join(scope)} · {chunk.chars:,} characters*", ""]
    lines.append(chunk.text)

    if chunk.has_more and chunk.next_pages:
        lines += ["", f"*More to read — call again with `pages=\"{chunk.next_pages}\"`.*"]
    elif chunk.truncated:
        lines += ["", "*Output was truncated to fit the response budget.*"]
    return "\n".join(lines)


def render_annotations(annotations: list[Annotation], *, heading: str) -> str:
    """Annotations grouped by page, in reading order."""
    if not annotations:
        return f"# {heading}\n\nNo annotations found."

    lines = [f"# {heading}", "", f"*{len(annotations)} annotation"
             f"{'s' if len(annotations) != 1 else ''}*", ""]
    current_page: str | None = object()  # sentinel distinct from any real label
    for note in annotations:
        page = note.page_label or (str(note.page_index) if note.page_index is not None else None)
        if page != current_page:
            current_page = page
            lines += [f"### Page {page}" if page else "### Unpaged", ""]
        prefix = {"highlight": "> ", "underline": "> "}.get(note.annotation_type, "")
        if note.text:
            lines.append(f"{prefix}{note.text.strip()}")
        if note.comment:
            lines.append(f"**Comment:** {note.comment.strip()}")
        meta = [f"`{note.key}`", note.annotation_type]
        if note.color:
            meta.append(note.color)
        if note.tags:
            meta.append(" ".join(f"`{t}`" for t in note.tags))
        lines += [f"<sub>{' · '.join(meta)}</sub>", ""]
    return "\n".join(lines).rstrip()


def render_notes(notes: list[Note], *, heading: str) -> str:
    """Notes, each with its key so it can be updated or deleted afterwards."""
    if not notes:
        return f"# {heading}\n\nNo notes found."
    lines = [f"# {heading}", "", f"*{len(notes)} note{'s' if len(notes) != 1 else ''}*", ""]
    for note in notes:
        title = note.title or "Untitled note"
        lines.append(f"## {title}")
        parent = f" · child of `{note.parent_key}`" if note.parent_key else " · standalone"
        lines.append(f"<sub>`{note.key}`{parent}</sub>")
        lines += ["", note.text.strip(), ""]
        if note.tags:
            lines += [" ".join(f"`{t}`" for t in note.tags), ""]
    return "\n".join(lines).rstrip()


def render_collections(page: CollectionPage, *, heading: str) -> str:
    """A collection listing, indented by depth when paths are available."""
    if not page.collections:
        return f"# {heading}\n\nNo collections found."
    lines = [f"# {heading}", "", f"*{len(page.collections)} collection"
             f"{'s' if len(page.collections) != 1 else ''}*", ""]
    for collection in page.collections:
        depth = collection.path.count("/") if collection.path else 0
        counts = []
        if collection.item_count is not None:
            counts.append(f"{collection.item_count} items")
        if collection.subcollection_count:
            counts.append(f"{collection.subcollection_count} subcollections")
        suffix = f" — {', '.join(counts)}" if counts else ""
        lines.append(f"{'  ' * depth}- **{collection.name}** (`{collection.key}`){suffix}")
    if page.next_cursor:
        lines += ["", f"*More — pass `cursor=\"{page.next_cursor}\"` to continue.*"]
    return "\n".join(lines)


def render_tags(tags: list[TagCount], *, heading: str) -> str:
    if not tags:
        return f"# {heading}\n\nNo tags found."
    lines = [f"# {heading}", "", f"*{len(tags)} tag{'s' if len(tags) != 1 else ''}*", ""]
    for tag in tags:
        count = f" ({tag.count})" if tag.count is not None else ""
        auto = " *auto*" if tag.automatic else ""
        lines.append(f"- `{tag.tag}`{count}{auto}")
    return "\n".join(lines)


def render_outline(entries: list[OutlineEntry], *, heading: str) -> str:
    if not entries:
        return f"# {heading}\n\nThis document has no embedded outline."
    lines = [f"# {heading}", ""]
    for entry in entries:
        page = f" — p. {entry.page}" if entry.page else ""
        lines.append(f"{'  ' * max(0, entry.level - 1)}- {entry.title}{page}")
    return "\n".join(lines)


def render_write_result(result: WriteResult) -> str:
    """A write outcome — or, unmistakably, a preview of one.

    The dry-run banner leads, because a preview mistaken for a completed write
    is the failure mode that costs a user real data.
    """
    verb = result.action.replace("_", " ")
    if result.dry_run:
        lines = [f"# Preview: {verb}", "",
                 "**Nothing was changed.** Re-run with `dry_run=False` to apply.", ""]
    else:
        lines = [f"# {verb[0].upper()}{verb[1:]}", ""]

    if result.message:
        lines += [result.message, ""]

    if result.changes:
        lines += ["| Field | Before | After |", "| --- | --- | --- |"]
        for change in result.changes:
            before = "—" if change.before in (None, "", []) else str(change.before)
            after = "—" if change.after in (None, "", []) else str(change.after)
            lines.append(f"| {change.field} | {before[:120]} | {after[:120]} |")
        lines.append("")

    if result.succeeded:
        label = "Would affect" if result.dry_run else "Affected"
        keys = ", ".join(f"`{k}`" for k in result.succeeded[:25])
        more = f" (+{len(result.succeeded) - 25} more)" if len(result.succeeded) > 25 else ""
        lines.append(f"**{label} {len(result.succeeded)}:** {keys}{more}")
    if result.unchanged:
        lines.append(f"**Already up to date:** {len(result.unchanged)}")
    if result.failed:
        lines += ["", "**Failed:**"]
        lines += [f"- `{key}` — {reason}" for key, reason in list(result.failed.items())[:25]]
    if result.created_key:
        lines += ["", f"**New key:** `{result.created_key}`"]
    if result.version is not None:
        lines.append(f"*Version now {result.version}.*")
    return "\n".join(lines).rstrip()


def render_duplicates(groups: list[DuplicateGroup], *, heading: str) -> str:
    if not groups:
        return f"# {heading}\n\nNo duplicates found."
    lines = [f"# {heading}", "", f"*{len(groups)} group"
             f"{'s' if len(groups) != 1 else ''}*", ""]
    for index, group in enumerate(groups, start=1):
        lines.append(f"### Group {index} — {group.reason} ({group.confidence:.0%} confidence)")
        for item in group.items:
            master = " ← suggested master" if item.key == group.master_key else ""
            byline = f"{item.creator_summary}, " if item.creator_summary else ""
            lines.append(f"- `{item.key}` {byline}{item.year or 'n.d.'} — {item.title}{master}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_stats(stats: LibraryStats) -> str:
    lines = ["# Library overview", ""]
    if stats.library:
        name = stats.library.name or stats.library.library_id
        lines += [f"**Library:** {name} ({stats.library.library_type})", ""]
    lines += [
        f"- **Items:** {stats.total_items:,}",
        f"- **Collections:** {stats.collection_count:,}",
        f"- **Tags:** {stats.tag_count:,}",
        f"- **Attachments:** {stats.attachment_count:,}",
        f"- **Notes:** {stats.note_count:,}",
    ]
    if stats.items_with_pdf is not None:
        lines.append(f"- **With a PDF:** {stats.items_with_pdf:,}")
    if stats.items_without_collection is not None:
        lines.append(f"- **Uncollected:** {stats.items_without_collection:,}")
    if stats.oldest_year and stats.newest_year:
        lines.append(f"- **Span:** {stats.oldest_year}–{stats.newest_year}")
    if stats.by_item_type:
        lines += ["", "## By type", ""]
        ranked = sorted(stats.by_item_type.items(), key=lambda kv: -kv[1])
        lines += [f"- {name}: {count:,}" for name, count in ranked]
    return "\n".join(lines)


def render_health(report: HealthReport) -> str:
    status = "reachable" if report.reachable else "**not reachable**"
    lines = ["# Server status", "",
             f"- **Backend:** {report.backend} ({status})"]
    if report.library:
        name = report.library.name or report.library.library_id
        lines.append(f"- **Library:** {name} ({report.library.library_type})")
    lines.append(f"- **Zotero schema:** v{report.schema_version}")
    lines.append(f"- **Semantic index:** {report.semantic_index}"
                 + (f" ({report.indexed_items:,} items)" if report.indexed_items else ""))
    if report.optional_features:
        lines += ["", "## Optional features", ""]
        lines += [f"- {name}: {'available' if ok else 'not installed'}"
                  for name, ok in sorted(report.optional_features.items())]
    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in report.warnings]
    return "\n".join(lines)


__all__ = [
    "creator_summary",
    "render_annotations",
    "render_collections",
    "render_content_chunk",
    "render_duplicates",
    "render_health",
    "render_item_detail",
    "render_notes",
    "render_outline",
    "render_result_page",
    "render_stats",
    "render_tags",
    "render_write_result",
]
