# Copyright 2026 Vinay

"""The one HTTP client, and the open-access cascade built on it.

``allow_external_services=false`` is a promise to users who chose local mode
for privacy reasons, so the tests that prove nothing calls out are the point of
this module, not an afterthought.
"""

from __future__ import annotations

import pytest

from zotero_mcp.config import NetworkSettings
from zotero_mcp.errors import RateLimited, Unsupported, UpstreamError
from zotero_mcp.external import http, openaccess


class FakeResponse:
    def __init__(
        self, status=200, payload=None, text="", content_type="application/json", headers=None
    ):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.content = text.encode() if isinstance(text, str) else text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def online(monkeypatch):
    """Install a scripted sequence of responses and record the requests made."""
    calls: list[dict] = []

    def install(*responses):
        queue = list(responses)

        def fake_get(url, headers=None, params=None, timeout=None, allow_redirects=None):
            calls.append({"url": url, "headers": headers or {}, "params": params or {}})
            outcome = queue.pop(0) if queue else responses[-1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr("requests.get", fake_get)
        monkeypatch.setattr(http.time, "sleep", lambda seconds: None)
        return calls

    return install


ALLOWED = NetworkSettings(allow_external_services=True, max_retries=3, backoff_seconds=0.0)
BLOCKED = NetworkSettings(allow_external_services=False)


def test_external_lookups_are_refused_when_they_are_disabled():
    with pytest.raises(Unsupported) as excinfo:
        http.get("https://example.invalid/", settings=BLOCKED)
    assert "ZOTERO_MCP_ALLOW_EXTERNAL" in excinfo.value.hint


def test_downloads_are_refused_when_external_services_are_disabled():
    with pytest.raises(Unsupported):
        http.download("https://example.invalid/x.pdf", settings=BLOCKED)


def test_json_is_parsed_and_other_content_comes_back_as_text(online):
    online(FakeResponse(payload={"ok": True}))
    assert http.get("https://example.invalid/", settings=ALLOWED) == {"ok": True}

    online(FakeResponse(text="<html/>", content_type="text/html"))
    assert http.get("https://example.invalid/", settings=ALLOWED) == "<html/>"


def test_a_404_is_absence_not_an_error(online):
    online(FakeResponse(status=404))
    assert http.get("https://example.invalid/", settings=ALLOWED) is None


def test_a_hard_failure_names_the_url(online):
    online(FakeResponse(status=403))
    with pytest.raises(UpstreamError) as excinfo:
        http.get("https://example.invalid/x", settings=ALLOWED)
    assert "example.invalid/x" in str(excinfo.value)


def test_malformed_json_is_reported_as_such(online):
    online(FakeResponse(payload=None, content_type="application/json"))
    with pytest.raises(UpstreamError) as excinfo:
        http.get("https://example.invalid/", settings=ALLOWED)
    assert "malformed JSON" in str(excinfo.value)


def test_a_transient_failure_is_retried_then_succeeds(online):
    calls = online(FakeResponse(status=503), FakeResponse(payload={"ok": True}))
    assert http.get("https://example.invalid/", settings=ALLOWED) == {"ok": True}
    assert len(calls) == 2


def test_persistent_rate_limiting_is_named_as_rate_limiting(online):
    online(FakeResponse(status=429))
    with pytest.raises(RateLimited):
        http.get("https://example.invalid/", settings=ALLOWED)


def test_a_retry_after_header_is_honoured(online, monkeypatch):
    online(FakeResponse(status=503, headers={"Retry-After": "2"}), FakeResponse(payload={}))
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    http.get("https://example.invalid/", settings=ALLOWED)
    assert slept == [2.0]


def test_a_nonsense_retry_after_falls_back_to_backoff(online, monkeypatch):
    settings = NetworkSettings(allow_external_services=True, backoff_seconds=1.0, max_retries=3)
    online(FakeResponse(status=503, headers={"Retry-After": "soon"}), FakeResponse(payload={}))
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    http.get("https://example.invalid/", settings=settings)
    assert slept == [1.0]


def test_a_connection_error_is_retried_then_reported(online):
    online(OSError("no route"), OSError("no route"), OSError("no route"))
    with pytest.raises(UpstreamError) as excinfo:
        http.get("https://example.invalid/", settings=ALLOWED)
    assert "no route" in str(excinfo.value)


def test_a_configured_contact_address_is_sent_in_the_user_agent(online):
    calls = online(FakeResponse(payload={}))
    settings = NetworkSettings(
        allow_external_services=True, contact_email="me@example.com", max_retries=1
    )
    http.get("https://example.invalid/", settings=settings)
    assert "mailto:me@example.com" in calls[0]["headers"]["User-Agent"]


def test_no_contact_address_means_no_mailto(online):
    calls = online(FakeResponse(payload={}))
    http.get("https://example.invalid/", settings=ALLOWED)
    assert "mailto" not in calls[0]["headers"]["User-Agent"]


def test_a_download_returns_bytes(online):
    online(FakeResponse(text="%PDF-1.4", content_type="application/pdf"))
    assert http.download("https://example.invalid/x.pdf", settings=ALLOWED) == b"%PDF-1.4"


def test_a_failed_download_is_none_rather_than_an_exception(online):
    online(FakeResponse(status=500, text=""))
    assert http.download("https://example.invalid/x.pdf", settings=ALLOWED) is None

    online(OSError("boom"))
    assert http.download("https://example.invalid/x.pdf", settings=ALLOWED) is None


# -- the open-access cascade ------------------------------------------------


def test_unpaywall_is_skipped_without_a_contact_address(online):
    calls = online(FakeResponse(payload={}))
    assert openaccess.from_unpaywall("10.1/x", ALLOWED) is None
    assert calls == []


def test_unpaywall_prefers_the_best_location_then_falls_back(online):
    settings = NetworkSettings(
        allow_external_services=True, contact_email="me@example.com", max_retries=1
    )
    online(FakeResponse(payload={"best_oa_location": {"url_for_pdf": "https://a/1.pdf"}}))
    assert openaccess.from_unpaywall("10.1/x", settings) == "https://a/1.pdf"

    online(
        FakeResponse(
            payload={"best_oa_location": {}, "oa_locations": [{"url_for_pdf": "https://b/2.pdf"}]}
        )
    )
    assert openaccess.from_unpaywall("10.1/x", settings) == "https://b/2.pdf"


def test_arxiv_pdfs_are_at_a_predictable_url():
    assert openaccess.from_arxiv("1706.03762") == "https://arxiv.org/pdf/1706.03762.pdf"


def test_pmc_returns_the_article_pdf_when_there_is_a_pmcid(online):
    online(FakeResponse(payload={"records": [{"pmcid": "PMC123"}]}))
    assert "PMC123" in openaccess.from_pmc("10.1/x", ALLOWED)

    online(FakeResponse(payload={"records": [{}]}))
    assert openaccess.from_pmc("10.1/x", ALLOWED) is None


def test_semantic_scholar_returns_its_open_access_link(online):
    online(FakeResponse(payload={"openAccessPdf": {"url": "https://s2/3.pdf"}}))
    assert openaccess.from_semantic_scholar("10.1/x", ALLOWED) == "https://s2/3.pdf"

    online(FakeResponse(payload={"openAccessPdf": None}))
    assert openaccess.from_semantic_scholar("10.1/x", ALLOWED) is None


def test_an_arxiv_id_short_circuits_the_cascade(online):
    calls = online(FakeResponse(payload={}))
    found = openaccess.find_pdf(doi="10.1/x", arxiv_id="1706.03762", settings=ALLOWED)
    assert found == ("https://arxiv.org/pdf/1706.03762.pdf", "arXiv")
    assert calls == []


def test_one_failing_source_does_not_stop_the_cascade(online):
    online(
        UpstreamError("Unpaywall is down"),
        FakeResponse(payload={"records": [{"pmcid": "PMC9"}]}),
    )
    settings = NetworkSettings(
        allow_external_services=True,
        contact_email="me@example.com",
        max_retries=1,
        backoff_seconds=0.0,
    )
    found = openaccess.find_pdf(doi="10.1/x", arxiv_id=None, settings=settings)
    assert found is not None
    assert found[1] == "PubMed Central"


def test_nothing_found_anywhere_is_none(online):
    online(FakeResponse(status=404))
    assert openaccess.find_pdf(doi="10.1/x", arxiv_id=None, settings=ALLOWED) is None


def test_no_identifiers_at_all_means_no_lookups(online):
    calls = online(FakeResponse(payload={}))
    assert openaccess.find_pdf(doi=None, arxiv_id=None, settings=ALLOWED) is None
    assert calls == []
