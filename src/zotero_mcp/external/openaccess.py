# Copyright 2026 Vinay

"""Finding a legally free PDF for a work.

Four sources, tried in order of how reliably they point at a real open-access
file: Unpaywall (which exists for exactly this), arXiv (authoritative for
preprints), PubMed Central, then Semantic Scholar. Each returns a URL or None;
the caller downloads.

Nothing here goes near a paywall or a proxy. Every source indexes material the
publisher or author has already made freely available.
"""

from __future__ import annotations

import logging

from zotero_mcp.config import NetworkSettings
from zotero_mcp.external import http

logger = logging.getLogger(__name__)


def from_unpaywall(doi: str, settings: NetworkSettings) -> str | None:
    """The best open-access location Unpaywall knows for a DOI.

    Unpaywall requires a contact address. Without one configured the source is
    skipped rather than called anonymously, which would be against its terms.
    """
    if not settings.contact_email:
        logger.debug("Skipping Unpaywall: no contact email configured")
        return None
    payload = http.get(
        f"https://api.unpaywall.org/v2/{doi}",
        settings=settings,
        params={"email": settings.contact_email},
    )
    if not isinstance(payload, dict):
        return None
    best = payload.get("best_oa_location") or {}
    if url := best.get("url_for_pdf"):
        return url
    for location in payload.get("oa_locations") or []:
        if url := location.get("url_for_pdf"):
            return url
    return None


def from_arxiv(arxiv_id: str) -> str:
    """arXiv serves every paper's PDF at a predictable URL."""
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def from_pmc(doi: str, settings: NetworkSettings) -> str | None:
    """PubMed Central's copy, when the work has one."""
    payload = http.get(
        "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
        settings=settings,
        params={"ids": doi, "format": "json"},
    )
    records = (payload or {}).get("records") or []
    for record in records:
        if pmcid := record.get("pmcid"):
            return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
    return None


def from_semantic_scholar(doi: str, settings: NetworkSettings) -> str | None:
    """Semantic Scholar's open-access link, if it has one."""
    payload = http.get(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
        settings=settings,
        params={"fields": "openAccessPdf"},
    )
    if not isinstance(payload, dict):
        return None
    return ((payload.get("openAccessPdf") or {}) or {}).get("url")


def find_pdf(
    *,
    doi: str | None,
    arxiv_id: str | None,
    settings: NetworkSettings,
) -> tuple[str, str] | None:
    """First open-access PDF found, as ``(url, source)``.

    Sources are attempted in order and failures are swallowed: an outage at one
    of four optional enrichment services must not fail the import of an item
    whose metadata is already in hand.
    """
    attempts: list[tuple[str, callable]] = []
    if arxiv_id:
        attempts.append(("arXiv", lambda: from_arxiv(arxiv_id)))
    if doi:
        attempts += [
            ("Unpaywall", lambda: from_unpaywall(doi, settings)),
            ("PubMed Central", lambda: from_pmc(doi, settings)),
            ("Semantic Scholar", lambda: from_semantic_scholar(doi, settings)),
        ]

    for source, lookup in attempts:
        try:
            if url := lookup():
                return url, source
        except Exception as exc:  # noqa: BLE001 (one source failing is not fatal)
            logger.debug("%s lookup failed: %s", source, exc)
    return None


__all__ = ["find_pdf", "from_arxiv", "from_pmc", "from_semantic_scholar", "from_unpaywall"]
