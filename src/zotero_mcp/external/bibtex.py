# Copyright 2026 Vinay

"""BibTeX, CSL-JSON and RIS, generated locally.

Better BibTeX, when the Zotero plugin is installed and running, produces
better keys and better escaping than anything generated here, so it is asked
first. The local generator is the fallback, and it is a real implementation
rather than the stub the predecessors shipped: their fallback emitted keys like
``Smith2017_ABCD2345``, which is not a citation key anybody would put in a
manuscript, and escaped braces in a way that corrupts any title containing
mathematics.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from zotero_mcp.mapping import citation_key_of, year_of

logger = logging.getLogger(__name__)

ZOTERO_TO_BIBTEX = {
    "journalArticle": "article",
    "book": "book",
    "bookSection": "incollection",
    "conferencePaper": "inproceedings",
    "thesis": "phdthesis",
    "report": "techreport",
    "manuscript": "unpublished",
    "preprint": "misc",
    "webpage": "misc",
    "dataset": "misc",
    "presentation": "misc",
    "blogPost": "misc",
}

ZOTERO_TO_CSL = {
    "journalArticle": "article-journal",
    "book": "book",
    "bookSection": "chapter",
    "conferencePaper": "paper-conference",
    "thesis": "thesis",
    "report": "report",
    "preprint": "article",
    "webpage": "webpage",
    "dataset": "dataset",
    "blogPost": "post-weblog",
}

ZOTERO_TO_RIS = {
    "journalArticle": "JOUR",
    "book": "BOOK",
    "bookSection": "CHAP",
    "conferencePaper": "CONF",
    "thesis": "THES",
    "report": "RPRT",
    "preprint": "UNPB",
    "webpage": "ELEC",
    "dataset": "DATA",
}

#: Characters that must be escaped in a BibTeX value. Braces are deliberately
#: absent: authors use them to protect capitalisation and mathematics, and
#: escaping them (as both predecessors do) breaks exactly those titles.
_BIBTEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_NON_ASCII_KEY = re.compile(r"[^A-Za-z0-9]")


def escape_bibtex(value: str) -> str:
    """Escape a value for a BibTeX field, leaving braces intact."""
    out = []
    for char in str(value):
        out.append(_BIBTEX_ESCAPES.get(char, char))
    return "".join(out)


def make_citation_key(data: dict[str, Any], used: set[str] | None = None) -> str:
    """A citation key a person would actually type.

    ``surnameYearword``, disambiguated with a letter suffix, which is the
    convention Better BibTeX and most style guides use. Falls back through
    the pieces that exist rather than emitting a key containing the item's
    internal Zotero id.
    """
    if existing := citation_key_of(data):
        return existing

    creators = data.get("creators") or []
    surname = ""
    for creator in creators:
        if creator.get("creatorType") in {"author", "editor"} or not surname:
            surname = creator.get("lastName") or creator.get("name") or ""
            if surname:
                break
    surname = _NON_ASCII_KEY.sub("", _fold(surname)).lower() or "anon"

    year = year_of(data.get("date")) or "nd"

    title_word = ""
    for word in re.findall(r"[A-Za-z]{4,}", data.get("title") or ""):
        if word.lower() not in {"the", "and", "for", "with", "from", "into", "that", "this"}:
            title_word = word.lower()
            break

    key = f"{surname}{year}{title_word}"
    if used is None:
        return key

    candidate = key
    suffix = ord("a")
    while candidate in used:
        candidate = f"{key}{chr(suffix)}"
        suffix += 1
    used.add(candidate)
    return candidate


def _fold(value: str) -> str:
    try:
        from unidecode import unidecode

        return unidecode(value)
    except ImportError:  # pragma: no cover
        return value


def _authors(creators: list[dict[str, Any]], role: str) -> str:
    names = []
    for creator in creators:
        if creator.get("creatorType") != role:
            continue
        if name := creator.get("name"):
            names.append(f"{{{name}}}")
        elif creator.get("lastName"):
            names.append(f"{creator['lastName']}, {creator.get('firstName', '')}".strip(", "))
    return " and ".join(names)


def to_bibtex(items: list[dict[str, Any]]) -> str:
    """Render items as a BibTeX bibliography."""
    used: set[str] = set()
    entries = []

    for item in items:
        data = item.get("data") or item
        item_type = data.get("itemType", "document")
        if item_type in {"attachment", "note", "annotation"}:
            continue

        entry_type = ZOTERO_TO_BIBTEX.get(item_type, "misc")
        key = make_citation_key(data, used)
        fields: list[tuple[str, str]] = []

        if authors := _authors(data.get("creators") or [], "author"):
            fields.append(("author", authors))
        if editors := _authors(data.get("creators") or [], "editor"):
            fields.append(("editor", editors))

        for name, value in (
            ("title", data.get("title")),
            ("year", year_of(data.get("date"))),
            ("journal", data.get("publicationTitle")),
            ("booktitle", data.get("proceedingsTitle") or data.get("bookTitle")),
            ("volume", data.get("volume")),
            ("number", data.get("issue")),
            # BibTeX page ranges use an en dash written as two hyphens.
            ("pages", (data.get("pages") or "").replace("-", "--")),
            ("publisher", data.get("publisher")),
            ("school", data.get("university")),
            ("institution", data.get("institution")),
            ("doi", data.get("DOI")),
            ("url", data.get("url")),
            ("isbn", data.get("ISBN")),
            ("issn", data.get("ISSN")),
            ("note", data.get("extra")),
        ):
            if value:
                fields.append((name, escape_bibtex(str(value))))

        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
        entries.append(f"@{entry_type}{{{key},\n{body}\n}}")

    return "\n\n".join(entries)


def to_csl_json(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render items as CSL-JSON, the interchange format most tools accept."""
    used: set[str] = set()
    records = []

    for item in items:
        data = item.get("data") or item
        item_type = data.get("itemType", "document")
        if item_type in {"attachment", "note", "annotation"}:
            continue

        record: dict[str, Any] = {
            "id": make_citation_key(data, used),
            "type": ZOTERO_TO_CSL.get(item_type, "document"),
            "title": data.get("title", ""),
        }
        authors = [
            (
                {"literal": c["name"]}
                if c.get("name")
                else {"family": c.get("lastName", ""), "given": c.get("firstName", "")}
            )
            for c in data.get("creators") or []
            if c.get("creatorType") == "author"
        ]
        if authors:
            record["author"] = authors
        if year := year_of(data.get("date")):
            record["issued"] = {"date-parts": [[int(year)]]}
        for source, target in (
            ("publicationTitle", "container-title"),
            ("proceedingsTitle", "container-title"),
            ("volume", "volume"),
            ("issue", "issue"),
            ("pages", "page"),
            ("publisher", "publisher"),
            ("DOI", "DOI"),
            ("url", "URL"),
            ("ISBN", "ISBN"),
            ("ISSN", "ISSN"),
            ("abstractNote", "abstract"),
        ):
            if value := data.get(source):
                record.setdefault(target, value)
        records.append(record)

    return records


def to_ris(items: list[dict[str, Any]]) -> str:
    """Render items as RIS, which is what most reference managers import."""
    blocks = []
    for item in items:
        data = item.get("data") or item
        item_type = data.get("itemType", "document")
        if item_type in {"attachment", "note", "annotation"}:
            continue

        lines = [f"TY  - {ZOTERO_TO_RIS.get(item_type, 'GEN')}"]
        for creator in data.get("creators") or []:
            tag = {"author": "AU", "editor": "ED"}.get(creator.get("creatorType"), "A2")
            if name := creator.get("name"):
                lines.append(f"{tag}  - {name}")
            elif creator.get("lastName"):
                lines.append(
                    f"{tag}  - {creator['lastName']}, {creator.get('firstName', '')}".rstrip(", ")
                )
        for tag, value in (
            ("TI", data.get("title")),
            ("T2", data.get("publicationTitle") or data.get("proceedingsTitle")),
            ("PY", year_of(data.get("date"))),
            ("VL", data.get("volume")),
            ("IS", data.get("issue")),
            ("PB", data.get("publisher")),
            ("DO", data.get("DOI")),
            ("UR", data.get("url")),
            ("SN", data.get("ISBN") or data.get("ISSN")),
            ("AB", data.get("abstractNote")),
        ):
            if value:
                lines.append(f"{tag}  - {value}")
        if pages := data.get("pages"):
            start, _, end = pages.partition("-")
            lines.append(f"SP  - {start.strip()}")
            if end:
                lines.append(f"EP  - {end.strip()}")
        for tag in data.get("tags") or []:
            if isinstance(tag, dict) and tag.get("tag"):
                lines.append(f"KW  - {tag['tag']}")
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def from_better_bibtex(item_keys: list[str]) -> str | None:
    """Ask the Better BibTeX plugin to export, if it is running.

    Its keys are the ones already used in the user's manuscripts, so matching
    them matters more than any improvement the local generator could make.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover
        return None

    endpoint = "http://127.0.0.1:23119/better-bibtex/json-rpc"
    try:
        response = requests.post(
            endpoint,
            json={
                "jsonrpc": "2.0",
                "method": "item.export",
                "params": [item_keys, "betterbibtex"],
                "id": 1,
            },
            timeout=10,
        )
        if not response.ok:
            return None
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 (the plugin is optional by design)
        logger.debug("Better BibTeX not available: %s", exc)
        return None

    result = payload.get("result")
    if isinstance(result, list) and result:
        result = result[-1]
    return result if isinstance(result, str) and result.strip() else None


__all__ = [
    "escape_bibtex",
    "from_better_bibtex",
    "make_citation_key",
    "to_bibtex",
    "to_csl_json",
    "to_ris",
]
