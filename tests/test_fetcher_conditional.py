from __future__ import annotations

from feedkeep.fetcher import Fetcher
from feedkeep.policy import Policy


def _policy(**overrides):
    values = dict(min_interval_ms=100, max_retries=1, timeout_seconds=1)
    values.update(overrides)
    return Policy(**values)


def test_second_fetch_sends_conditional_headers_and_gets_304(mock_server, fixed_clock):
    url = mock_server.base_url + "/feeds/rss-basic.xml"
    fetcher = Fetcher(_policy(), fixed_clock)
    first = fetcher.fetch(url)
    assert first.ok
    assert first.attempts == 1
    assert first.body_bytes > 0
    assert len(first.body_sha256) == 64
    assert first.etag

    second = fetcher.fetch(url, etag=first.etag, last_modified=first.last_modified)
    assert second.not_modified
    assert second.status == 304
    assert second.body is None
    assert second.body_bytes == 0

    records = mock_server.snapshot()
    assert len(records) == 2
    assert records[0]["if_none_match"] is None
    assert records[1]["if_none_match"] == first.etag
    assert records[1]["if_modified_since"] == first.last_modified
    assert records[1]["status"] == 304


def test_stale_etag_returns_updated_body(mock_server, fixed_clock):
    url = mock_server.base_url + "/feeds/rss-updated.xml"
    fetcher = Fetcher(_policy(), fixed_clock)
    result = fetcher.fetch(url, etag='"stale-value"')
    assert result.ok
    assert b"rss-basic:4" in result.body


def test_if_modified_since_alone_returns_304(mock_server, fixed_clock):
    url = mock_server.base_url + "/feeds/atom-basic.xml"
    fetcher = Fetcher(_policy(), fixed_clock)
    first = fetcher.fetch(url)
    second = fetcher.fetch(url, last_modified=first.last_modified)
    assert second.not_modified
    record = mock_server.snapshot()[-1]
    assert record["if_modified_since"] == first.last_modified
    assert record["if_none_match"] is None


def test_fetcher_declares_identity_and_minimal_headers(mock_server, fixed_clock):
    fetcher = Fetcher(_policy(), fixed_clock)
    result = fetcher.fetch(mock_server.base_url + "/feeds/jsonfeed-basic.json")
    assert result.ok
    record = mock_server.snapshot()[0]
    assert record["user_agent"].startswith("feedkeep/")
    assert record["if_none_match"] is None
