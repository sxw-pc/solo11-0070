"""Deterministically regenerate and verify the bundled mock fixtures."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "mockserver" / "fixtures"
BASE = "http://127.0.0.1:8765"
MANIFEST = "manifest.json"


def _xml(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + body.strip() + "\n").encode("utf-8")


def _json(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _rss(body: str) -> bytes:
    return _xml(
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        f'  <channel>\n    <title>示例技术公告</title>\n    <link>{BASE}/feeds/rss-basic.xml</link>\n'
        '    <description>feedkeep 合成 RSS 订阅源</description>\n'
        + body + "  </channel>\n</rss>"
    )


def _rss_items(include_fourth: bool = False, revised: bool = False) -> str:
    title_one = "示例公告一：发布窗口（修订）" if revised else "示例公告一：发布窗口"
    summary_one = "修订后的发布窗口说明。" if revised else "第一条合成公告的摘要。"
    body = f"""    <item>
      <title>{title_one}</title>
      <link>{BASE}/articles/rss-1</link>
      <guid isPermaLink="false">urn:feedkeep:rss-basic:1</guid>
      <pubDate>Mon, 01 Jan 2024 08:00:00 GMT</pubDate>
      <description>{summary_one}</description>
      <content:encoded><![CDATA[<p>正文一，仅用于离线测试。</p>]]></content:encoded>
      <author>示例编辑甲</author>
      <category>公告</category>
      <enclosure url="{BASE}/files/rss-1.pdf" type="application/pdf" length="1024" />
    </item>
    <item>
      <title>示例公告二：维护说明</title>
      <link>{BASE}/articles/rss-2</link>
      <guid isPermaLink="false">urn:feedkeep:rss-basic:2</guid>
      <pubDate>Wed, 03 Jan 2024 09:30:00 GMT</pubDate>
      <description>第二条合成公告的摘要。</description>
      <enclosure url="{BASE}/files/rss-2.txt" type="text/plain" length="128" />
    </item>
    <item>
      <title>示例公告三：状态页</title>
      <link>{BASE}/articles/rss-3</link>
      <guid isPermaLink="false">urn:feedkeep:rss-basic:3</guid>
      <pubDate>Fri, 05 Jan 2024 12:00:00 GMT</pubDate>
      <description>第三条合成公告的摘要。</description>
    </item>
"""
    if include_fourth:
        body += f"""    <item>
      <title>示例公告四：新增条目</title>
      <link>{BASE}/articles/rss-4</link>
      <guid isPermaLink="false">urn:feedkeep:rss-basic:4</guid>
      <pubDate>Sun, 07 Jan 2024 10:00:00 GMT</pubDate>
      <description>第四条合成公告的摘要。</description>
    </item>
"""
    return body


def _atom_entries(updated: bool = False) -> str:
    alpha_title = "示例 Atom 条目甲（修订）" if updated else "示例 Atom 条目甲"
    alpha_content = "修订后的 Atom 正文。" if updated else "Atom 正文甲。"
    body = f"""  <entry>
    <id>urn:feedkeep:atom:alpha</id>
    <title>{alpha_title}</title>
    <updated>2024-01-02T08:00:00Z</updated>
    <published>2024-01-01T08:00:00Z</published>
    <link rel="alternate" href="{BASE}/articles/atom-alpha" />
    <link rel="enclosure" href="{BASE}/files/atom-alpha.pdf" type="application/pdf" length="2048" />
    <author><name>示例编辑乙</name></author>
    <category term="公告" />
    <summary>Atom 摘要甲。</summary>
    <content type="html">{alpha_content}</content>
  </entry>
  <entry>
    <id>urn:feedkeep:atom:beta</id>
    <title>示例 Atom 条目乙</title>
    <updated>2024-01-03T10:30:00Z</updated>
    <link href="{BASE}/articles/atom-beta" />
    <summary>Atom 摘要乙。</summary>
  </entry>
"""
    if not updated:
        body += f"""  <entry>
    <id>urn:feedkeep:atom:gamma</id>
    <title>示例 Atom 条目丙</title>
    <updated>2024-01-05T12:00:00Z</updated>
    <published>2024-01-04T12:00:00Z</published>
    <link rel="alternate" href="{BASE}/articles/atom-gamma" />
    <summary>Atom 摘要丙。</summary>
  </entry>
"""
    else:
        body += f"""  <entry>
    <id>urn:feedkeep:atom:delta</id>
    <title>示例 Atom 条目丁（新增）</title>
    <updated>2024-01-07T12:00:00Z</updated>
    <link rel="alternate" href="{BASE}/articles/atom-delta" />
    <summary>Atom 摘要丁。</summary>
  </entry>
"""
    return body


def _atom(body: str) -> bytes:
    return _xml(
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>示例 Atom 发布源</title>\n"
        f"  <id>urn:feedkeep:atom-feed</id>\n  <updated>2024-01-07T12:00:00Z</updated>\n"
        f'  <link rel="self" href="{BASE}/feeds/atom-basic.xml" />\n'
        + body + "</feed>"
    )


def _jf_items(updated: bool = False) -> list[dict[str, Any]]:
    first_content = "<p>修订后的 JSON Feed 正文。</p>" if updated else "<p>JSON Feed 正文一。</p>"
    items = [
        {"id": "jf-1", "url": f"{BASE}/articles/jf-1",
         "title": "示例 JSON Feed 条目一（修订）" if updated else "示例 JSON Feed 条目一",
         "content_html": first_content, "summary": "JSON Feed 摘要一。",
         "date_published": "2024-01-02T08:00:00+00:00",
         "authors": [{"name": "示例编辑丙"}], "tags": ["公告", "jsonfeed"],
         "attachments": [{"url": f"{BASE}/files/jf-1.pdf",
                          "mime_type": "application/pdf", "size_in_bytes": 4096}]},
        {"id": "jf-2", "url": f"{BASE}/articles/jf-2",
         "title": "示例 JSON Feed 条目二", "content_text": "JSON Feed 正文二。",
         "date_published": "2024-01-04T09:00:00Z", "tags": ["状态"]},
    ]
    if updated:
        items.append({"id": "jf-4", "url": f"{BASE}/articles/jf-4",
                      "title": "示例 JSON Feed 条目四（新增）",
                      "content_text": "JSON Feed 正文四。",
                      "date_published": "2024-01-08T09:00:00Z"})
    else:
        items.append({"id": "jf-3", "url": f"{BASE}/articles/jf-3",
                      "title": "示例 JSON Feed 条目三",
                      "content_text": "JSON Feed 正文三。",
                      "date_published": "2024-01-06T09:00:00Z"})
    return items

def _json_feed(updated: bool = False) -> bytes:
    return _json({
        "version": "https://jsonfeed.org/version/1.1",
        "title": "示例 JSON Feed 发布源",
        "home_page_url": f"{BASE}/jsonfeed",
        "feed_url": f"{BASE}/feeds/jsonfeed-basic.json",
        "items": _jf_items(updated),
    })


def _mixed_atom() -> bytes:
    return _xml(
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>条目级故障隔离示例</title>\n  <id>urn:feedkeep:atom-mixed</id>\n"
        "  <updated>2024-01-10T00:00:00Z</updated>\n"
        f"""  <entry>
    <id>urn:feedkeep:mixed:good-1</id><title>有效条目一</title>
    <updated>2024-01-09T08:00:00Z</updated>
    <link href="{BASE}/articles/mixed-1" />
  </entry>
  <entry>
    <title>缺少 id 的条目</title><updated>2024-01-09T09:00:00Z</updated>
    <link href="{BASE}/articles/mixed-bad-id" />
  </entry>
  <entry>
    <id>urn:feedkeep:mixed:good-2</id><title>有效条目二</title>
    <updated>2024-01-09T10:00:00Z</updated>
    <link href="{BASE}/articles/mixed-2" />
  </entry>
  <entry>
    <id>urn:feedkeep:mixed:no-time</id><title>缺少时间的条目</title>
    <link href="{BASE}/articles/mixed-no-time" />
  </entry>
  <entry>
    <id>urn:feedkeep:mixed:bad-time</id><title>时间无效的条目</title>
    <updated>not-an-iso-date</updated>
    <link href="{BASE}/articles/mixed-bad-time" />
  </entry>
"""
        "</feed>"
    )


def _odd_json() -> bytes:
    items = [
        {"id": 123, "url": f"{BASE}/articles/odd-1", "title": 456,
         "date_published": "2024-01-11T00:00:00Z",
         "attachments": [{"url": f"{BASE}/files/odd.bin", "mime_type": None,
                          "size_in_bytes": "2048"}]},
        {"url": f"{BASE}/articles/odd-2", "title": "无 id 但有链接",
         "date_published": "2024-01-12T00:00:00Z"},
        {"id": {"bad": "object"}, "title": "id 类型非法"},
        {"id": "odd-bad-date", "title": "日期非法", "date_published": "not-a-date"},
        {"title": "无 id 但可回退", "date_published": "2024-01-13T00:00:00Z",
         "attachments": [{"url": "javascript:alert(1)", "size_in_bytes": 1}]},
    ]
    return _json({"version": "https://jsonfeed.org/version/1.1",
                  "title": "奇值 JSON Feed", "items": items})

def generate_all() -> dict[str, bytes]:
    return {
        "rss_basic.xml": _rss(_rss_items()),
        "rss_updated.xml": _rss(_rss_items(include_fourth=True, revised=True)),
        "atom_basic.xml": _atom(_atom_entries()),
        "atom_updated.xml": _atom(_atom_entries(updated=True)),
        "jsonfeed_basic.json": _json_feed(),
        "jsonfeed_updated.json": _json_feed(updated=True),
        "rss_malformed.xml": _xml(
            "<rss version=\"2.0\"><channel><title>坏源</title><item>"
            f"<title>oops</title><link>{BASE}/articles/broken</link>"
            "</description></channel>"
        ),
        "atom_mixed_bad_items.xml": _mixed_atom(),
        "jsonfeed_odd_values.json": _odd_json(),
    }


def manifest_for(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}


def _manifest_bytes(files: dict[str, bytes]) -> bytes:
    return _json(manifest_for(files))


def write_fixtures() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    files = generate_all()
    for name, data in files.items():
        (FIXTURE_DIR / name).write_bytes(data)
    (FIXTURE_DIR / MANIFEST).write_bytes(_manifest_bytes(files))


def check_fixtures() -> list[str]:
    files = generate_all()
    problems: list[str] = []
    for name, expected in files.items():
        path = FIXTURE_DIR / name
        if not path.exists():
            problems.append(f"missing {name}")
        elif path.read_bytes() != expected:
            problems.append(f"drift {name}")
    manifest_path = FIXTURE_DIR / MANIFEST
    if not manifest_path.exists():
        problems.append("missing manifest.json")
    elif manifest_path.read_bytes() != _manifest_bytes(files):
        problems.append("drift manifest.json")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--write"]:
        write_fixtures()
        print(f"wrote {len(generate_all())} fixtures to {FIXTURE_DIR}")
        return 0
    if args == ["--check"]:
        problems = check_fixtures()
        if problems:
            print("fixture check failed: " + ", ".join(problems), file=sys.stderr)
            return 1
        print("fixtures ok")
        return 0
    print("usage: python -m feedkeep.mockdata [--write|--check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
