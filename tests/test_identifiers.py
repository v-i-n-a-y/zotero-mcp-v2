# Copyright 2026 Vinay

"""Identifier recognition and normalisation."""

import pytest

from zotero_mcp.identifiers import (
    IdentifierKind,
    is_zotero_key,
    normalize_arxiv_id,
    normalize_doi,
    normalize_isbn,
    normalize_pmcid,
    normalize_pmid,
    parse_identifier,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1038/nature12373", "10.1038/nature12373"),
        ("https://doi.org/10.1038/nature12373", "10.1038/nature12373"),
        ("http://dx.doi.org/10.1038/Nature12373", "10.1038/nature12373"),
        ("doi: 10.1000/XYZ.123", "10.1000/xyz.123"),
        ("info:doi/10.1000/xyz", "10.1000/xyz"),
        # A DOI lifted out of a reference list keeps its full stop.
        ("See 10.1000/xyz.123.", "10.1000/xyz.123"),
        ("(10.1000/abc)", "10.1000/abc"),
        (
            "10.1002/(SICI)1097-0142(19960101)77:1<1::AID>3.0.CO;2-9",
            "10.1002/(sici)1097-0142(19960101)77:1<1::aid>3.0.co;2-9",
        ),
    ],
)
def test_normalize_doi_accepts(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not a doi", "10.x/abc", "1038/nature"])
def test_normalize_doi_rejects(raw):
    assert normalize_doi(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2301.00001", "2301.00001"),
        ("arXiv:2301.00001v2", "2301.00001"),
        ("https://arxiv.org/abs/2301.00001", "2301.00001"),
        ("https://arxiv.org/pdf/2301.00001v3.pdf", "2301.00001"),
        ("hep-th/9901001", "hep-th/9901001"),
        ("https://arxiv.org/abs/math.GT/0309136", "math.GT/0309136"),
        ("2301.000012", "2301.00001"),
    ],
)
def test_normalize_arxiv(raw, expected):
    assert normalize_arxiv_id(raw) == expected


def test_normalize_arxiv_keeps_version_on_request():
    assert normalize_arxiv_id("arXiv:2301.00001v2", keep_version=True) == "2301.00001v2"
    # No version present means nothing to keep.
    assert normalize_arxiv_id("2301.00001", keep_version=True) == "2301.00001"


def test_isbn_checksum_is_verified():
    assert normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert normalize_isbn("0-306-40615-2") == "0306406152"
    # A single transposed digit must be rejected, not silently accepted.
    assert normalize_isbn("978-0-306-40615-8") is None
    assert normalize_isbn("1234567890123") is None


def test_pmid_requires_a_prefix():
    """A bare integer is far more often a year or a count than a PMID."""
    assert normalize_pmid("PMID: 23193287") == "23193287"
    assert normalize_pmid("https://pubmed.ncbi.nlm.nih.gov/23193287/") == "23193287"
    assert normalize_pmid("23193287") is None


def test_pmcid():
    assert normalize_pmcid("PMC3539452") == "PMC3539452"
    assert normalize_pmcid("pmc3539452") == "PMC3539452"
    assert normalize_pmcid("3539452") is None


def test_zotero_key_shape():
    assert is_zotero_key("ABCD2345")
    assert not is_zotero_key("ABCD234")  # too short
    assert not is_zotero_key("ABCDI345")  # 'I' is not in Zotero's alphabet
    assert not is_zotero_key("abcd2345x")


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("10.1038/nature12373", IdentifierKind.DOI),
        # A DOI inside a URL reads as a DOI, which is the more specific answer.
        ("https://doi.org/10.1038/nature12373", IdentifierKind.DOI),
        ("arXiv:2301.00001", IdentifierKind.ARXIV),
        ("PMC3539452", IdentifierKind.PMCID),
        ("PMID: 23193287", IdentifierKind.PMID),
        ("978-0-306-40615-7", IdentifierKind.ISBN),
        ("ABCD2345", IdentifierKind.ZOTERO_KEY),
        ("https://example.com/paper", IdentifierKind.URL),
        ("just some words", IdentifierKind.UNKNOWN),
        ("", IdentifierKind.UNKNOWN),
    ],
)
def test_parse_identifier_classification(raw, kind):
    assert parse_identifier(raw).kind is kind


def test_parse_identifier_returns_canonical_value_not_raw():
    parsed = parse_identifier("https://doi.org/10.1038/Nature12373")
    assert parsed.value == "10.1038/nature12373"
    assert parsed.raw == "https://doi.org/10.1038/Nature12373"
