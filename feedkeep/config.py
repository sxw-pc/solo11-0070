"""Strict YAML loading with stable, field-addressable validation errors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import (
    DEFAULT_REVISION_ORDER,
    REVISION_ORDER_STRATEGIES,
    FeedSource,
)
from .policy import LOOPBACK_HOSTS, Policy, PolicyViolation, normalize_allowed_hosts

FORMATS = ("auto", "rss", "atom", "jsonfeed")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FEED_KEYS = ("id", "name", "url", "format", "enabled", "tags", "note")
# Optional keys keep their own allow-list so the required-field check above
# continues to demand exactly the legacy FEED_KEYS.
FEED_OPTIONAL_KEYS = ("revision_order",)
FEED_ALLOWED_KEYS = FEED_KEYS + FEED_OPTIONAL_KEYS
POLICY_KEYS = (
    "version", "allowed_hosts", "timeout_seconds", "max_body_bytes",
    "max_items_per_feed", "min_interval_ms", "max_retries", "backoff_base_ms",
)


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    file: str
    path: str
    message: str

    def __str__(self) -> str:
        location = f"{self.file}:{self.path}" if self.path else self.file
        return f"{self.code} {location}: {self.message}"


class ConfigError(Exception):
    def __init__(self, issues: Iterable[ConfigIssue]):
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


def _issue(code: str, path: Path, pointer: str, message: str) -> ConfigIssue:
    return ConfigIssue(code, str(path), pointer, message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_mapping(path: Path, allow_empty: bool = False) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([_issue("CFG_001", path, "", f"无法读取文件: {exc}")]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([_issue("CFG_002", path, "", f"YAML 语法错误: {exc}")]) from exc
    if data is None and allow_empty:
        return {}
    if not isinstance(data, dict):
        raise ConfigError([_issue("CFG_002", path, "", "顶层必须是映射对象")])
    return data


def _unknown_keys(data: dict[str, Any], allowed: Iterable[str], path: Path, prefix: str) -> list[ConfigIssue]:
    allowed_set = set(allowed)
    return [
        _issue("CFG_008", path, f"{prefix}{key}" if not prefix else f"{prefix}.{key}", "未知字段")
        for key in data
        if key not in allowed_set
    ]


def load_policy(path: str | Path) -> Policy:
    source = Path(path)
    data = _read_mapping(source, allow_empty=True)
    issues = _unknown_keys(data, POLICY_KEYS, source, "")
    defaults = Policy()
    version = data.get("version", 1)
    if version != 1 or not _is_int(version):
        issues.append(_issue("CFG_009", source, "version", "目前只支持 version: 1"))

    raw_hosts = data.get("allowed_hosts", list(defaults.allowed_hosts))
    hosts = defaults.allowed_hosts
    if not isinstance(raw_hosts, list) or not raw_hosts or not all(isinstance(h, str) and h for h in raw_hosts):
        issues.append(_issue("CFG_011", source, "allowed_hosts", "必须是非空字符串列表"))
    else:
        hosts = normalize_allowed_hosts(raw_hosts)
        bad_hosts = [h for h in hosts if h not in LOOPBACK_HOSTS]
        if bad_hosts:
            issues.append(_issue(
                "CFG_012", source, "allowed_hosts",
                f"只允许回环主机 {list(LOOPBACK_HOSTS)!r}，收到 {bad_hosts!r}",
            ))

    def number(name: str, default: Any, code: str, predicate: Any, detail: str) -> Any:
        if name not in data:
            return default
        value = data[name]
        if not _is_number(value) or not predicate(value):
            issues.append(_issue(code, source, name, detail))
            return default
        return value

    timeout = number("timeout_seconds", defaults.timeout_seconds, "CFG_011",
                     lambda v: 0 < v <= 120, "必须是 (0, 120] 秒内的数字")
    body = number("max_body_bytes", defaults.max_body_bytes, "CFG_011",
                  lambda v: _is_int(v) and v >= 1, "必须是正整数")
    items = number("max_items_per_feed", defaults.max_items_per_feed, "CFG_011",
                   lambda v: _is_int(v) and 1 <= v <= 10000, "必须是 1..10000 的整数")
    interval = number("min_interval_ms", defaults.min_interval_ms, "CFG_013",
                      lambda v: _is_int(v) and 100 <= v <= 60000, "不得低于 100ms 且不超过 60000ms")
    retries = number("max_retries", defaults.max_retries, "CFG_011",
                     lambda v: _is_int(v) and 0 <= v <= 3, "必须是 0..3 的整数")
    backoff = number("backoff_base_ms", defaults.backoff_base_ms, "CFG_011",
                     lambda v: _is_int(v) and 0 <= v <= 60000, "必须是 0..60000 的整数")
    if issues:
        raise ConfigError(issues)
    return Policy(
        allowed_hosts=tuple(hosts),
        timeout_seconds=float(timeout),
        max_body_bytes=int(body),
        max_items_per_feed=int(items),
        min_interval_ms=int(interval),
        max_retries=int(retries),
        backoff_base_ms=int(backoff),
    )


def _validate_feed(raw: Any, index: int, path: Path, policy: Policy) -> tuple[FeedSource | None, list[ConfigIssue]]:
    prefix = f"feeds[{index}]"
    issues: list[ConfigIssue] = []
    if not isinstance(raw, dict):
        return None, [_issue("CFG_005", path, prefix, "每个订阅源必须是映射对象")]
    issues.extend(_unknown_keys(raw, FEED_ALLOWED_KEYS, path, prefix))
    for key in FEED_KEYS:
        if key not in raw:
            issues.append(_issue("CFG_004", path, f"{prefix}.{key}", "缺少必填字段"))
    if issues:
        return None, issues

    feed_id, name, url = raw["id"], raw["name"], raw["url"]
    fmt, enabled, tags, note = raw["format"], raw["enabled"], raw["tags"], raw["note"]
    if not isinstance(feed_id, str) or not ID_PATTERN.fullmatch(feed_id):
        issues.append(_issue("CFG_007", path, f"{prefix}.id", "必须是 slug（小写字母/数字/-/_）"))
    if not isinstance(name, str) or not name.strip():
        issues.append(_issue("CFG_005", path, f"{prefix}.name", "必须是非空字符串"))
    if not isinstance(url, str) or not url:
        issues.append(_issue("CFG_003", path, f"{prefix}.url", "必须是非空字符串"))
    else:
        try:
            policy.assert_url_allowed(url)
        except PolicyViolation as exc:
            issues.append(_issue("CFG_003", path, f"{prefix}.url", exc.message))
    if fmt not in FORMATS:
        issues.append(_issue("CFG_009", path, f"{prefix}.format", f"必须是 {FORMATS!r} 之一"))
    if not isinstance(enabled, bool):
        issues.append(_issue("CFG_005", path, f"{prefix}.enabled", "必须是布尔值"))
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        issues.append(_issue("CFG_005", path, f"{prefix}.tags", "必须是字符串列表"))
    if not isinstance(note, str):
        issues.append(_issue("CFG_005", path, f"{prefix}.note", "必须是字符串"))
    revision_order = raw.get("revision_order", DEFAULT_REVISION_ORDER)
    if revision_order not in REVISION_ORDER_STRATEGIES:
        issues.append(_issue(
            "CFG_009", path, f"{prefix}.revision_order",
            f"必须是 {REVISION_ORDER_STRATEGIES!r} 之一",
        ))
    if issues:
        return None, issues
    return FeedSource(
        id=feed_id,
        name=name.strip(),
        url=url,
        format=fmt,
        enabled=enabled,
        tags=tuple(dict.fromkeys(tags)),
        note=note,
        revision_order=revision_order,
    ), []


def load_feeds(path: str | Path, policy: Policy) -> list[FeedSource]:
    source = Path(path)
    data = _read_mapping(source)
    issues = _unknown_keys(data, ("version", "feeds"), source, "")
    if "version" in data and data["version"] != 1:
        issues.append(_issue("CFG_009", source, "version", "目前只支持 version: 1"))
    raw_feeds = data.get("feeds")
    if not isinstance(raw_feeds, list) or not raw_feeds:
        issues.append(_issue("CFG_010", source, "feeds", "必须是非空列表"))
        raise ConfigError(issues)
    feeds: list[FeedSource] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_feeds):
        feed, feed_issues = _validate_feed(raw, index, source, policy)
        issues.extend(feed_issues)
        if feed is not None:
            if feed.id in seen:
                issues.append(_issue("CFG_006", source, f"feeds[{index}].id", f"重复 id: {feed.id}"))
            else:
                seen.add(feed.id)
                feeds.append(feed)
    if issues:
        raise ConfigError(issues)
    return feeds


def load_config(
    feeds_path: str | Path,
    policy_path: str | Path = "configs/policy.yaml",
) -> tuple[list[FeedSource], Policy]:
    """Convenience loader for callers that keep both files together."""

    policy = load_policy(policy_path)
    return load_feeds(feeds_path, policy), policy
