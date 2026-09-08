# Copyright 2026 Vinay

"""Turning an identifier into a Zotero item.

One function per source, each returning Zotero item ``data`` or None, so
:func:`fetch` can try them in an order that reflects which source actually
knows about which identifier. Every mapping is field-by-field and deliberate:
Crossref's ``container-title`` is Zotero's ``publicationTitle``, its
``type`` is a Zotero item type through an explicit table, and its dates are
part-arrays rather than strings.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from zotero_mcp.config import NetworkSettings
from zotero_mcp.errors import NotFound
from zotero_mcp.external import http
from zotero_mcp.identifiers import IdentifierKind, ParsedIdentifier

logger = logging.getLogger(__name__)

#: Crossref work types onto Zotero item types. Anything unlisted becomes a
#: document, which is Zotero's own catch-all and keeps every field placeable.
CROSSREF_TYPES = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book": "book",
    "book-chapter": "bookSection",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "reference-entry": "dictionaryEntry",
    "dissertation": "thesis",
    "report": "report",
    "posted-content": "preprint",
    "dataset": "dataset",
    "component": "document",
    "standard": "standard",
    "peer-review": "document",
}


def _creators(authors: list[dict[str, Any]], role: str = "author") -> list[dict[str, str]]:
    creators = []
    for author in authors or []:
        if family := author.get("family"):
            creators.append(
                {"creatorType": role, "firstName": author.get("given", ""), "lastName": family}
            )
        elif name := author.get("name"):
            # Institutional authors have no given/family split, and sending
            # empty ones makes Zotero reject the whole item.
            creators.append({"creatorType": role, "name": name})
    return creators


def _crossref_date(parts: dict[str, Any] | None) -> str:
    """Crossref dates are arrays of parts, not strings."""
    if not parts:
        return ""
    values = (parts.get("date-parts") or [[]])[0]
    return "-".join(f"{int(v):02d}" if i else str(v) for i, v in enumerate(values) if v)


def from_crossref(doi: str, settings: NetworkSettings) -> dict[str, Any] | None:
    """Metadata for a DOI, from Crossref."""
    payload = http.get(f"https://api.crossref.org/works/{doi}", settings=settings)
    if not payload:
        return None
    work = payload.get("message") or {}

    titles = work.get("title") or []
    containers = work.get("container-title") or []
    item_type = CROSSREF_TYPES.get(work.get("type", ""), "document")

    data: dict[str, Any] = {
        "itemType": item_type,
        "title": titles[0] if titles else "Untitled",
        "creators": _creators(work.get("author") or [])
        + _creators(work.get("editor") or [], "editor"),
        "date": _crossref_date(work.get("issued")) or _crossref_date(work.get("published")),
        "DOI": doi,
        "url": work.get("URL", ""),
        "abstractNote": _clean_jats(work.get("abstract", "")),
        "language": work.get("language", ""),
        "extra": "",
    }

    if containers:
        field = "proceedingsTitle" if item_type == "conferencePaper" else "publicationTitle"
        data[field] = containers[0]
    for source, target in (
        ("volume", "volume"),
        ("issue", "issue"),
        ("page", "pages"),
        ("publisher", "publisher"),
        ("ISSN", "ISSN"),
    ):
        value = work.get(source)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            data[target] = str(value)
    if isbns := work.get("ISBN"):
        data["ISBN"] = isbns[0] if isinstance(isbns, list) else str(isbns)

    return _placeable(data)


_JATS = re.compile(r"</?jats:[^>]+>")


def _clean_jats(abstract: str) -> str:
    """Crossref abstracts arrive as JATS XML fragments."""
    if not abstract:
        return ""
    import html as html_module

    text = _JATS.sub("", abstract)
    text = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(text).strip()


def from_arxiv(arxiv_id: str, settings: NetworkSettings) -> dict[str, Any] | None:
    """Metadata for an arXiv identifier, from the arXiv API."""
    import xml.etree.ElementTree as ET

    raw = http.get(
        "https://export.arxiv.org/api/query",
        settings=settings,
        params={"id_list": arxiv_id, "max_results": 1},
        accept="application/atom+xml",
    )
    if not raw or not isinstance(raw, str):
        return None

    namespace = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        entry = ET.fromstring(raw).find("atom:entry", namespace)
    except ET.ParseError:
        return None
    if entry is None:
        return None

    def text(path: str) -> str:
        node = entry.find(path, namespace)
        return (node.text or "").strip() if node is not None and node.text else ""

    authors = [
        {"creatorType": "author", **_split_name(node.text or "")}
        for node in entry.findall("atom:author/atom:name", namespace)
        if node.text
    ]

    doi = text("arxiv:doi")
    data = {
        "itemType": "preprint",
        "title": " ".join(text("atom:title").split()),
        "creators": authors,
        "abstractNote": " ".join(text("atom:summary").split()),
        "date": text("atom:published")[:10],
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "repository": "arXiv",
        "archiveID": f"arXiv:{arxiv_id}",
        "DOI": doi,
    }
    return _placeable(data)


def _split_name(name: str) -> dict[str, str]:
    parts = name.strip().split()
    if len(parts) == 1:
        return {"name": parts[0]}
    return {"firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def from_isbn(isbn: str, settings: NetworkSettings) -> dict[str, Any] | None:
    """Book metadata from Open Library."""
    payload = http.get(
        "https://openlibrary.org/api/books",
        settings=settings,
        params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
    )
    record = (payload or {}).get(f"ISBN:{isbn}")
    if not record:
        return None

    data = {
        "itemType": "book",
        "title": record.get("title", "Untitled"),
        "creators": [
            {"creatorType": "author", **_split_name(author.get("name", ""))}
            for author in record.get("authors") or []
            if author.get("name")
        ],
        "date": str(record.get("publish_date", "")),
        "publisher": (record.get("publishers") or [{}])[0].get("name", ""),
        "place": (record.get("publish_places") or [{}])[0].get("name", ""),
        "numPages": str(record.get("number_of_pages", "") or ""),
        "ISBN": isbn,
        "url": record.get("url", ""),
    }
    return _placeable(data)


def from_pubmed(pmid: str, settings: NetworkSettings) -> dict[str, Any] | None:
    """Article metadata from NCBI's summary endpoint."""
    payload = http.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        settings=settings,
        params={"db": "pubmed", "id": pmid, "retmode": "json"},
    )
    record = ((payload or {}).get("result") or {}).get(pmid)
    if not record or record.get("error"):
        return None

    doi = next(
        (a.get("value") for a in record.get("articleids") or [] if a.get("idtype") == "doi"),
        "",
    )
    data = {
        "itemType": "journalArticle",
        "title": record.get("title", "Untitled").rstrip("."),
        "creators": [
            {"creatorType": "author", **_split_name(author.get("name", ""))}
            for author in record.get("authors") or []
            if author.get("name")
        ],
        "publicationTitle": record.get("fulljournalname") or record.get("source", ""),
        "date": record.get("pubdate", ""),
        "volume": record.get("volume", ""),
        "issue": record.get("issue", ""),
        "pages": record.get("pages", ""),
        "DOI": doi,
        "extra": f"PMID: {pmid}",
    }
    return _placeable(data)


_META_TAG = re.compile(
    r"""<meta\s+[^>]*?(?:name|property)\s*=\s*["']([^"']+)["'][^>]*?content\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)
_META_TAG_REVERSED = re.compile(
    r"""<meta\s+[^>]*?content\s*=\s*["']([^"']*)["'][^>]*?(?:name|property)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def from_webpage(url: str, settings: NetworkSettings) -> dict[str, Any] | None:
    """Metadata scraped from a page's own tags.

    Publishers embed Highwire ``citation_*`` tags, which are far richer than
    OpenGraph and are what makes "add this URL" produce a real reference rather
    than a bare webpage entry. If the page names a DOI, the caller is better
    served by Crossref, so that is reported back rather than used here.
    """
    html = http.get(url, settings=settings, accept="text/html")
    if not isinstance(html, str):
        return None

    tags: dict[str, list[str]] = {}
    for pattern, order in ((_META_TAG, (0, 1)), (_META_TAG_REVERSED, (1, 0))):
        for match in pattern.finditer(html):
            groups = match.groups()
            name = groups[order[0]].strip().lower()
            content = groups[order[1]].strip()
            if content:
                tags.setdefault(name, []).append(content)

    def first(*names: str) -> str:
        for name in names:
            if values := tags.get(name):
                return values[0]
        return ""

    if doi := first("citation_doi", "dc.identifier.doi"):
        from zotero_mcp.identifiers import normalize_doi

        if normalised := normalize_doi(doi):
            return {"_redirect_doi": normalised}

    title = first("citation_title", "og:title", "dc.title", "twitter:title")
    if not title:
        match = _TITLE_TAG.search(html)
        title = " ".join(match.group(1).split()) if match else url

    authors = tags.get("citation_author") or tags.get("dc.creator") or []
    is_article = bool(tags.get("citation_title"))

    data = {
        "itemType": "journalArticle" if is_article else "webpage",
        "title": title,
        "creators": [
            {"creatorType": "author", **_split_name(_flip_name(author))} for author in authors
        ],
        "abstractNote": first("citation_abstract", "og:description", "description"),
        "date": first(
            "citation_publication_date", "citation_date", "article:published_time", "dc.date"
        )[:10],
        "url": first("og:url", "citation_public_url") or url,
        "accessDate": _today(),
    }
    if is_article:
        data["publicationTitle"] = first("citation_journal_title")
        data["volume"] = first("citation_volume")
        data["issue"] = first("citation_issue")
        first_page = first("citation_firstpage")
        last_page = first("citation_lastpage")
        data["pages"] = f"{first_page}-{last_page}" if first_page and last_page else first_page
        data["ISSN"] = first("citation_issn")
    else:
        data["websiteTitle"] = first("og:site_name")

    return _placeable(data)


def _flip_name(name: str) -> str:
    """Highwire authors are 'Family, Given'; everything else is 'Given Family'."""
    if "," in name:
        family, _, given = name.partition(",")
        return f"{given.strip()} {family.strip()}".strip()
    return name


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


#: Keys Zotero defines on every item, outside the per-type field table.
_STRUCTURAL = frozenset({"itemType", "creators", "tags", "collections", "relations"})


def _placeable(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values, and route every field onto the item type's real name.

    Without the routing step a preprint's ``repository`` or a conference
    paper's ``proceedingsTitle`` is silently discarded by Zotero, which is the
    class of bug the schema module exists to prevent.
    """
    from zotero_mcp import schema

    populated = {k: v for k, v in data.items() if v not in (None, "", [], {})}
    item_type = populated.get("itemType", "document")

    # The structural keys live outside the schema's field table, so routing
    # them through it would drop every author on the item.
    structural = {k: v for k, v in populated.items() if k in _STRUCTURAL}
    fields = {k: v for k, v in populated.items() if k not in _STRUCTURAL}

    resolved, unplaceable = schema.resolve_fields(item_type, fields)
    if unplaceable:
        logger.debug("Dropping fields %s: not valid for %s", unplaceable, item_type)
    return {**structural, **resolved}


def fetch(parsed: ParsedIdentifier, settings: NetworkSettings) -> dict[str, Any]:
    """Metadata for a parsed identifier, from whichever source knows it.

    Raises:
        NotFound: No source recognised the identifier.
    """
    if parsed.kind is IdentifierKind.DOI:
        if data := from_crossref(parsed.value, settings):
            return data
        raise NotFound(
            f"Crossref has no record for DOI {parsed.value}.",
            hint="Check the DOI, or add the item by URL instead.",
        )

    if parsed.kind is IdentifierKind.ARXIV:
        if data := from_arxiv(parsed.value, settings):
            return data
        raise NotFound(f"arXiv has no record for {parsed.value}.")

    if parsed.kind is IdentifierKind.ISBN:
        if data := from_isbn(parsed.value, settings):
            return data
        raise NotFound(f"Open Library has no record for ISBN {parsed.value}.")

    if parsed.kind is IdentifierKind.PMID:
        if data := from_pubmed(parsed.value, settings):
            return data
        raise NotFound(f"PubMed has no record for PMID {parsed.value}.")

    if parsed.kind is IdentifierKind.PMCID:
        if data := from_pubmed(parsed.value.removeprefix("PMC"), settings):
            return data
        raise NotFound(f"PubMed has no record for {parsed.value}.")

    if parsed.kind is IdentifierKind.URL:
        data = from_webpage(parsed.value, settings)
        if data and (doi := data.get("_redirect_doi")):
            # The page told us its DOI, and Crossref knows more than its tags do.
            if better := from_crossref(doi, settings):
                return better
            raise NotFound(f"The page names DOI {doi}, but Crossref has no record for it.")
        if data:
            return data
        raise NotFound(f"Could not read metadata from {parsed.value}.")

    raise NotFound(
        f"{parsed.raw!r} is not an identifier this server recognises.",
        hint="Pass a DOI, arXiv id, ISBN, PMID, PMCID or URL.",
    )


__all__ = [
    "CROSSREF_TYPES",
    "fetch",
    "from_arxiv",
    "from_crossref",
    "from_isbn",
    "from_pubmed",
    "from_webpage",
]
