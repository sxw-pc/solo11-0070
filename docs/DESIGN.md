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

SQLite 使用单文件、`foreign_keys=ON`、`journal_mode=DELETE`（关闭 WAL）、`synchronous=FULL`。`schema_meta.schema_version` 记录结构版本；当前为 2，遇到更高版本直接拒绝，避免旧二进制写坏新库。版本 1 的数据库在首次打开时自动迁移到版本 2：新增列与计数以 `ALTER TABLE` 补齐，`item_events` 因需要放宽 `CHECK` 约束而整表重建，旧事件行原样复制，审计数据不丢失。

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
- 生命周期：`first_seen_at/last_seen_at/last_changed_at/missing_since/retracted_at`；
- `updated_at`：发布者声明的最后修改时间（Atom `<updated>`），用于撤回标记的新旧裁决，不进入内容指纹；
- 来源 run：`first_run_id/last_run_id`。

### `item_enclosures`

每个条目可有多条附件元数据，按 `position` 排序。只保存 URL、MIME、声明长度，**从不下载二进制**。

### `item_events`

允许五类事件：

- `discovered`：首次出现；
- `updated`：内容指纹改变，detail 包含变更字段与新/旧指纹；
- `missing`：一次完整成功抓取中未再出现（观察意义上的缺席）；
- `restored`：此前缺失（missing）的条目重新出现；
- `retracted`：发布者通过 Atom tombstone 明确撤回，detail 保留原始 `ref`、声明时间 `declared_at` 与撤回前 `last_content_hash`。

表上有 `(source_id,item_key,run_id,event_type)` 唯一约束，防止同一 run 重放产生重复事件。一条目在同一 run 至多写一条终态事件。

### `item_issues`

条目级错误与 run 绑定，包含原始条目序号、稳定错误码、消息。整个源解析失败不会进入这里，而是记录在 `source_runs.error_code`。

## 3. 条目身份

身份按以下优先级生成，只有得到身份后才可入库：

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
- 三者都没有时产生条目级错误 `ITEM_NO_IDENTITY`，该条目被跳过，其他条目继续；
- 同一响应内两个条目得到相同 key 时，保留第一条，第二条记 `ITEM_DUPLICATE_KEY`；
- 主键同时含 source_id，因此不同来源的相同 id 不会冲突。

## 4. 内容指纹

`content_hash = sha256(canonical_json)`，canonical JSON 使用：

- UTF-8、`ensure_ascii=False`、键排序、无多余空格；
- 字段：title、link、summary、content、published_at、authors、categories、enclosures；
- 文本先做 NFC、换行归一与行尾空白清理；
- 附件按解析顺序记录 URL、MIME、声明长度、position。

身份字段（raw_id / item_key）**不进入**内容指纹：发布方改了 id 意味着新条目；只改正文才意味着同一 item 的 `updated`。

## 5. 生命周期状态机

```text
              discovered
  (absent) ----------------> active
                              |  ^
                  missing     |  | restored
                              v  |
                            missing

  active/missing --retracted--> retracted
  retracted --entry reappears--> active（不产生 restored 事件）
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

### 5.1 Atom tombstone（发布者明确撤回）

Atom feed 根下命名空间为 `http://purl.org/atompub/tombstones/1.0` 的 `deleted-entry` 元素是发布者的明确删除指令，与“本轮没看到条目”的观察缺席严格区分：

- parser 独立收集 tombstone，校验 `ref` 与 `when`；`when` 归一化为 UTC 秒级时间，缺 ref、缺 when、时间无法解析、同响应重复 ref 都进入 `item_issues`，不影响普通 entry；
- `ref` 走与 Atom 显式 `id` 完全相同的身份规范化，因此 tombstone 与条目命中同一稳定 `item_key`；
- 撤回裁决按**事件时间**而非 XML 顺序：tombstone 的 `when` 早于条目最新事实（优先 `updated_at`，否则 `last_changed_at`）时判定为陈旧（`TOMBSTONE_STALE` issue），保留较新条目，绝不按抓取顺序撤回新内容；时间相等视为有效；
- 同一响应中 entry 与 tombstone 指向同一 ref 时，仍按 `when` 与 entry `updated` 分胜负：tombstone 新则撤回，entry 新则拒绝撤回并记 stale；
- 有效撤回把条目移入 `retracted` 状态（写 `retracted_at=声明时间`、清 `missing_since`），写一条 `retracted` 事件，detail 带原始 `ref`、`declared_at` 与 `last_content_hash`；
- 已撤回条目再次收到 tombstone 是幂等 no-op；未归档的 ref 没有可撤回对象，不产生事件；
- 撤回后的条目重新以普通 entry 出现时回到 active，但**不写 `restored`**——撤回不是观察缺席，恢复语义只适用于普通 missing；
- 截断响应不做 missing 对账，但已成功解析的有效 tombstone 仍独立处理，因为它是显式证据而非缺席推断。

### 5.2 撤回内容保留策略

每个来源通过 `retention_mode` 显式选择策略，缺省为 `retain_retracted_content`：

- `retain_retracted_content`：保留原正文、摘要、作者、分类、enclosures 与 `content_hash`，仅状态变为 retracted，供内部历史查询，回答“发布者撤回了什么”；
- `erase_retracted_content`：保留 `item_key/raw_id/content_hash` 与撤回事件，但清空正文、摘要、作者、分类、附件行等发布者内容，回答“这条目已撤回，内容按保留政策删除”。

两种模式都把条目移出 active inventory 并保留明确撤回证据；清空内容不得被误报为抓取缺失。

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
