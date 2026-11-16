from __future__ import annotations

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.policy import Policy


def test_response_body_limit_aborts_fetch(mock_server, fixed_clock):
    policy = Policy(max_body_bytes=4096, max_retries=2, min_interval_ms=100)
    result = Fetcher(policy, fixed_clock).fetch(mock_server.base_url + "/feeds/huge.xml")
    assert result.error_code == "BODY_TOO_LARGE"
    assert result.status == 200
    assert result.attempts == 1
    assert result.body is None
    assert result.body_bytes > 4096


def test_timeout_is_retried_then_reported(mock_server, fixed_clock):
    policy = Policy(timeout_seconds=0.05, max_retries=1, min_interval_ms=100,
                    backoff_base_ms=1)
    result = Fetcher(policy, fixed_clock).fetch(
        mock_server.base_url + "/feeds/slow.xml?delay_ms=200"
    )
    assert result.error_code == "FETCH_TIMEOUT"
    assert result.attempts == 2
    assert len(fixed_clock.sleeps) >= 2


def test_500_is_retried_until_success(mock_server, fixed_clock):
    policy = Policy(max_retries=2, min_interval_ms=100, backoff_base_ms=1)
    result = Fetcher(policy, fixed_clock).fetch(
        mock_server.base_url + "/feeds/flaky-500.xml?n=2"
    )
    assert result.ok
    assert result.attempts == 3
    statuses = [record["status"] for record in mock_server.snapshot()]
    assert statuses == [500, 500, 200]


def test_404_is_not_retried(mock_server, fixed_clock):
    policy = Policy(max_retries=3, min_interval_ms=100)
    result = Fetcher(policy, fixed_clock).fetch(
        mock_server.base_url + "/feeds/not-there.xml"
    )
    assert result.error_code == "HTTP_404"
    assert result.status == 404
    assert result.attempts == 1
    assert len(mock_server.snapshot()) == 1


def test_policy_violation_never_opens_socket(fixed_clock):
    policy = Policy(min_interval_ms=100)
    result = Fetcher(policy, fixed_clock).fetch("http://198.51.100.9/feed.xml")
    assert result.error_code == "POLICY_VIOLATION"
    assert result.attempts == 0
    assert result.status is None


def test_rate_limit_sleeps_between_same_host_requests(mock_server, fixed_clock):
    policy = Policy(min_interval_ms=250, max_retries=0)
    fetcher = Fetcher(policy, fixed_clock)
    first = fetcher.fetch(mock_server.base_url + "/feeds/rss-basic.xml")
    second = fetcher.fetch(
        mock_server.base_url + "/feeds/rss-basic.xml",
        etag=first.etag, last_modified=first.last_modified,
    )
    assert second.not_modified
    assert fixed_clock.total_slept == pytest.approx(0.25)


def test_redirect_to_non_whitelisted_host_is_rejected(mock_server, fixed_clock):
    policy = Policy(max_retries=2, min_interval_ms=100)
    result = Fetcher(policy, fixed_clock).fetch(
        mock_server.base_url + "/feeds/redirect-external.xml"
    )
    assert result.error_code == "POLICY_VIOLATION"
    assert result.attempts == 1
