from __future__ import annotations

import unicodedata

import pytest

from feedkeep.parsers.common import (
    ParseError,
    normalize_text,
    normalize_title,
    normalize_url,
    parse_declared_length,
    parse_iso_datetime,
    parse_rfc822_datetime,
    parse_xml_document,
    unique_strings,
)


def test_iso_dates_are_utc_seconds():
    assert parse_iso_datetime("2024-01-02T03:04:05Z") == "2024-01-02T03:04:05Z"
    assert parse_iso_datetime("2024-01-02T11:04:05+08:00") == "2024-01-02T03:04:05Z"
    assert parse_iso_datetime("2024-01-02") == "2024-01-02T00:00:00Z"
    assert parse_iso_datetime(None) is None


def test_rfc822_dates_are_canonical():
    assert parse_rfc822_datetime("Mon, 01 Jan 2024 08:00:00 GMT") == "2024-01-01T08:00:00Z"
    assert parse_rfc822_datetime("Mon, 01 Jan 2024 16:00:00 +0800") == "2024-01-01T08:00:00Z"


@pytest.mark.parametrize("value,parser", [
    ("not-a-date", parse_iso_datetime),
    ("not-a-date", parse_rfc822_datetime),
    ("2024-13-01T00:00:00Z", parse_iso_datetime),
])
def test_invalid_dates_raise(value, parser):
    with pytest.raises((ValueError, TypeError)):
        parser(value)


def test_text_normalization():
    assert normalize_text("  a\r\nb \r c  ") == "a\nb\n c"
    assert normalize_title("  a\n\t b  ") == "a b"
    assert normalize_text("") is None
    decomposed = "Café"
    assert normalize_text(decomposed) == unicodedata.normalize("NFC", decomposed)


def test_url_normalization():
    assert normalize_url("HTTP://127.0.0.1:80/a?x=1#frag") == "http://127.0.0.1/a?x=1"
    assert normalize_url("https://LOCALHOST:443/Path") == "https://localhost/Path"
    assert normalize_url("http://[::1]:8765/a") == "http://[::1]:8765/a"
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("http://user:pass@127.0.0.1/a") is None
    assert normalize_url(None) is None


def test_declared_lengths_are_tolerant():
    assert parse_declared_length("42") == 42
    assert parse_declared_length(7) == 7
    assert parse_declared_length("x") is None
    assert parse_declared_length(-1) is None
    assert parse_declared_length(True) is None


def test_unique_strings_trim_and_dedupe():
    assert unique_strings([" a ", "a", "", None, "b"]) == ("a", "b")


def test_xml_security_and_syntax_errors():
    with pytest.raises(ParseError) as raised:
        parse_xml_document(b'<!DOCTYPE rss SYSTEM "http://127.0.0.1/x"><rss/>')
    assert raised.value.code == "PARSE_UNSAFE_XML"
    with pytest.raises(ParseError) as raised:
        parse_xml_document(b"<rss><channel></rss>")
    assert raised.value.code == "PARSE_INVALID_XML"
