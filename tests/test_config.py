from __future__ import annotations

import pytest
import yaml

from feedkeep.config import ConfigError, load_config, load_feeds, load_policy
from feedkeep.models import FeedSource


def _codes(exc: ConfigError) -> set[str]:
    return {issue.code for issue in exc.issues}


def test_loads_bundled_config(repo_root):
    policy = load_policy(repo_root / "configs" / "policy.yaml")
    feeds = load_feeds(repo_root / "configs" / "feeds.yaml", policy)
    assert len(feeds) == 6
    assert {feed.format for feed in feeds} == {"auto", "atom", "jsonfeed", "rss"}
    assert all(feed.url.startswith("http://127.0.0.1:8765/") for feed in feeds)
    assert policy.min_interval_ms == 200


def test_rejects_duplicate_ids(tmp_path, mock_server, source_factory, write_feeds, write_policy):
    first = source_factory(mock_server, source_id="dup")
    second = source_factory(mock_server, path="/feeds/atom-basic.xml", source_id="dup")
    path = write_feeds([first, second])
    policy = load_policy(write_policy())
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, policy)
    assert "CFG_006" in _codes(raised.value)
    assert any(issue.path == "feeds[1].id" for issue in raised.value.issues)


def test_rejects_missing_field(tmp_path, write_policy):
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "version: 1\nfeeds:\n  - id: x\n    name: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    assert any(issue.code == "CFG_004" and "url" in issue.path for issue in raised.value.issues)


def test_rejects_non_loopback_url(tmp_path, write_policy):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "feeds": [{
            "id": "external", "name": "external", "url": "http://192.0.2.10/feed.xml",
            "format": "auto", "enabled": True, "tags": [], "note": "",
        }],
    }), encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    issue = next(issue for issue in raised.value.issues if issue.code == "CFG_003")
    assert issue.path == "feeds[0].url"
    assert "白名单" in issue.message


def test_rejects_short_interval(write_policy):
    with pytest.raises(ConfigError) as raised:
        load_policy(write_policy(min_interval_ms=99))
    assert "CFG_013" in _codes(raised.value)


def test_rejects_external_policy_host(write_policy):
    with pytest.raises(ConfigError) as raised:
        load_policy(write_policy(allowed_hosts=["127.0.0.1", "example.test"]))
    assert "CFG_012" in _codes(raised.value)


def test_rejects_unknown_fields(tmp_path, write_policy):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "feeds": [{
            "id": "x", "name": "x", "url": "http://127.0.0.1:8765/feeds/rss-basic.xml",
            "format": "auto", "enabled": True, "tags": [], "note": "", "cookie": "no",
        }],
    }), encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    assert any(issue.code == "CFG_008" and issue.path.endswith(".cookie")
               for issue in raised.value.issues)


def test_rejects_bad_format_and_types(tmp_path, write_policy):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "feeds": [{
            "id": "x", "name": "x", "url": "http://127.0.0.1:8765/feeds/rss-basic.xml",
            "format": "html", "enabled": "yes", "tags": "tag", "note": 1,
        }],
    }), encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    codes = _codes(raised.value)
    assert "CFG_009" in codes and "CFG_005" in codes


def test_policy_defaults_and_type_errors(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("timeout_seconds: 2.5\nmax_retries: 0\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.timeout_seconds == 2.5
    assert policy.max_retries == 0
    assert policy.min_interval_ms == 200
    path.write_text("timeout_seconds: true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(path)


def test_load_config_convenience(tmp_path, mock_server, source_factory, write_feeds, write_policy):
    feeds_path = write_feeds([source_factory(mock_server)], "feeds.yaml")
    policy_path = write_policy()
    feeds, policy = load_config(feeds_path, policy_path)
    assert feeds[0].id == "rss-basic"
    assert isinstance(policy, object)


def test_validation_mentions_file_and_path(tmp_path, write_feeds, source_factory, write_policy):
    external = FeedSource(
        id="bad", name="bad", url="http://198.51.100.8/feed.xml", format="auto",
        enabled=True, tags=(), note="",
    )
    path = write_feeds([external])
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    rendered = str(raised.value)
    assert "CFG_003" in rendered
    assert "feeds.yaml" in rendered
    assert "feeds[0].url" in rendered


def test_retention_mode_defaults_to_retain(tmp_path, write_feeds, source_factory,
                                           mock_server, write_policy):
    path = write_feeds([source_factory(mock_server)])
    feeds = load_feeds(path, load_policy(write_policy()))
    assert feeds[0].retention_mode == "retain_retracted_content"


def test_retention_mode_accepts_erase(tmp_path, write_feeds, source_factory,
                                      mock_server, write_policy):
    import yaml
    path = write_feeds([source_factory(mock_server)])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["feeds"][0]["retention_mode"] = "erase_retracted_content"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    feeds = load_feeds(path, load_policy(write_policy()))
    assert feeds[0].retention_mode == "erase_retracted_content"


def test_retention_mode_rejects_unknown_value(tmp_path, write_feeds, source_factory,
                                              mock_server, write_policy):
    import yaml
    path = write_feeds([source_factory(mock_server)])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["feeds"][0]["retention_mode"] = "shred-everything"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_feeds(path, load_policy(write_policy()))
    assert any(
        issue.code == "CFG_009" and issue.path.endswith("retention_mode")
        for issue in raised.value.issues
    )
