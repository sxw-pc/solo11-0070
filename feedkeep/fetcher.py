"""Bounded conditional HTTP fetching using only the standard library."""

from __future__ import annotations

import hashlib
import http.client
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Optional
from urllib.parse import urlsplit

from .clock import Clock, SystemClock
from .models import FetchResult
from .policy import HostRateLimiter, Policy, PolicyViolation, backoff_seconds

CHUNK_SIZE = 8192


class _BodyLimitExceeded(Exception):
    def __init__(self, observed: int):
        super().__init__("body limit exceeded")
        self.observed = observed


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: Policy):
        super().__init__()
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.policy.assert_url_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_limited(response, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > limit:
                raise _BodyLimitExceeded(int(declared))
        except ValueError:
            pass
    data = bytearray()
    while True:
        allowance = limit + 1 - len(data)
        chunk = response.read(min(CHUNK_SIZE, allowance))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise _BodyLimitExceeded(len(data))


def _read_failure_body(response, limit: int) -> bytes:
    data = bytearray()
    try:
        while len(data) <= limit:
            chunk = response.read(min(CHUNK_SIZE, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    except (OSError, http.client.HTTPException):
        pass
    return bytes(data[:limit])


def _content_type(headers) -> Optional[str]:
    if not headers:
        return None
    value = headers.get("Content-Type")
    return value.split(";", 1)[0].strip() if value else None


def _failure(url: str, status: Optional[int], attempts: int, duration_ms: int,
             bytes_read: int, body: Optional[bytes], headers,
             code: str, message: str) -> FetchResult:
    return FetchResult(
        url=url, status=status, attempts=attempts, duration_ms=duration_ms,
        body_bytes=bytes_read, body_sha256=_hash(body) if body else None,
        body=body, etag=headers.get("ETag") if headers else None,
        last_modified=headers.get("Last-Modified") if headers else None,
        content_type=_content_type(headers), error_code=code,
        error_message=message[:300],
    )


class Fetcher:
    def __init__(self, policy: Policy, clock: Optional[Clock] = None):
        self.policy = policy
        self.clock = clock or SystemClock()
        self.limiter = HostRateLimiter(policy, self.clock)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PolicyRedirectHandler(policy),
        )

    def fetch(self, url: str, etag: Optional[str] = None,
              last_modified: Optional[str] = None) -> FetchResult:
        start = self.clock.monotonic()
        try:
            self.policy.assert_url_allowed(url)
        except PolicyViolation as exc:
            return _failure(url, None, 0, self._elapsed_ms(start), 0, None, None,
                            "POLICY_VIOLATION", exc.message)
        host = urlsplit(url).hostname or ""
        headers = self.policy.build_headers(etag, last_modified)
        attempts = 0
        result: Optional[FetchResult] = None
        for attempt in range(self.policy.max_retries + 1):
            self.limiter.wait(host)
            attempts += 1
            try:
                result, retryable = self._attempt(url, headers, start)
            except PolicyViolation as exc:
                return _failure(url, None, attempts, self._elapsed_ms(start), 0,
                                None, None, "POLICY_VIOLATION", exc.message)
            result = replace(result, attempts=attempts,
                             duration_ms=self._elapsed_ms(start))
            if result.ok or result.not_modified or not retryable:
                return result
            if attempt < self.policy.max_retries:
                self.clock.sleep(backoff_seconds(self.policy, attempt))
        assert result is not None
        return result

    def _attempt(self, url: str, headers: dict[str, str],
                 start: float) -> tuple[FetchResult, bool]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=self.policy.timeout_seconds)
        except urllib.error.HTTPError as exc:
            return self._handle_http_error(exc, url, start)
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (socket.timeout, TimeoutError))
            code = "FETCH_TIMEOUT" if timed_out else "FETCH_CONNECTION"
            return _failure(url, None, 0, self._elapsed_ms(start), 0, None, None,
                            code, f"连接失败: {exc.reason}"), True
        except (socket.timeout, TimeoutError):
            return _failure(url, None, 0, self._elapsed_ms(start), 0, None, None,
                            "FETCH_TIMEOUT", "请求超时"), True
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            return _failure(url, None, 0, self._elapsed_ms(start), 0, None, None,
                            "FETCH_CONNECTION", f"连接异常: {type(exc).__name__}"), True

        try:
            status = response.status
            response_headers = response.headers
            try:
                body = _read_limited(response, self.policy.max_body_bytes)
            except _BodyLimitExceeded as exc:
                return _failure(url, status, 0, self._elapsed_ms(start),
                                exc.observed, None, response_headers,
                                "BODY_TOO_LARGE",
                                f"响应体超过上限 {self.policy.max_body_bytes} 字节"), False
            except (socket.timeout, TimeoutError):
                return _failure(url, status, 0, self._elapsed_ms(start), 0, None,
                                response_headers, "FETCH_TIMEOUT", "读取响应体超时"), True
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                return _failure(url, status, 0, self._elapsed_ms(start), 0, None,
                                response_headers, "FETCH_CONNECTION",
                                f"读取响应体异常: {type(exc).__name__}"), True
            if status != 200:
                retryable = status == 429 or 500 <= status <= 599
                return _failure(url, status, 0, self._elapsed_ms(start), len(body),
                                body, response_headers, f"HTTP_{status}",
                                f"HTTP 状态码 {status}"), retryable
            return FetchResult(
                url=url, status=status, attempts=0,
                duration_ms=self._elapsed_ms(start), body_bytes=len(body),
                body_sha256=_hash(body), body=body,
                etag=response_headers.get("ETag"),
                last_modified=response_headers.get("Last-Modified"),
                content_type=_content_type(response_headers),
            ), False
        finally:
            response.close()

    def _handle_http_error(self, exc: urllib.error.HTTPError, url: str,
                           start: float) -> tuple[FetchResult, bool]:
        if exc.code == 304:
            headers = exc.headers
            result = FetchResult(
                url=url, status=304, attempts=0,
                duration_ms=self._elapsed_ms(start), body_bytes=0, body_sha256=None,
                etag=headers.get("ETag") if headers else None,
                last_modified=headers.get("Last-Modified") if headers else None,
                content_type=_content_type(headers),
            )
            exc.close()
            return result, False
        retryable = exc.code == 429 or 500 <= exc.code <= 599
        body = _read_failure_body(exc, self.policy.max_body_bytes)
        result = _failure(url, exc.code, 0, self._elapsed_ms(start), len(body),
                          body, exc.headers, f"HTTP_{exc.code}",
                          f"HTTP 状态码 {exc.code}")
        exc.close()
        return result, retryable

    def _elapsed_ms(self, start: float) -> int:
        return max(0, int(round((self.clock.monotonic() - start) * 1000)))
