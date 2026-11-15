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
    identity_contract: id_first   # 可选，id_first（默认）或 link_first
```

`identity_contract` 声明该源的条目身份契约：

- `id_first`：发布者 ID 优先（id -> 链接 -> title+日期）。同一链接下两个不同 ID 保留为两个业务条目；
- `link_first`：规范链接优先（链接 -> id -> title+日期）。发布者轮换 ID 但链接不变时，文章保持一条连续历史；
- 两条路线在 id 与链接都缺失时都回退到 `title+published_at`。

切换已归档源的契约前，先用迁移工具生成只读冲突清单，再显式应用，见下文 `identity-plan` / `identity-migrate`。

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
| `python -m feedkeep identity-plan --config ... [--source ID ...] [--json] [--out ...]` | 只读生成身份契约迁移冲突清单，不改动数据库 |
| `python -m feedkeep identity-migrate --config ... [--source ID ...] [--json] [--out ...]` | 显式应用身份契约迁移；有冲突时整体拒绝 |
| `python -m feedkeep demo [--out ...] [--port ...]` | 离线自检演示 |

迁移工作流：先改 `feeds.yaml` 中目标源的 `identity_contract`，运行 `identity-plan` 查看当前契约、待迁移条目与阻塞冲突（一对多、多对一、内容合并、台账漂移都会被阻止并列出旧/新键与原因）；清单无冲突后运行 `identity-migrate`。迁移保留 `first_seen` / `last_changed` / 缺失状态与完整事件链，为每个改名条目追加 `identity_changed` 事件；中断后重复运行幂等，且不改变原始素材。

| 退出码 | 含义 |
|---|---|
| `0` | 成功 / 全部来源未修改 / 迁移计划无冲突 |
| `1` | 部分来源失败（run 状态 `partial`） |
| `2` | 全部失败、无启用来源或没有可报告记录 |
| `3` | 配置 / 策略校验失败 |
| `4` | 身份迁移存在阻塞冲突（未改动任何数据） |
| `130` | 用户中断 |

## 目录

```text
configs/          源与策略样例
feedkeep/         采集、解析、归档、生命周期、报告、CLI
mockserver/       回环 mock 发布服务 + 静态 fixture
docs/             设计、合规边界、报告字段字典
reports/          运行产物（内容被 .gitignore 忽略）
tests/            离线测试（身份、迁移、解析、归档、端到端）
```

## 关键保证

- **出网边界**：请求前校验 URL 白名单，不使用系统代理；重定向目标也做同样校验；非白名单主机不会触发 DNS。
- **请求边界**：连接/读取超时、响应体上限、单源条目上限、同主机最小间隔、重试上限。
- **格式边界**：XML 显式拒绝 `DOCTYPE` / `ENTITY`；JSON 仅接受 UTF-8；三格式统一条目模型。
- **归档幂等**：条目身份按源声明的契约生成——`id_first` 走 id -> 链接 -> title+日期，`link_first` 走链接 -> id -> title+日期；同内容重复出现只更新 `last_seen_at`。
- **身份迁移安全**：契约切换先出只读冲突清单再显式应用；非一对一映射一律阻止，不丢内容、不任选胜者；迁移保留全部生命周期状态与事件链，重复执行幂等。
- **生命周期**：`discovered` / `updated` / `missing` / `restored` / `identity_changed` 事件；成功抓取且未被截断时才做缺失对账。
- **可审计**：每个 run、每个 source_run、每次条目级错误、每个生命周期事件都落库；报告数组有固定排序规则。
- **可复现**：fixture 由脚本生成并有 sha256 清单；注入时钟后，测试不比较真实时间。

进一步说明见：

- [docs/DESIGN.md](docs/DESIGN.md)：数据模型、身份与指纹、状态机、重试语义；
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md)：合规边界与使用范围；
- [docs/REPORTER.md](docs/REPORTER.md)：报告字段字典与排序规则。
