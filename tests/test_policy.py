from __future__ import annotations

import socket

import pytest

from feedkeep.clock import FixedClock
from feedkeep.policy import (
    HostRateLimiter,
    Policy,
    PolicyViolation,
    backoff_seconds,
)


def test_loopback_urls_allowed():
    policy = Policy()
    for url in (
        "http://127.0.0.1:8765/feeds/a.xml",
        "http://localhost/feeds/a.xml",
        "http://[::1]:8765/feeds/a.xml",
        "https://127.0.0.1/",
    ):
        policy.assert_url_allowed(url)


@pytest.mark.parametrize("url,code", [
    ("http://198.51.100.7/feed.xml", "POLICY_005"),
    ("ftp://127.0.0.1/feed.xml", "POLICY_002"),
    ("http://user:pass@127.0.0.1/feed.xml", "POLICY_003"),
    ("http:///feed.xml", "POLICY_004"),
    ("http://127.0.0.1:99999/feed.xml", "POLICY_001"),
    ("http://127.0.0.1/\nfeed.xml", "POLICY_001"),
    ("http://127.0.0.1\\@198.51.100.7/", "POLICY_001"),
])
def test_invalid_urls_rejected(url, code):
    with pytest.raises(PolicyViolation) as raised:
        Policy().assert_url_allowed(url)
    assert raised.value.code == code


def test_non_whitelisted_is_rejected_without_dns(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("DNS must not run")
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    with pytest.raises(PolicyViolation):
        Policy().assert_url_allowed("http://198.51.100.2/source.xml")


def test_headers_are_minimal_and_identified():
    headers = Policy().build_headers('"abc"', "Mon, 01 Jan 2024 00:00:00 GMT")
    assert headers["User-Agent"].startswith("feedkeep/")
    assert "Accept" in headers
    assert headers["If-None-Match"] == '"abc"'
    assert "If-Modified-Since" in headers
    assert not any(key.lower() in {"authorization", "cookie", "proxy-authorization"}
                   for key in headers)


def test_header_injection_values_are_ignored():
    headers = Policy().build_headers('"abc"\r\nX-Evil: 1', "date\nX-Evil: 1")
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers


def test_rate_limiter_enforces_interval():
    clock = FixedClock(monotonic_start=10.0)
    policy = Policy(min_interval_ms=150)
    limiter = HostRateLimiter(policy, clock)
    assert limiter.wait("127.0.0.1") == 0
    slept = limiter.wait("127.0.0.1")
    assert slept == pytest.approx(0.15)
    assert clock.sleeps == [pytest.approx(0.15)]
    limiter.wait("localhost")
    assert clock.sleeps == [pytest.approx(0.15)]


def test_backoff_is_exponential_and_capped():
    policy = Policy(backoff_base_ms=250)
    assert backoff_seconds(policy, 0) == pytest.approx(0.25)
    assert backoff_seconds(policy, 1) == pytest.approx(0.5)
    assert backoff_seconds(policy, 2) == pytest.approx(1.0)
    assert backoff_seconds(policy, 20) == pytest.approx(30.0)


def test_allowed_hosts_are_case_insensitive():
    policy = Policy(allowed_hosts=("LOCALHOST",))
    policy.assert_url_allowed("http://LocalHost:8765/x")
