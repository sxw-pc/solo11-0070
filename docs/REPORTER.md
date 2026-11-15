# 报告字段字典与排序规则

`feedkeep report` 从 SQLite 读取一轮已完成的 run，生成两个文件：

```text
reports/fetch-report-run-000001.json
reports/fetch-report-run-000001.md
```

默认选择最近一轮已完成 run；`--run-id run-000001` 可指定历史 run。没有已完成 run 时退出码为 2。

## 1. JSON 顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 报告 schema 版本，当前为 2 |
| `generated_at` | string | 生成时刻，UTC 秒级 ISO-8601 |
| `run` | object | 本轮汇总 |
| `sources` | array | 每个启用来源的本轮明细 |
| `item_issues` | array | 条目级错误 |
| `events` | array | 本轮生命周期事件 |
| `inventory` | array | 生成时刻各来源的当前归档清单 |
| `partial_coverage` | array | 该 run 未完成的渐进观察覆盖检查点；已完成观察不出现 |

`partial_coverage[]` 每个元素描述一个已提交、已校验但全量响应尚未确认的分块前缀：

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` / `source_id` | string | 所属 run 与来源 |
| `chunk_index` | int | 已提交分块序号（0 起始） |
| `chunk_count` | int | 该响应按 `chunk_size` 切出的总分块数 |
| `keys_covered` | int | 该分块已应用的唯一条目键数 |
| `reconcile_mode` | string | 固定为 `progressive`（原子模式无 partial 状态） |
| `truncated` | bool | 响应是否被条目上限截断 |
| `full_response_confirmed` | bool | partial 行恒为 `false` |
| `observed_at` | string | 分块提交时刻 |

partial 存在意味着该 run 的未见条目**没有**也**不会**被标记为 missing；续跑完成后检查点被清除，数组回到空。

## 2. `run` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string | `run-000001` 形式 |
| `started_at` / `finished_at` | string | 本轮开始/结束时间 |
| `status` | string | `ok` / `partial` / `failed` |
| `config_path` | string/null | 本轮配置路径 |
| `sources_total` | int | 启用的来源数 |
| `sources_ok` | int | 成功解析并归档的来源数 |
| `sources_not_modified` | int | 返回 304 的来源数 |
| `sources_failed` | int | 源级失败数 |
| `items_seen` | int | 本轮成功观察到的唯一条目数 |
| `items_new` | int | 首次发现数 |
| `items_updated` | int | 内容指纹变化数 |
| `items_missing` | int | 本轮新标记缺失数 |
| `items_restored` | int | 从缺失恢复数 |
| `item_errors` | int | 条目级错误总数 |

状态计算：

- 无启用来源或全部来源失败：`failed`；
- 至少一个失败但非全部：`partial`；
- 其余：`ok`。

`not_modified` 不算失败。条目级错误不改变源或 run 的成功状态。

## 3. `sources[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 配置中的 slug |
| `source_name` | string | 本轮源名快照 |
| `status` | string | `ok` / `not_modified` / `failed` |
| `http_status` | int/null | 最终 HTTP 状态；连接失败/策略拒绝为 null |
| `duration_ms` | int | 逻辑 fetch 总耗时，含重试和限速等待 |
| `body_bytes` | int | 成功响应字节数；超限时为观察到的字节数 |
| `body_sha256` | string/null | 响应体 sha256；304、超限丢弃时为 null |
| `etag` | string/null | 最终响应 ETag |
| `last_modified` | string/null | 最终响应 Last-Modified |
| `attempts` | int | 实际发出的 HTTP 尝试次数；预连接策略拒绝为 0 |
| `items_total` | int | 本轮观察到的唯一条目数 |
| `items_new` | int | 该源新增数 |
| `items_updated` | int | 该源更新数 |
| `items_missing` | int | 该源新缺失数 |
| `items_restored` | int | 该源恢复数 |
| `item_errors` | int | 该源条目级错误数 |
| `error_code` | string/null | 失败或告警码 |
| `error_message` | string/null | 面向人的简短消息 |
| `truncated` | bool | 是否因单源条目上限截断 |
| `reconcile_mode` | string | 本轮对账语义：`atomic`（原子快照）/ `progressive`（渐进可见） |
| `observed_at` | string | 处理该源的时刻 |

超限的 source 若解析成功但条目被截断，`status` 仍为 `ok`，同时 `truncated=true`、`error_code=ITEMS_TRUNCATED`，且不会执行缺失对账。

## 4. `item_issues[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 来源 |
| `item_index` | int | 源文件中该条目的 0 起始序号 |
| `code` | string | 稳定错误码，例如 `ATOM_ITEM_MISSING_ID` |
| `message` | string | 简短说明 |

常见条目级错误：

- `RSS_ITEM_BAD_DATE`
- `ATOM_ITEM_MISSING_ID`
- `ATOM_ITEM_MISSING_TIME`
- `ATOM_ITEM_BAD_DATE`
- `JSONFEED_ITEM_BAD_ID`
- `JSONFEED_ITEM_BAD_DATE`
- `ITEM_NO_IDENTITY`
- `ITEM_DUPLICATE_KEY`

## 5. `events[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 来源 |
| `item_key` | string | 条目稳定身份 |
| `event_type` | string | `discovered` / `updated` / `missing` / `restored` |
| `observed_at` | string | 事件发生时刻 |
| `detail` | object | 事件细节 |

`detail` 约定：

- discovered：`content_hash`
- updated：`content_hash`、`previous_hash`、`changed_fields`
- missing：`last_content_hash`
- restored：`content_hash`

## 6. `inventory[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 来源 |
| `total` | int | 当前账本中该源条目总数 |
| `active` | int | `missing_since IS NULL` 的条目数 |
| `missing` | int | 当前缺失条目数 |

inventory 表示**报告生成时**的账本状态；较早 run 的报告如果稍后生成，inventory 可能包含后续 run 的影响。run 自身的计数不受影响。

## 7. Markdown 结构

顺序固定：

1. 标题（run id）；
2. 元信息（状态、开始、结束、配置、报告生成）；
3. 汇总表；
4. 来源明细表（含“对账模式”列，显示 `atomic` / `progressive`）；
5. 来源错误；
6. 条目级错误；
7. 生命周期事件；
8. 当前归档清单；
9. 渐进观察覆盖检查点（未完成）。

表格单元格中的 `|` 会转义，换行会替换为空格；空值渲染为 `-`。没有内容时输出“无。”或用空表占位。

## 8. 排序规则

| 数组 | 排序键 |
|---|---|
| `sources` | `source_id` 升序 |
| `item_issues` | `source_id`、`item_index`、插入 id |
| `events` | `source_id`、`item_key`、`event_type`、`observed_at` |
| `inventory` | `source_id` 升序 |

JSON 使用：

```python
json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
```

因此对象键顺序、数组顺序与末尾换行均稳定。

## 9. 可复现性

在以下条件同时满足时，两次生成的 JSON/Markdown 字节一致：

- 同一个 SQLite 状态；
- 使用相同 `run_id`；
- 注入相同时钟（测试中的 `FixedClock`）；
- 不并发写入。

真实运行时 `generated_at`、run 时间、`duration_ms` 属于时间相关字段，会随执行变化；这是设计上明确允许的差异。不要用真实墙钟断言报告字节相等。
