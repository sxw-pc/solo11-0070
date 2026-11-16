# 流订台（feedkeep）

一个**纯本地、完全离线、配置驱动**的发布订阅源增量采集与条目归档引擎。支持 RSS 2.0、Atom 1.0、JSON Feed 1.1，把条件请求、多格式解析、归一化、条目级幂等归档、生命周期追踪与可审计报告收敛为可重复执行的命令。

## 它解决什么

- 同一批条目被反复抓取、反复入库；
- 订阅源坏掉时不知道坏在哪个源、哪个字段；
- 条目悄悄被修订却没有变更记录；
- 每次重跑报告顺序不稳、数字对不上；
- 评测或 CI 环境不能访问外网。

feedkeep 以“源”管理 ETag / Last-Modified，以“条目”管理稳定身份、内容指纹与生命周期，以“run”管理一次可审计的抓取记录。

## 明确不做什么

- 不抓 HTML 列表页，不配置 DOM 选择器，不建站内链接图；
- 不做网页快照、视觉回归或截图；
- 不下载 enclosure / 附件的二进制内容，只归档 URL、MIME 与声明长度；
- 不做常驻调度器、分布式队列、搜索索引或 Web UI。

## 快速开始

要求 Python 3.10+、PyYAML，测试另需 pytest。

```bash
pip install -e ".[dev]"
```

不安装也可在仓库根目录直接运行，源码布局本身可导入。

### 一条命令自检

```bash
python -m feedkeep demo
```

该命令会：

1. 校验 9 个 fixture 的 sha256 清单；
2. 在回环地址上自动启动 mock 发布服务；
3. 执行两轮抓取（第二轮验证条件请求 304）；
4. 生成 JSON / Markdown 报告；
5. 执行条目级错误隔离、坏源隔离与幂等断言；
6. 自动关闭服务并退出。

报告写入 `reports/demo/`，第一轮报告另存于 `reports/demo/first-run/`。`configs/feeds.yaml` 中的 `rss-malformed` 是**故意配置的坏源**，因此正常 run 状态是 `partial`，demo 仍以退出码 0 表示自检通过。

### 手动跑一轮

终端 A：

```bash
python -m mockserver.server
# mock feed server listening on http://127.0.0.1:8765
```

终端 B：

```bash
python -m feedkeep fetch --config configs/feeds.yaml
python -m feedkeep report --out reports/
```

也可以在默认策略下自定义数据库：

```bash
python -m feedkeep fetch --config configs/feeds.yaml --db data/archive.sqlite3
python -m feedkeep report --db data/archive.sqlite3 --out reports/
```

### 测试

```bash
pytest -q
```

测试只访问 `127.0.0.1`，由 pytest fixture 自行拉起 mock 服务，不依赖启动顺序或外网。

## 配置

`configs/feeds.yaml`：

```yaml
version: 1
feeds:
  - id: rss-basic
    name: 示例 RSS 基础源
    url: http://127.0.0.1:8765/feeds/rss-basic.xml
    format: auto          # auto / rss / atom / jsonfeed
    enabled: true
    tags: [rss, demo]
    note: 合成源说明
    revision_order: observed_order   # 可选，默认 observed_order
```

`revision_order` 仅对 Atom 源生效，控制镜像/副本响应倒退时如何保护当前条目版本：

- `observed_order`（默认）：每次完整有效观察即为权威，内容可随响应回滚，与旧版本行为完全一致；
- `source_revision_order`：以 Atom `updated` 作为每个稳定 `item_key` 的单调高水位，更早的观察记为 `stale_revision` 事件而不覆盖当前内容，同一 `updated` 时间却出现不同 hash 记为 `revision_conflict`；被拒绝的观察仍刷新 `last_seen_at`，不会触发 missing。RSS / JSON Feed 始终沿用观察顺序语义。

`configs/policy.yaml` 控制白名单与请求边界：

```yaml
version: 1
allowed_hosts: [127.0.0.1, localhost, "::1"]
timeout_seconds: 5
max_body_bytes: 524288
max_items_per_feed: 200
min_interval_ms: 200
max_retries: 2
backoff_base_ms: 250
```

白名单只接受回环地址；`min_interval_ms` 不得低于 100。配置错误使用稳定错误码（如 `CFG_003`）并带文件名与字段路径。

## CLI 与退出码

| 命令 | 作用 |
|---|---|
| `python -m feedkeep fetch --config ... [--policy ...] [--db ...] [--quiet]` | 执行一轮抓取 |
| `python -m feedkeep report [--db ...] [--out ...] [--run-id ...]` | 生成最近一轮或指定 run 的报告 |
| `python -m feedkeep demo [--out ...] [--port ...]` | 离线自检演示 |

| 退出码 | 含义 |
|---|---|
| `0` | 成功 / 全部来源未修改 |
| `1` | 部分来源失败（run 状态 `partial`） |
| `2` | 全部失败、无启用来源或没有可报告记录 |
| `3` | 配置 / 策略校验失败 |
| `130` | 用户中断 |

## 目录

```text
configs/          源与策略样例
feedkeep/         采集、解析、归档、生命周期、报告、CLI
mockserver/       回环 mock 发布服务 + 静态 fixture
docs/             设计、合规边界、报告字段字典
reports/          运行产物（内容被 .gitignore 忽略）
tests/            105 项离线测试
```

## 关键保证

- **出网边界**：请求前校验 URL 白名单，不使用系统代理；重定向目标也做同样校验；非白名单主机不会触发 DNS。
- **请求边界**：连接/读取超时、响应体上限、单源条目上限、同主机最小间隔、重试上限。
- **格式边界**：XML 显式拒绝 `DOCTYPE` / `ENTITY`；JSON 仅接受 UTF-8；三格式统一条目模型。
- **归档幂等**：条目身份优先使用发布方 id，其次规范化链接，最后 title+发布时间；同内容重复出现只更新 `last_seen_at`。
- **生命周期**：`discovered` / `updated` / `missing` / `restored` 事件；成功抓取且未被截断时才做缺失对账。
- **可审计**：每个 run、每个 source_run、每次条目级错误、每个生命周期事件都落库；报告数组有固定排序规则。
- **可复现**：fixture 由脚本生成并有 sha256 清单；注入时钟后，测试不比较真实时间。

进一步说明见：

- [docs/DESIGN.md](docs/DESIGN.md)：数据模型、身份与指纹、状态机、重试语义；
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md)：合规边界与使用范围；
- [docs/REPORTER.md](docs/REPORTER.md)：报告字段字典与排序规则。
