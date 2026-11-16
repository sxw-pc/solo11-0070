"""Loopback-only synthetic feed publisher with fault injection."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
LAST_MODIFIED_EPOCH = 1_704_672_000  # 2024-01-08T00:00:00Z
LAST_MODIFIED = formatdate(LAST_MODIFIED_EPOCH, usegmt=True)
MAX_LOG = 2000
HUGE_BYTES = 600 * 1024

STATIC_ENDPOINTS = {
    "/feeds/rss-basic.xml": ("rss_basic.xml", "application/rss+xml; charset=utf-8"),
    "/feeds/rss-updated.xml": ("rss_updated.xml", "application/rss+xml; charset=utf-8"),
    "/feeds/atom-basic.xml": ("atom_basic.xml", "application/atom+xml; charset=utf-8"),
    "/feeds/atom-updated.xml": ("atom_updated.xml", "application/atom+xml; charset=utf-8"),
    "/feeds/jsonfeed-basic.json": ("jsonfeed_basic.json", "application/feed+json; charset=utf-8"),
    "/feeds/jsonfeed-updated.json": ("jsonfeed_updated.json", "application/feed+json; charset=utf-8"),
    "/feeds/rss-malformed.xml": ("rss_malformed.xml", "application/rss+xml; charset=utf-8"),
    "/feeds/atom-mixed.xml": ("atom_mixed_bad_items.xml", "application/atom+xml; charset=utf-8"),
    "/feeds/jsonfeed-odd.json": ("jsonfeed_odd_values.json", "application/feed+json; charset=utf-8"),
}


@dataclass(frozen=True)
class RequestRecord:
    path: str
    query: dict[str, list[str]]
    if_none_match: Optional[str]
    if_modified_since: Optional[str]
    user_agent: Optional[str]
    status: int

    def as_dict(self) -> dict:
        return asdict(self)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def strong_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:24] + '"'


class MockFeedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        self.requests: deque[RequestRecord] = deque(maxlen=MAX_LOG)
        self._lock = threading.Lock()
        self.flaky_counts: dict[str, int] = {}
        self.thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, record: RequestRecord) -> None:
        with self._lock:
            self.requests.append(record)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [record.as_dict() for record in self.requests]

    def count_flaky(self, key: str) -> int:
        with self._lock:
            self.flaky_counts[key] = self.flaky_counts.get(key, 0) + 1
            return self.flaky_counts[key]

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.flaky_counts.clear()


def _not_modified(if_none_match: Optional[str], if_modified_since: Optional[str],
                  etag: str) -> bool:
    if if_none_match:
        tags = [tag.strip() for tag in if_none_match.split(",")]
        if "*" in tags or etag in tags or f"W/{etag}" in tags:
            return True
        return False
    if if_modified_since:
        try:
            since = parsedate_to_datetime(if_modified_since)
            return since.timestamp() >= LAST_MODIFIED_EPOCH
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _static_response(name: str, content_type: str, if_none_match: Optional[str],
                     if_modified_since: Optional[str]) -> tuple[int, dict[str, str], bytes]:
    body = fixture_bytes(name)
    etag = strong_etag(body)
    headers = {
        "Content-Type": content_type,
        "ETag": etag,
        "Last-Modified": LAST_MODIFIED,
    }
    if _not_modified(if_none_match, if_modified_since, etag):
        return 304, headers, b""
    return 200, headers, body


class FeedRequestHandler(BaseHTTPRequestHandler):
    server_version = "FeedkeepMock/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _route(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        inm = self.headers.get("If-None-Match")
        ims = self.headers.get("If-Modified-Since")
        if path in STATIC_ENDPOINTS:
            name, content_type = STATIC_ENDPOINTS[path]
            return _static_response(name, content_type, inm, ims)
        if path == "/healthz":
            body = json.dumps({"status": "ok"}, separators=(",", ":")).encode()
            return 200, {"Content-Type": "application/json"}, body
        if path == "/feeds/flaky-500.xml":
            key = f"{path}?n={query.get('n', ['3'])[0]}"
            count = self.server.count_flaky(key)
            try:
                threshold = max(0, min(20, int(query.get("n", ["3"])[0])))
            except ValueError:
                threshold = 3
            if count <= threshold:
                return 500, {"Content-Type": "text/plain; charset=utf-8"}, b"temporary failure\n"
            return _static_response("rss_basic.xml", "application/rss+xml; charset=utf-8",
                                    inm, ims)
        if path == "/feeds/slow.xml":
            try:
                delay_ms = max(0, min(30_000, int(query.get("delay_ms", ["6000"])[0])))
            except ValueError:
                delay_ms = 6000
            time.sleep(delay_ms / 1000.0)
            return _static_response("rss_basic.xml", "application/rss+xml; charset=utf-8",
                                    inm, ims)
        if path == "/feeds/huge.xml":
            body = b"<rss><channel>" + (b"x" * HUGE_BYTES) + b"</channel></rss>"
            return 200, {
                "Content-Type": "application/rss+xml; charset=utf-8",
                "ETag": strong_etag(body),
            }, body
        if path == "/feeds/redirect-external.xml":
            return 302, {"Location": "http://198.51.100.99/feed.xml"}, b""
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found\n"

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            status, headers, body = self._route(parts.path, query)
        except Exception as exc:  # defensive: the mock server must stay alive
            status, headers, body = 500, {"Content-Type": "text/plain; charset=utf-8"}, (
                f"mock error: {type(exc).__name__}\n".encode()
            )
        self.server.record(RequestRecord(
            path=parts.path, query=query,
            if_none_match=self.headers.get("If-None-Match"),
            if_modified_since=self.headers.get("If-Modified-Since"),
            user_agent=self.headers.get("User-Agent"), status=status,
        ))
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def do_HEAD(self) -> None:
        self.send_error(405)


def start_mock_server(host: str = "127.0.0.1", port: int = 8765) -> MockFeedServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("mock server must bind to a loopback address")
    server = MockFeedServer((host, port), FeedRequestHandler)
    thread = threading.Thread(target=server.serve_forever, name="feedkeep-mock", daemon=True)
    server.thread = thread
    thread.start()
    return server


def stop_mock_server(server: MockFeedServer) -> None:
    server.shutdown()
    server.server_close()
    if server.thread is not None:
        server.thread.join(timeout=2)


@contextmanager
def running_mock_server(host: str = "127.0.0.1", port: int = 0):
    server = start_mock_server(host, port)
    try:
        yield server
    finally:
        stop_mock_server(server)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    port = 8765
    if args:
        if len(args) == 2 and args[0] in {"--port", "-p"}:
            port = int(args[1])
        else:
            print("usage: python -m mockserver.server [--port PORT]", file=sys.stderr)
            return 2
    with running_mock_server(port=port) as server:
        print(f"mock feed server listening on {server.base_url}", flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
