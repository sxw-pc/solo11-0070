# feedkeep 设计说明

## 1. 总体流程

```text
feeds.yaml + policy.yaml
        |
        v
  严格 schema 校验
        |
        v
  Fetcher（白名单 -> 限速 -> 条件请求 -> 重试 -> 字节上限）
        |
        v
  detect_format -> rss / atom / jsonfeed parser
        |
        v
  条目级容错 + identity + content fingerprint
        |
        v
  Store（幂等 upsert + 生命周期事件 + run 审计）
        |
        v
  report（JSON / Markdown，固定排序）
```

一次 CLI 调用只处理一轮。没有常驻循环、后台队列或跨机状态。

## 2. 数据模型

SQLite 使用单文件、`foreign_keys=ON`、`journal_mode=DELETE`（关闭 WAL）、`synchronous=FULL`。`schema_meta.schema_version` 记录结构版本；当前为 2（v1 库打开时自动做保留数据的 additive 升级：事件表重建以容纳 `identity_changed` 并允许空 `run_id`、sources 增加 `identity_contract`、新建 `identity_migrations`），遇到更高版本直接拒绝，避免旧二进制写坏新库。

### `sources`

每个配置源的持久状态：

- 配置快照：`name/url/format/enabled/tags_json/note`；
- 条件请求缓存：`etag/last_modified`；
- 运行状态：`last_checked_at/last_success_at/consecutive_failures`。

### `runs`

一次抓取命令的总账：

- `id`：形如 `run-000001`，按已有最大值递增，便于稳定引用；
- `started_at/finished_at/status`；
- 汇总计数：来源成功/未修改/失败、条目 seen/new/updated/missing/restored、条目级错误数；
- `config_path`：便于审计本轮使用的配置路径。

### `source_runs`

每个源在本轮的明细，保留源名快照，避免后续改名影响历史审计：

- 状态：`ok` / `not_modified` / `failed`；
- HTTP 状态、总耗时、响应字节数、响应体 sha256、ETag、Last-Modified；
- 实际 HTTP 尝试次数 `attempts`；
- 条目计数、条目错误数、是否截断；
- `error_code/error_message/observed_at`。

一个响应体过大时，字节数记录为服务端声明或实际观察到的值；内容被丢弃，因此 sha256 为空。304 没有响应体，sha256 也为空。

### `items`

条目账本，主键 `(source_id, item_key)`：

- 身份与内容：`item_key/raw_id/title/link/summary/content/published_at`；
- 作者、分类：排序稳定的 JSON 数组；
- 内容指纹：`content_hash`；
- 生命周期：`first_seen_at/last_seen_at/last_changed_at/missing_since`；
- 来源 run：`first_run_id/last_run_id`。

### `item_enclosures`

每个条目可有多条附件元数据，按 `position` 排序。只保存 URL、MIME、声明长度，**从不下载二进制**。

### `item_events`

允许五类事件：

- `discovered`：首次出现；
- `updated`：内容指纹改变，detail 包含变更字段与新/旧指纹；
- `missing`：一次完整成功抓取中未再出现；
- `restored`：此前缺失的条目重新出现；
- `identity_changed`：身份契约迁移把条目改名到新 `item_key`，detail 含旧键、新键与前后契约；该事件不属于抓取 run，`run_id` 为空。

表上有 `(source_id,item_key,run_id,event_type)` 唯一约束，防止同一 run 重放产生重复事件（`run_id` 为空时 SQLite 唯一约束允许每个事件类型各一条）。

### `identity_migrations`

身份契约迁移台账，每个源每次契约切换一行（`(source_id,from_contract,to_contract)` 唯一）：迁移条目数与应用时间。它是判断库内现有键实际遵循哪个契约的权威来源；重复执行迁移时据此报告“已对齐”并跳过改名。

### `item_issues`

条目级错误与 run 绑定，包含原始条目序号、稳定错误码、消息。整个源解析失败不会进入这里，而是记录在 `source_runs.error_code`。

## 3. 条目身份

### 3.1 身份契约

每个源在 `feeds.yaml` 中通过可选字段 `identity_contract` 显式声明身份契约，缺省为 `id_first`：

- `id_first`（发布者 ID 优先）：显式 id -> 规范链接 -> `title+published_at`。同一规范链接下两个不同 id 的版本是**两个业务条目**，发布方改 id 即新条目；
- `link_first`（规范链接优先）：规范链接 -> 显式 id -> `title+published_at`。同一文章链接在发布方轮换 id 后仍是**一条连续历史**。

两条路线在两个主字段都缺失时都回退到 `title+published_at`，三者皆无时产生条目级错误 `ITEM_NO_IDENTITY`。契约只决定 `item_key` 的选取顺序，不改变内容指纹；`sources.identity_contract` 保存配置声明的目标契约，库内现有键实际遵循的契约以 `identity_migrations` 台账为准。

身份按所选契约的优先级生成，只有得到身份后才可入库：

1. 发布方显式 id（RSS `guid`、Atom `id`、JSON Feed `id`）；
2. 规范化链接（scheme/host 小写、去掉默认端口与 fragment、保留 path/query）；
3. `title + published_at` 组合。

生成规则：

```text
item_key = "id:"   + sha256(raw_id)[:32]
         | "link:" + sha256(normalized_link)[:32]
         | "td:"   + sha256(title.casefold() + separator + date)[:32]
```

- 显式 id 只做首尾空白与 NFC 归一化；
- 同一响应内两个条目得到相同 key 时，保留第一条，第二条记 `ITEM_DUPLICATE_KEY`；
- 主键同时含 source_id，因此不同来源的相同 id 不会冲突。

### 3.2 身份契约迁移

直接改变某源的契约会让旧 `item_key` 留在库中、新键被当作新条目，并可能把旧条目同时记为 missing。因此契约切换必须经过显式迁移工具，分两步执行：

1. `feedkeep identity-plan`：**只读**重算每个条目的新键，生成冲突清单，不写数据库；
2. `feedkeep identity-migrate`：仅当清单无阻塞冲突时才应用，否则整体拒绝、退出码 4、不动任何数据。

规划器只接受**无歧义的一对一映射**。以下情况一律阻止自动迁移，报告给出 `source_id`、旧/新键与原因，绝不丢内容或任选胜者：

| 冲突码 | 触发条件 |
|---|---|
| `IDM_MANY_OLD_ONE_NEW_CONTENT` | 多个旧身份映射同一新身份且内容指纹不同（合并必丢内容） |
| `IDM_MANY_OLD_ONE_NEW` | 多个旧身份映射同一新身份，即使指纹相同也无安全自动合并 |
| `IDM_ONE_OLD_MANY_NEW` | 一个旧身份对应多个新身份（含目标契约下无法重算身份的条目） |
| `IDM_LEDGER_DRIFT` | 迁移台账与 sources 表记录的契约矛盾（中断或库被外部改动） |

应用迁移时每个源在一个事务内完成，键名经“旧键 → 临时键 → 新键”两阶段改名，因此交换、链形映射也不会碰撞；外键约束延迟到提交时检查。`items`、`item_enclosures`、`item_events` 随键一起移动，`first_seen_at / last_seen_at / last_changed_at / missing_since` 与全部内容列原样保留，每个改名条目在新键上追加一条 `identity_changed` 事件（`run_id` 为空，detail 记录旧键、新键与前后契约）。迁移结果写入 `identity_migrations` 台账。中断后重复运行幂等：已对齐的源报告零改名，不产生重复事件；报告同时输出当前契约、已对齐源数与迁移条目数。迁移只改键与台账，**不改变任何原始素材**。

## 4. 内容指纹

`content_hash = sha256(canonical_json)`，canonical JSON 使用：

- UTF-8、`ensure_ascii=False`、键排序、无多余空格；
- 字段：title、link、summary、content、published_at、authors、categories、enclosures；
- 文本先做 NFC、换行归一与行尾空白清理；
- 附件按解析顺序记录 URL、MIME、声明长度、position。

身份字段（raw_id / item_key）**不进入**内容指纹：只改正文才意味着同一 item 的 `updated`。发布方改 id 的语义由身份契约决定——`id_first` 下视为新条目，`link_first` 下只要规范链接不变就仍是同一条目的连续历史。

## 5. 生命周期状态机

```text
              discovered
  (absent) ----------------> active
                              |  ^
                  missing     |  | restored
                              v  |
                            missing
```

规则：

- 新 key：插入 item，写 `discovered`；
- 已存在且 fingerprint 相同：
  - 活跃条目：仅推进 `last_seen_at`，无事件；
  - 缺失条目：清除 `missing_since`，写 `restored`；
- 已存在且 fingerprint 不同：写 `updated`（detail 列 changed_fields）；若此前缺失，同时写 `restored`；
- 成功解析的源中，活跃旧条目本轮的 key 不在 seen 集合中：设置 `missing_since`，写 `missing`；
- 整源失败、304、未修改、或本轮发生条目上限截断时：**不做缺失对账**；
- 截断时 source_run 的 `truncated=true`，`error_code=ITEMS_TRUNCATED`，但 source 状态仍可为 `ok`。

条目级解析错误不会让整源失败，但缺失对账仍以“本轮成功解析出的条目集合”为准。这是有意的：一个持续无法解析的条目会在生命周期上表现为缺失，同时错误码解释原因。

## 6. 条件请求与重试

每个源成功获取 200 后保存响应 ETag 与 Last-Modified。下一轮带：

```text
If-None-Match: <etag>
If-Modified-Since: <last-modified>
```

- `304`：不解析、不碰条目，只更新 source 的 `last_checked_at/last_success_at`、重置连续失败，并把 `source_runs.status` 记为 `not_modified`；
- 解析失败时不更新条件缓存，下一轮会重新下载并再次报告失败，避免 304 掩盖坏源；
- 一个逻辑 fetch 的总尝试次数上限为 `1 + max_retries`。

重试矩阵：

| 结果 | 重试 | 说明 |
|---|---|---|
| 429 / 5xx | 是 | 指数退避 |
| 连接失败 / 连接或读取超时 | 是 | 指数退避 |
| 其他 4xx | 否 | 立即源级失败，保留 HTTP 码 |
| 响应体超限 | 否 | 丢弃内容，`BODY_TOO_LARGE` |
| 白名单拒绝 | 否 | 未建立连接、未解析 DNS，`POLICY_VIOLATION` |
| 整源 XML/JSON/格式错误 | 否 | 到达应用层后直接失败 |

第 n 次重试前等待：

```text
min(backoff_base_ms * 2^n, 30_000ms)
```

每次尝试前都经过同主机最小间隔限制；`attempts` 是实际发出的 HTTP 尝试次数。

## 7. 请求边界

- 只允许 `http/https`，且 hostname 必须精确命中策略白名单；
- 白名单本身只允许回环别名；
- URL 中含用户信息、控制字符或反斜杠直接拒绝；
- 禁用环境代理；
- 自定义重定向处理器对每个跳转目标重新执行白名单校验；
- 请求头只有 `Accept`、`User-Agent` 和可选条件头；
- 不伪造 UA 池、不使用代理链、不保存 Cookie / Authorization；
- 响应流式读取，超过 `max_body_bytes` 立即停止并丢弃；
- XML 预扫描拒绝 `DOCTYPE` 与 `ENTITY`，防外部实体/实体扩展。

## 8. 配置错误与策略错误

- `ConfigError` 汇总多个 `ConfigIssue`，每个 issue 包含稳定 code、文件名、字段路径、消息；
- 配置文件缺失：`CFG_001`；
- YAML/顶层结构错误：`CFG_002`；
- URL 非法或主机不在白名单：`CFG_003`；
- 缺字段/重复 id/非法 slug/未知字段/格式或数值非法：`CFG_004` 到 `CFG_013`；
- 运行期白名单拒绝使用 `PolicyViolation`（`POLICY_001` 到 `POLICY_006`），仍会写入失败 source_run。

## 9. 确定性与排序

- fixture 由 `feedkeep/mockdata.py` 生成；manifest 保存每个文件的 sha256；`--check` 检测手工漂移；
- 所有时间通过注入的 `Clock` 获取；测试使用 `FixedClock`；
- 报告 JSON 使用排序键与固定缩进；
- 来源按 `source_id` 升序；条目问题按来源、序号、插入 id；事件按来源、key、类型、时间；inventory 按来源；
- run id 递增，不依赖随机 UUID；
- 报告中的 `generated_at`、run 时间与 `duration_ms` 属于时间相关字段，测试在固定时钟下仍字节稳定。

## 10. 事务与失败隔离

- 一轮抓取的每个源独立完成解析与归档；坏源不阻断后续源；
- `apply_observation` 在单个 SQLite 事务内写 item、附件、事件、条目问题和缺失状态；
- source 成功状态与 source_run 在观察后写入；
- 存储异常会转为该源的 `STORE_ERROR`，不影响其他源；
- 整轮结束后统一计算 `ok / partial / failed` 并写 runs 汇总。

## 11. 测试策略

- 纯函数：配置、策略、格式嗅探、归一化、身份、指纹、生命周期；
- 组件：三个 parser、Fetcher、Store；
- 集成：mock 服务上的条件请求、超时、重试、404、体积上限、条目截断；
- 端到端：CLI fetch/report 与 demo 自检；
- 所有断言使用固定时钟或只检查错误码/计数，不比较真实时间。
