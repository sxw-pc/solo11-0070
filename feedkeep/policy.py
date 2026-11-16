"""Network policy: loopback-only targets, request bounds and rate limiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from .clock import Clock

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_ALLOWED_HOSTS = LOOPBACK_HOSTS
USER_AGENT = "feedkeep/0.1 (offline feed archiver; local evaluation)"
ACCEPT = (
    "application/rss+xml, application/atom+xml, application/feed+json, "
    "application/xml;q=0.9, application/json;q=0.9, */*;q=0.1"
)
MAX_BACKOFF_MS = 30_000


class PolicyViolation(Exception):
    """A request was refused before any socket or DNS activity occurred."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Policy:
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    timeout_seconds: float = 5.0
    max_body_bytes: int = 524_288
    max_items_per_feed: int = 200
    min_interval_ms: int = 200
    max_retries: int = 2
    backoff_base_ms: int = 250

    @property
    def min_interval_seconds(self) -> float:
        return self.min_interval_ms / 1000.0

    @property
    def backoff_base_seconds(self) -> float:
        return self.backoff_base_ms / 1000.0

    def is_allowed_host(self, host: str) -> bool:
        allowed = {h.lower() for h in self.allowed_hosts}
        return host.lower() in LOOPBACK_HOSTS and host.lower() in allowed

    def assert_url_allowed(self, url: str) -> None:
        """Validate a URL without performing DNS resolution."""

        if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
            raise PolicyViolation("POLICY_001", "URL 为空或包含控制字符")
        if "\\" in url:
            raise PolicyViolation("POLICY_001", "URL 不允许反斜杠")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise PolicyViolation("POLICY_001", f"URL 无法解析: {exc}") from exc
        if parts.scheme.lower() not in {"http", "https"}:
            raise PolicyViolation("POLICY_002", "只允许 http(s) URL")
        if parts.username is not None or parts.password is not None:
            raise PolicyViolation("POLICY_003", "URL 不允许携带用户信息")
        if not parts.hostname:
            raise PolicyViolation("POLICY_004", "URL 缺少主机名")
        if port is not None and not (1 <= port <= 65535):
            raise PolicyViolation("POLICY_006", "端口越界")
        if not self.is_allowed_host(parts.hostname):
            raise PolicyViolation(
                "POLICY_005",
                f"主机 {parts.hostname!r} 不在白名单 {list(self.allowed_hosts)!r} 内",
            )

    def build_headers(
        self,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> dict[str, str]:
        """Construct the deliberately small request header set."""

        headers = {"Accept": ACCEPT, "User-Agent": USER_AGENT}
        if etag and "\r" not in etag and "\n" not in etag:
            headers["If-None-Match"] = etag
        if last_modified and "\r" not in last_modified and "\n" not in last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers


class HostRateLimiter:
    """Enforce a minimum interval between requests to the same hostname."""

    def __init__(self, policy: Policy, clock: Clock):
        self.policy = policy
        self.clock = clock
        self._last_request: dict[str, float] = {}

    def wait(self, host: str) -> float:
        normalized = host.lower()
        now = self.clock.monotonic()
        slept = 0.0
        previous = self._last_request.get(normalized)
        if previous is not None:
            elapsed = now - previous
            remaining = self.policy.min_interval_seconds - elapsed
            if remaining > 0:
                self.clock.sleep(remaining)
                slept = remaining
        self._last_request[normalized] = self.clock.monotonic()
        return slept


def backoff_seconds(policy: Policy, attempt: int) -> float:
    """Exponential backoff for attempt 0, 1, ... capped at 30 seconds."""

    milliseconds = policy.backoff_base_ms * (2 ** max(0, attempt))
    return min(milliseconds, MAX_BACKOFF_MS) / 1000.0


def normalize_allowed_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(host.strip().lower() for host in hosts))
