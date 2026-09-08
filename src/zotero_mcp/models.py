"""The structured half of every tool result.

A tool here returns two things: markdown for the human in the loop, and one of
these models as structured content for the model driving the conversation.

That second half is the point. The predecessor returned markdown only, so a
caller that wanted an item key — the join key for literally every follow-up
call — had to find it inside prose and hope the formatting had not changed.
Models re-derive those keys wrongly often enough that "the assistant made up a
Zotero key" is a recognisable failure mode. Handing back typed data removes the
guesswork, and gives MCP clients a real output schema to validate against.

Every model is deliberately shallow and JSON-clean: no Zotero response objects
leak through, and nothing here needs the Zotero client to construct.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Base: reject unknown fields so a typo in a constructor call fails loudly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class Creator(_Model):
    """One author, editor, translator or similar."""

    creator_type: str = "author"
    first_name: str | None = None
    last_name: str | None = None
    #: Institutional creators are stored as a single unsplit name.
    name: str | None = None

    @property
    def display(self) -> str:
        if self.name:
            return self.name
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or "Unknown"

    @classmethod
    def from_zotero(cls, raw: dict[str, Any]) -> Creator:
        return cls(
            creator_type=raw.get("creatorType", "author"),
            first_name=raw.get("firstName"),
            last_name=raw.get("lastName"),
            name=raw.get("name"),
        )

    def to_zotero(self) -> dict[str, str]:
        """Back to Zotero's shape, which is a two-field *or* one-field record.

        Sending both shapes at once makes Zotero reject the whole write, so
        the single-field form wins whenever a bare ``name`` is present.
        """
        if self.name:
            return {"creatorType": self.creator_type, "name": self.name}
        return {
            "creatorType": self.creator_type,
            "firstName": self.first_name or "",
            "lastName": self.last_name or "",
        }


class LibraryRef(_Model):
    """Which library an object lives in."""

    library_id: str
    library_type: Literal["user", "group"] = "user"
    name: str | None = None


class CollectionRef(_Model):
    """A collection, with enough context to navigate the hierarchy."""

    key: str
    name: str
    parent_key: str | None = None
    #: Slash-joined path from the root, when the caller asked for a tree.
    path: str | None = None
    item_count: int | None = None
    subcollection_count: int | None = None


class TagCount(_Model):
    """A tag and how many items carry it."""

    tag: str
    count: int | None = None
    #: Zotero type 0 is a manually added tag, 1 an automatic one from an import.
    automatic: bool = False


class AttachmentRef(_Model):
    """A child attachment, and whether its bytes are actually reachable."""

    key: str
    title: str
    content_type: str | None = None
    filename: str | None = None
    link_mode: str | None = None
    #: True when the file is present locally or downloadable. False for a
    #: linked file whose path no longer resolves — a common and otherwise
    #: baffling reason that reading a paper fails.
    available: bool | None = None
    page_count: int | None = None


class ItemRef(_Model):
    """The minimum needed to name an item in a list or a follow-up call."""

    key: str
    title: str
    item_type: str
    year: str | None = None
    creator_summary: str | None = None


class ItemSummary(_Model):
    """A search hit or list row: identity, provenance and a short preview."""

    key: str
    title: str
    item_type: str
    version: int | None = None
    date: str | None = None
    year: str | None = None
    creators: list[Creator] = Field(default_factory=list)
    creator_summary: str | None = None
    publication: str | None = None
    doi: str | None = None
    url: str | None = None
    citation_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    abstract_preview: str | None = None
    num_children: int | None = None
    has_pdf: bool | None = None
    #: Populated by semantic search only.
    score: float | None = None
    matched_text: str | None = None
    #: Deep link that opens the item in the Zotero desktop client.
    zotero_uri: str | None = None


class ItemDetail(_Model):
    """Everything known about one item.

    ``fields`` carries the item's own Zotero fields verbatim under their actual
    names, so nothing is lost to a formatter that did not anticipate a type.
    """

    key: str
    title: str
    item_type: str
    version: int | None = None
    library: LibraryRef | None = None
    date: str | None = None
    creators: list[Creator] = Field(default_factory=list)
    abstract: str | None = None
    doi: str | None = None
    url: str | None = None
    citation_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    collections: list[CollectionRef] = Field(default_factory=list)
    related_keys: list[str] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    note_count: int = 0
    annotation_count: int | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    date_added: str | None = None
    date_modified: str | None = None
    zotero_uri: str | None = None


class Annotation(_Model):
    """A PDF or EPUB annotation."""

    key: str
    parent_key: str
    #: Highlight, note, image, ink, underline or text.
    annotation_type: str
    text: str | None = None
    comment: str | None = None
    color: str | None = None
    page_label: str | None = None
    #: Zotero's 1-based index into the PDF, which need not match page_label.
    page_index: int | None = None
    sort_index: str | None = None
    tags: list[str] = Field(default_factory=list)
    date_modified: str | None = None
    #: Set when the annotation was extracted from the file rather than read
    #: from Zotero's own database — the two can disagree.
    source: Literal["zotero", "extracted"] = "zotero"


class Note(_Model):
    """A standalone or child note. ``text`` is HTML stripped to plain text."""

    key: str
    parent_key: str | None = None
    title: str | None = None
    text: str
    html: str | None = None
    tags: list[str] = Field(default_factory=list)
    date_modified: str | None = None


class ContentChunk(_Model):
    """A ranged read out of a document.

    Always carries the range it covers and whether more exists, so the caller
    can continue instead of re-requesting the whole document.
    """

    item_key: str
    attachment_key: str | None = None
    title: str | None = None
    text: str
    #: 1-based inclusive page range this chunk covers.
    first_page: int | None = None
    last_page: int | None = None
    total_pages: int | None = None
    has_more: bool = False
    next_pages: str | None = None
    truncated: bool = False
    source: str | None = None
    chars: int = 0


class OutlineEntry(_Model):
    """One heading in a document's table of contents."""

    level: int
    title: str
    page: int | None = None


class SearchDiagnostics(_Model):
    """Why a search returned what it did.

    Search over a personal library is substring matching with a fallback
    cascade, and it fails in ways the caller can act on — too many words, a
    diacritic mismatch, an empty collection. Reporting the strategy that
    actually produced the results turns a silent empty list into a fixable one.
    """

    strategy: str
    #: Every strategy attempted, in order, with its result count.
    attempts: list[str] = Field(default_factory=list)
    query_variants: list[str] = Field(default_factory=list)
    elapsed_seconds: float | None = None
    timed_out: bool = False
    suggestion: str | None = None


class ResultPage(_Model):
    """A page of item summaries, plus how to get the next one."""

    items: list[ItemSummary] = Field(default_factory=list)
    total: int | None = None
    offset: int = 0
    returned: int = 0
    next_cursor: str | None = None
    diagnostics: SearchDiagnostics | None = None


class CollectionPage(_Model):
    """A page of collections."""

    collections: list[CollectionRef] = Field(default_factory=list)
    total: int | None = None
    offset: int = 0
    returned: int = 0
    next_cursor: str | None = None


class Change(_Model):
    """One field-level difference a write would make or has made."""

    field: str
    before: Any = None
    after: Any = None


class WriteResult(_Model):
    """The outcome of a write, or the preview of one that was not performed.

    ``dry_run`` is part of the result rather than only the request so that a
    preview can never be mistaken for a completed write — the most dangerous
    ambiguity in a library-mutating tool.
    """

    action: str
    dry_run: bool = False
    succeeded: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)
    unchanged: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    #: Version of the primary object after the write, for optimistic locking.
    version: int | None = None
    created_key: str | None = None
    message: str | None = None

    @property
    def total_affected(self) -> int:
        return len(self.succeeded)


class DuplicateGroup(_Model):
    """A set of items judged to be the same work."""

    items: list[ItemRef] = Field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    master_key: str | None = None


class LibraryStats(_Model):
    """A quantitative overview of the active library."""

    library: LibraryRef | None = None
    total_items: int = 0
    by_item_type: dict[str, int] = Field(default_factory=dict)
    collection_count: int = 0
    tag_count: int = 0
    attachment_count: int = 0
    note_count: int = 0
    items_without_collection: int | None = None
    items_with_pdf: int | None = None
    oldest_year: str | None = None
    newest_year: str | None = None


class HealthReport(_Model):
    """Whether the server can actually reach everything it claims to offer."""

    backend: str
    reachable: bool
    library: LibraryRef | None = None
    schema_version: int = 0
    semantic_index: str = "unavailable"
    indexed_items: int | None = None
    optional_features: dict[str, bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "Annotation",
    "AttachmentRef",
    "Change",
    "CollectionPage",
    "CollectionRef",
    "ContentChunk",
    "Creator",
    "DuplicateGroup",
    "HealthReport",
    "ItemDetail",
    "ItemRef",
    "ItemSummary",
    "LibraryRef",
    "LibraryStats",
    "Note",
    "OutlineEntry",
    "ResultPage",
    "SearchDiagnostics",
    "TagCount",
    "WriteResult",
]
