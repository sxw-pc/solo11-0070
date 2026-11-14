from __future__ import annotations

import pytest

from feedkeep.detect import FormatDetectionError, detect_format
from feedkeep.mockdata import generate_all


def test_detects_bundled_formats():
    fixtures = generate_all()
    assert detect_format(fixtures["rss_basic.xml"]) == "rss"
    assert detect_format(fixtures["rss_updated.xml"]) == "rss"
    assert detect_format(fixtures["atom_basic.xml"]) == "atom"
    assert detect_format(fixtures["jsonfeed_basic.json"]) == "jsonfeed"


def test_detects_malformed_rss_by_root_marker():
    assert detect_format(generate_all()["rss_malformed.xml"]) == "rss"


def test_detects_namespaced_atom_without_content_type():
    body = b'<?xml version="1.0"?><a:feed xmlns:a="urn:test"><a:entry /></a:feed>'
    assert detect_format(body) == "atom"


def test_detects_json_with_bom():
    assert detect_format(b"\xef\xbb\xbf {\"version\":\"1.1\",\"items\":[]}") == "jsonfeed"


def test_content_type_hint_for_unusual_body():
    assert detect_format(b"plain", "application/atom+xml; charset=utf-8") == "atom"
    assert detect_format(b"plain", "application/rss+xml") == "rss"


def test_empty_and_unknown_bodies_fail():
    with pytest.raises(FormatDetectionError) as raised:
        detect_format(b"   ")
    assert raised.value.code == "FORMAT_EMPTY"
    with pytest.raises(FormatDetectionError) as raised:
        detect_format(b"<html><body>no feed</body></html>")
    assert raised.value.code == "FORMAT_UNKNOWN_XML"
    with pytest.raises(FormatDetectionError) as raised:
        detect_format(b"not a feed")
    assert raised.value.code == "FORMAT_UNKNOWN"
