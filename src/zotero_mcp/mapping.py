"""Turn raw Zotero API payloads into this package's models.

One module, one direction, no side effects — nothing here opens a connection or
reads a file, so every mapping rule is unit-testable against a literal dict.

The rules encoded here are the ones the predecessors got subtly wrong and had
to keep re-fixing: where a citation key actually lives, that ``dc:relation``
can be a bare string as well as a list, that a "year" is not the first four
characters of a date, and that HTML stripping has to survive the entity soup
Zotero's note editor produces.
"""

from __future__ import annotations

import html as html_module
import re
from typing import Any

from zotero_mcp import schema
from zotero_mcp.models import (
    Annotation,
    AttachmentRef,
    CollectionRef,
    Creator,
    ItemDetail,
    ItemRef,
    ItemSummary,
    LibraryRef,
    Note,
    TagCount,
)

# Better BibTeX writes its key into the Extra field. Zotero 7 also has a real
# `citationKey` field; both are checked, the native field winning.
_CITATION_KEY_RE = re.compile(
    r"^\s*(?:citation\s*key|citekey)\s*[:=]\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A year anywhere in the date string. Zotero dates are free text and arrive as
# "2017", "2017-06", "June 2017", "2017-06-12", "n.d." and worse, so taking
# date[:4] — which both predecessors do — yields "June" or "n.d." surprisingly
# often.
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b")

_BLOCK_END_RE = re.compile(r"(?i)</(?:p|div|li|h[1-6]|tr|blockquote)\s*>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html(value: str | None) -> str:
    """Flatten Zotero note HTML to readable plain text.

    Block ends become newlines before tags are removed, so paragraphs do not
    run together into one wall of text — which is what a naive tag-strip
    produces and why notes were previously unreadable in tool output.
    """
    if not value:
        return ""
    text = _BR_RE.sub("\n", value)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def year_of(date: str | None) -> str | None:
    """Extract a four-digit year from a free-text Zotero date."""
    if not date:
        return None
    match = _YEAR_RE.search(str(date))
    return match.group(1) if match else None


def citation_key_of(data: dict[str, Any]) -> str | None:
    """Find the Better BibTeX / Zotero 7 citation key for an item."""
    if native := data.get("citationKey"):
        return str(native).strip() or None
    match = _CITATION_KEY_RE.search(data.get("extra") or "")
    return match.group(1) if match else None


def related_keys_of(data: dict[str, Any]) -> list[str]:
    """Item keys from ``dc:relation``.

    Zotero serialises a single relation as a bare string and multiple ones as a
    list. Treating the string case as an iterable yields one "key" per
    character, which is exactly the bug that made related-item lookups return
    nonsense.
    """
    relations = (data.get("relations") or {}).get("dc:relation", [])
    if isinstance(relations, str):
        relations = [relations]
    keys = []
    for uri in relations:
        if isinstance(uri, str) and (tail := uri.rstrip("/").rsplit("/", 1)[-1]):
            keys.append(tail)
    return keys


def tags_of(data: dict[str, Any]) -> list[str]:
    """Tag names, tolerating both the object and bare-string encodings."""
    out = []
    for tag in data.get("tags") or []:
        if isinstance(tag, dict):
            if name := tag.get("tag"):
                out.append(name)
        elif isinstance(tag, str):
            out.append(tag)
    return out


def creators_of(data: dict[str, Any]) -> list[Creator]:
    return [Creator.from_zotero(c) for c in data.get("creators") or [] if isinstance(c, dict)]


def publication_of(data: dict[str, Any], item_type: str) -> str | None:
    """The 'where was this published' string, whatever the type calls it.

    Resolved through the schema's base fields rather than a hand-written list,
    so conference papers, theses, statutes and datasets all answer correctly.
    """
    for field in (
        "publicationTitle",
        "proceedingsTitle",
        "bookTitle",
        "repository",
        "publisher",
        "university",
        "institution",
        "websiteTitle",
        "blogTitle",
    ):
        actual = schema.resolve_field(item_type, field) or field
        if value := data.get(actual):
            return str(value)
    return None


def zotero_uri(key: str, library: LibraryRef | None = None) -> str:
    """A ``zotero://`` deep link that opens the object in the desktop client."""
    if library and library.library_type == "group":
        return f"zotero://select/groups/{library.library_id}/items/{key}"
    return f"zotero://select/library/items/{key}"


def _unwrap(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a Zotero object into (data, meta), tolerating an unwrapped dict.

    The Web API returns ``{"key":…, "data": {...}, "meta": {...}}`` while some
    local paths hand back the data alone. Accepting both here means no caller
    has to guess which shape it has.
    """
    if "data" in raw and isinstance(raw["data"], dict):
        return raw["data"], raw.get("meta") or {}
    return raw, raw.get("meta") or {}


def to_item_summary(
    raw: dict[str, Any],
    *,
    abstract_chars: int = 320,
    library: LibraryRef | None = None,
    score: float | None = None,
    matched_text: str | None = None,
    has_pdf: bool | None = None,
) -> ItemSummary:
    """Map one item to a list row."""
    data, meta = _unwrap(raw)
    item_type = data.get("itemType", "document")
    creators = creators_of(data)

    abstract = (data.get("abstractNote") or "").strip()
    if abstract and len(abstract) > abstract_chars:
        abstract = abstract[:abstract_chars].rstrip() + "…"

    key = raw.get("key") or data.get("key") or ""
    return ItemSummary(
        key=key,
        title=data.get("title") or _fallback_title(data, item_type),
        item_type=item_type,
        version=raw.get("version") or data.get("version"),
        date=data.get("date"),
        year=year_of(data.get("date")),
        creators=creators,
        creator_summary=meta.get("creatorSummary") or _creator_summary(creators),
        publication=publication_of(data, item_type),
        doi=data.get("DOI") or None,
        url=data.get("url") or None,
        citation_key=citation_key_of(data),
        tags=tags_of(data),
        collections=list(data.get("collections") or []),
        abstract_preview=abstract or None,
        num_children=meta.get("numChildren"),
        has_pdf=has_pdf,
        score=score,
        matched_text=matched_text,
        zotero_uri=zotero_uri(key, library) if key else None,
    )


def _creator_summary(creators: list[Creator]) -> str | None:
    """Local copy of the byline rule, so mapping does not import rendering."""
    names = [c.last_name or c.name for c in creators if (c.last_name or c.name)]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return f"{names[0]} et al."


def _fallback_title(data: dict[str, Any], item_type: str) -> str:
    """A title for types that store it under another name, or have none.

    Notes and attachments frequently have no title at all; showing "Untitled"
    is more useful than showing an empty string that renders as a blank row.
    """
    for candidate in ("title", "caseName", "nameOfAct", "subject"):
        if value := data.get(candidate):
            return str(value)
    actual = schema.resolve_field(item_type, "title")
    if actual and (value := data.get(actual)):
        return str(value)
    if item_type == "note":
        text = strip_html(data.get("note"))
        return (text.split("\n", 1)[0][:80] or "Untitled note") if text else "Untitled note"
    return "Untitled"


def to_attachment_ref(raw: dict[str, Any], *, available: bool | None = None) -> AttachmentRef:
    data, _ = _unwrap(raw)
    return AttachmentRef(
        key=raw.get("key") or data.get("key") or "",
        title=data.get("title") or data.get("filename") or "Untitled",
        content_type=data.get("contentType") or None,
        filename=data.get("filename") or None,
        link_mode=data.get("linkMode") or None,
        available=available,
    )


def to_item_detail(
    raw: dict[str, Any],
    *,
    library: LibraryRef | None = None,
    children: list[dict[str, Any]] | None = None,
    collections: list[CollectionRef] | None = None,
) -> ItemDetail:
    """Map one item to its full record, folding in children when supplied."""
    data, meta = _unwrap(raw)
    item_type = data.get("itemType", "document")
    key = raw.get("key") or data.get("key") or ""

    attachments: list[AttachmentRef] = []
    note_count = 0
    annotation_count: int | None = None
    for child in children or []:
        child_data, _ = _unwrap(child)
        child_type = child_data.get("itemType")
        if child_type == "attachment":
            attachments.append(to_attachment_ref(child))
        elif child_type == "note":
            note_count += 1
        elif child_type == "annotation":
            annotation_count = (annotation_count or 0) + 1

    # Fields Zotero manages itself are not item content and only add noise to
    # the rendered "Fields" section.
    reserved = {
        "key",
        "version",
        "itemType",
        "creators",
        "tags",
        "collections",
        "relations",
        "dateAdded",
        "dateModified",
    }
    fields = {k: v for k, v in data.items() if k not in reserved and v not in (None, "", [], {})}

    return ItemDetail(
        key=key,
        title=data.get("title") or _fallback_title(data, item_type),
        item_type=item_type,
        version=raw.get("version") or data.get("version"),
        library=library,
        date=data.get("date"),
        creators=creators_of(data),
        abstract=(data.get("abstractNote") or "").strip() or None,
        doi=data.get("DOI") or None,
        url=data.get("url") or None,
        citation_key=citation_key_of(data),
        tags=tags_of(data),
        collections=collections or [],
        related_keys=related_keys_of(data),
        attachments=attachments,
        note_count=note_count or (meta.get("numChildren", 0) if not children else 0),
        annotation_count=annotation_count,
        fields=fields,
        date_added=data.get("dateAdded"),
        date_modified=data.get("dateModified"),
        zotero_uri=zotero_uri(key, library) if key else None,
    )


def to_item_ref(raw: dict[str, Any]) -> ItemRef:
    data, meta = _unwrap(raw)
    item_type = data.get("itemType", "document")
    return ItemRef(
        key=raw.get("key") or data.get("key") or "",
        title=data.get("title") or _fallback_title(data, item_type),
        item_type=item_type,
        year=year_of(data.get("date")),
        creator_summary=meta.get("creatorSummary") or _creator_summary(creators_of(data)),
    )


def to_collection_ref(raw: dict[str, Any], *, path: str | None = None) -> CollectionRef:
    data, meta = _unwrap(raw)
    parent = data.get("parentCollection")
    return CollectionRef(
        key=raw.get("key") or data.get("key") or "",
        name=data.get("name") or "Untitled",
        # Zotero encodes "no parent" as boolean false, not as null.
        parent_key=parent if isinstance(parent, str) and parent else None,
        path=path,
        item_count=meta.get("numItems"),
        subcollection_count=meta.get("numCollections"),
    )


def to_annotation(raw: dict[str, Any], *, source: str = "zotero") -> Annotation:
    data, _ = _unwrap(raw)
    page_label = data.get("annotationPageLabel") or None
    position = data.get("annotationPosition")
    page_index = None
    if isinstance(position, dict):
        page_index = position.get("pageIndex")
    elif isinstance(position, str):
        # Older Zotero versions store the position as a JSON string.
        import json

        try:
            page_index = json.loads(position).get("pageIndex")
        except (ValueError, AttributeError):
            page_index = None
    return Annotation(
        key=raw.get("key") or data.get("key") or "",
        parent_key=data.get("parentItem") or "",
        annotation_type=data.get("annotationType") or "highlight",
        text=(data.get("annotationText") or "").strip() or None,
        comment=(data.get("annotationComment") or "").strip() or None,
        color=data.get("annotationColor") or None,
        page_label=page_label,
        # Zotero's pageIndex is 0-based; every human-facing page number is not.
        page_index=(page_index + 1) if isinstance(page_index, int) else None,
        sort_index=data.get("annotationSortIndex") or None,
        tags=tags_of(data),
        date_modified=data.get("dateModified"),
        source=source,  # type: ignore[arg-type]
    )


def to_note(raw: dict[str, Any]) -> Note:
    data, _ = _unwrap(raw)
    html = data.get("note") or ""
    text = strip_html(html)
    first_line = text.split("\n", 1)[0].strip() if text else ""
    parent = data.get("parentItem")
    return Note(
        key=raw.get("key") or data.get("key") or "",
        parent_key=parent if isinstance(parent, str) and parent else None,
        title=(first_line[:100] or None),
        text=text,
        html=html or None,
        tags=tags_of(data),
        date_modified=data.get("dateModified"),
    )


def to_tag_count(raw: dict[str, Any] | str) -> TagCount:
    if isinstance(raw, str):
        return TagCount(tag=raw)
    meta = raw.get("meta") or {}
    return TagCount(
        tag=raw.get("tag") or (raw.get("data") or {}).get("tag") or "",
        count=meta.get("numItems"),
        automatic=meta.get("type") == 1,
    )


def collection_paths(collections: list[dict[str, Any]]) -> dict[str, str]:
    """Build ``key -> "Parent/Child"`` paths for a full collection list.

    Cycles cannot occur in Zotero's data model but a corrupt or partial listing
    can produce one, so the walk is depth-bounded rather than trusting the
    input to terminate.
    """
    by_key = {c.get("key") or (c.get("data") or {}).get("key"): c for c in collections}
    names: dict[str, str] = {}
    parents: dict[str, str | None] = {}
    for key, raw in by_key.items():
        if not key:
            continue
        data, _ = _unwrap(raw)
        names[key] = data.get("name") or "Untitled"
        parent = data.get("parentCollection")
        parents[key] = parent if isinstance(parent, str) and parent else None

    paths: dict[str, str] = {}
    for key in names:
        segments = [names[key]]
        cursor = parents.get(key)
        depth = 0
        while cursor and cursor in names and depth < 32:
            segments.append(names[cursor])
            cursor = parents.get(cursor)
            depth += 1
        paths[key] = "/".join(reversed(segments))
    return paths


__all__ = [
    "citation_key_of",
    "collection_paths",
    "creators_of",
    "publication_of",
    "related_keys_of",
    "strip_html",
    "tags_of",
    "to_annotation",
    "to_attachment_ref",
    "to_collection_ref",
    "to_item_detail",
    "to_item_ref",
    "to_item_summary",
    "to_note",
    "to_tag_count",
    "year_of",
    "zotero_uri",
]
