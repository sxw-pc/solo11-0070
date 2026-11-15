from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
import yaml

from feedkeep.clock import FixedClock
from feedkeep.identity import normalize_item
from feedkeep.models import Enclosure, FeedSource, ParsedItem
from feedkeep.policy import Policy
from feedkeep.store import Store
from mockserver.server import running_mock_server


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock("2024-03-01T00:00:00Z", monotonic_start=2000.0)


@pytest.fixture
def store(tmp_path) -> Store:
    instance = Store(tmp_path / "archive.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def mock_server():
    with running_mock_server(port=0) as server:
        server.reset()
        yield server


@pytest.fixture
def source_factory():
    def make(server, path="/feeds/rss-basic.xml", source_id="rss-basic",
             fmt="auto", enabled=True, url=None, identity_contract="id_first"):
        return FeedSource(
            id=source_id, name=f"source {source_id}", url=url or server.base_url + path,
            format=fmt, enabled=enabled, tags=("test",), note="synthetic",
            identity_contract=identity_contract,
        )
    return make


@pytest.fixture
def write_feeds(tmp_path):
    def make(sources, filename="feeds.yaml"):
        feeds = [{
            "id": source.id, "name": source.name, "url": source.url,
            "format": source.format, "enabled": source.enabled,
            "tags": list(source.tags), "note": source.note,
            "identity_contract": source.identity_contract,
        } for source in sources]
        path = tmp_path / filename
        path.write_text(
            yaml.safe_dump({"version": 1, "feeds": feeds}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path
    return make


@pytest.fixture
def write_policy(tmp_path):
    def make(**overrides):
        data = {
            "version": 1,
            "allowed_hosts": ["127.0.0.1", "localhost", "::1"],
            "timeout_seconds": 1,
            "max_body_bytes": 524288,
            "max_items_per_feed": 200,
            "min_interval_ms": 100,
            "max_retries": 1,
            "backoff_base_ms": 1,
        }
        data.update(overrides)
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path
    return make


@pytest.fixture
def item_factory():
    def make(raw_id="item-1", title="示例标题", link=None, summary=None,
             content=None, published="2024-01-01T00:00:00Z", authors=(),
             categories=(), enclosures=(), contract="id_first"):
        parsed = ParsedItem(
            raw_id=raw_id, title=title, link=link, summary=summary, content=content,
            published_at=published, authors=tuple(authors),
            categories=tuple(categories), enclosures=tuple(enclosures),
        )
        return normalize_item(parsed, contract)
    return make


@pytest.fixture
def default_policy() -> Policy:
    return Policy(
        timeout_seconds=1.0, max_body_bytes=524288, max_items_per_feed=200,
        min_interval_ms=100, max_retries=1, backoff_base_ms=1,
    )


@pytest.fixture
def enclosure():
    def make(url="http://127.0.0.1:8765/files/a.pdf", mime="application/pdf", length=10, position=0):
        return Enclosure(url, mime, length, position)
    return make


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
