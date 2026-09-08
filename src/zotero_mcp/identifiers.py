"""Recognising and normalising scholarly identifiers.

Stdlib only, and imports nothing else from this package, so it stays cheap to
import from anywhere (the CLI, a worker process, a downstream consumer) and
can be unit-tested without a Zotero client, network, or optional extras.

The purpose is to collapse the many shapes a user or an LLM will hand us —
``https://doi.org/10.1/x``, ``doi:10.1/x``, ``arXiv:2301.00001v2``,
``PMC1234``, an ISBN with hyphens — onto one canonical form per identifier
type, and to *classify* a bare string so a single ``add`` tool can accept any
of them without the caller having to say which it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "IdentifierKind",
    "ParsedIdentifier",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_isbn",
    "normalize_pmcid",
    "normalize_pmid",
    "parse_identifier",
]


class IdentifierKind(str, Enum):
    """The kind of scholarly identifier a string was recognised as."""

    DOI = "doi"
    ARXIV = "arxiv"
    ISBN = "isbn"
    PMID = "pmid"
    PMCID = "pmcid"
    ZOTERO_KEY = "zotero_key"
    URL = "url"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedIdentifier:
    """A classified identifier plus its canonical form.

    ``value`` is always the normalised form (bare DOI, bare arXiv id, ...),
    never the URL or prefixed shape the caller supplied.
    """

    kind: IdentifierKind
    value: str
    raw: str


# --- DOI --------------------------------------------------------------------
# Registrant codes are numeric and at least two digits; the suffix is opaque
# and may contain almost anything, so it is only bounded by whitespace.
_DOI_CORE = r"10\.\d{4,9}/[-._;()/:a-z0-9<>\[\]+]+"
_DOI_RE = re.compile(_DOI_CORE, re.IGNORECASE)
_DOI_STRIP_PREFIX = re.compile(
    r"^\s*(?:https?://(?:dx\.)?doi\.org/|doi:\s*|info:doi/)",
    re.IGNORECASE,
)
# Punctuation that routinely rides along when a DOI is copied out of prose or
# a citation, but is never part of the DOI itself.
_DOI_TRAILING = ".,;:)]}>'\"​"


def normalize_doi(value: str | None) -> str | None:
    """Return the bare, lower-cased DOI in *value*, or ``None``.

    Accepts a bare DOI, a ``doi:`` prefix, or a doi.org/dx.doi.org URL, and
    tolerates a DOI embedded in surrounding text. Trailing sentence
    punctuation is stripped — a DOI copied from a reference list almost always
    arrives with a full stop attached.
    """
    if not value:
        return None
    text = _DOI_STRIP_PREFIX.sub("", str(value).strip())
    match = _DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(_DOI_TRAILING)
    return doi.lower() or None


# --- arXiv ------------------------------------------------------------------
# Post-2007 identifiers are YYMM.NNNNN (4 or 5 digits) with an optional
# version. Pre-2007 ones are archive/subject-class plus 7 digits.
_ARXIV_NEW = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_ARXIV_OLD = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.IGNORECASE)


def normalize_arxiv_id(value: str | None, *, keep_version: bool = False) -> str | None:
    """Return the bare arXiv identifier in *value*, or ``None``.

    Handles ``arXiv:2301.00001v2``, ``https://arxiv.org/abs/2301.00001``,
    ``.../pdf/2301.00001v2.pdf`` and both the modern ``YYMM.NNNNN`` and legacy
    ``hep-th/9901001`` schemes.

    Args:
        value: The string to inspect.
        keep_version: Keep a trailing ``vN``. Off by default: the version is
            usually noise for metadata lookup, but matters when fetching a
            specific PDF.
    """
    if not value:
        return None
    text = str(value).strip()
    text = re.sub(r"^\s*arxiv:\s*", "", text, flags=re.IGNORECASE)
    # Drop a trailing .pdf so /pdf/2301.00001v2.pdf does not confuse the
    # new-style pattern by leaving ".pdf" adjacent to the digits.
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)

    for pattern in (_ARXIV_NEW, _ARXIV_OLD):
        match = pattern.search(text)
        if match:
            base = match.group(1)
            version = match.group(2) or ""
            return f"{base}{version}" if keep_version and version else base
    return None


# --- ISBN -------------------------------------------------------------------
_ISBN_CLEAN = re.compile(r"[^0-9xX]")


def _isbn10_valid(digits: str) -> bool:
    total = sum((10 - i) * (10 if c in "xX" else int(c)) for i, c in enumerate(digits))
    return total % 11 == 0


def _isbn13_valid(digits: str) -> bool:
    total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits))
    return total % 10 == 0


def normalize_isbn(value: str | None) -> str | None:
    """Return a hyphen-free ISBN-10/13 if *value* is one and its checksum passes.

    The checksum is verified rather than merely counting digits: a 13-digit
    run in a URL or an accession number is otherwise indistinguishable from an
    ISBN, and mis-classifying one sends the caller down a book-metadata path
    that cannot succeed.
    """
    if not value:
        return None
    digits = _ISBN_CLEAN.sub("", str(value))
    if len(digits) == 10 and _isbn10_valid(digits):
        return digits.upper()
    if len(digits) == 13 and digits.isdigit() and _isbn13_valid(digits):
        return digits
    return None


# --- PubMed -----------------------------------------------------------------
_PMID_RE = re.compile(
    r"(?:pmid:?\s*|pubmed(?:\.ncbi\.nlm\.nih\.gov)?/)(\d{1,9})(?!\d)",
    re.IGNORECASE,
)
_PMCID_RE = re.compile(r"\bPMC(\d{5,9})\b", re.IGNORECASE)


def normalize_pmid(value: str | None) -> str | None:
    """Return a bare PubMed ID, or ``None``.

    A prefix (``PMID:`` or a ``pubmed/`` URL path) is required: a bare run of
    digits is far more likely to be a year, a page number or an item count
    than a PMID, and guessing wrong is worse than declining.
    """
    if not value:
        return None
    match = _PMID_RE.search(str(value))
    return match.group(1) if match else None


def normalize_pmcid(value: str | None) -> str | None:
    """Return a canonical ``PMC…`` identifier, or ``None``."""
    if not value:
        return None
    match = _PMCID_RE.search(str(value))
    return f"PMC{match.group(1)}" if match else None


# --- Zotero keys ------------------------------------------------------------
# Zotero object keys are exactly 8 characters from a base-32 alphabet that
# omits the visually ambiguous I, L, O and U.
_ZOTERO_KEY_RE = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$")


def is_zotero_key(value: str | None) -> bool:
    """True if *value* has the exact shape of a Zotero item/collection key."""
    return bool(value) and bool(_ZOTERO_KEY_RE.match(str(value).strip()))


# --- Classification ---------------------------------------------------------
def parse_identifier(value: str) -> ParsedIdentifier:
    """Classify *value* and return it in canonical form.

    Ordering matters and is deliberate. A DOI is checked first because DOIs
    appear *inside* many URLs and are the more specific reading. arXiv beats
    the generic URL case for the same reason. The Zotero-key check runs before
    the URL fallback but after the identifier schemes, since its 8-character
    pattern is narrow enough to be unambiguous.
    """
    raw = (value or "").strip()
    if not raw:
        return ParsedIdentifier(IdentifierKind.UNKNOWN, "", raw)

    if doi := normalize_doi(raw):
        return ParsedIdentifier(IdentifierKind.DOI, doi, raw)
    if arxiv := normalize_arxiv_id(raw):
        return ParsedIdentifier(IdentifierKind.ARXIV, arxiv, raw)
    if pmcid := normalize_pmcid(raw):
        return ParsedIdentifier(IdentifierKind.PMCID, pmcid, raw)
    if pmid := normalize_pmid(raw):
        return ParsedIdentifier(IdentifierKind.PMID, pmid, raw)
    if isbn := normalize_isbn(raw):
        return ParsedIdentifier(IdentifierKind.ISBN, isbn, raw)
    if is_zotero_key(raw):
        return ParsedIdentifier(IdentifierKind.ZOTERO_KEY, raw.upper(), raw)
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return ParsedIdentifier(IdentifierKind.URL, raw, raw)
    return ParsedIdentifier(IdentifierKind.UNKNOWN, raw, raw)
