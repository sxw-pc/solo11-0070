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
| `events` | array | 本轮条目生命周期事件 |
| `source_events` | array | 截至生成时刻的全部来源生命周期事件 |
| `inventory` | array | 本轮结束时各来源的归档清单（含退役） |

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
| `epoch_id` | int | 条目所属归档纪元 |
| `item_key` | string | 条目稳定身份 |
| `event_type` | string | `discovered` / `updated` / `missing` / `restored` |
| `observed_at` | string | 事件发生时刻 |
| `detail` | object | 事件细节 |

`detail` 约定：

- discovered：`content_hash`
- updated：`content_hash`、`previous_hash`、`changed_fields`
- missing：`last_content_hash`
- restored：`content_hash`

## 6. `source_events[]` 字段

操作者驱动的来源生命周期时间线（暂停/退役/恢复），覆盖截至生成时刻的全部事件，因此历史 run 的报告也能区分“最后一次抓取结果”与“之后人为退役”。

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 来源 |
| `event_type` | string | `paused` / `retired` / `resumed` / `new_epoch` |
| `epoch_id` | int | 事件作用的归档纪元 |
| `occurred_at` | string | 事件发生时刻 |
| `last_run_id` | string/null | 事件发生前最近一个已完成的 run；尚无已完成 run 时为 null |
| `detail` | object | 事件细节（如 `items_retired`、`previous_epoch`） |

## 7. `inventory[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 来源 |
| `epoch_id` | int | 本轮结束时该来源的当前纪元 |
| `lifecycle` | string | 本轮结束时的来源状态：`active` / `paused` / `retired` |
| `total` | int | 截至本轮已归档的条目总数（含历史纪元） |
| `active` | int | 本轮结束时活跃条目数 |
| `missing` | int | 本轮结束时缺失条目数 |
| `retired` | int | 本轮结束时已退役条目数（所属纪元已关闭） |

inventory 按 **run 结束边界**重建：条目以首次出现的 run 判定是否存在，缺失状态重放到本轮为止的 `missing`/`restored` 事件，来源生命周期状态重放 `last_run_id` 早于本轮的 `source_events`（事件按 run 边界而非墙钟秒排序，同一秒内的“先 run 后退役”也能正确归属）。因此历史 run 的报告反映当时的状态，不受之后的退役或新纪元影响；`source_events` 时间线则始终完整呈现。已退役纪元的条目计入 `retired`，不再区分活跃/缺失。

## 8. Markdown 结构

顺序固定：

1. 标题（run id）；
2. 元信息（状态、开始、结束、配置、报告生成）；
3. 汇总表；
4. 来源明细表；
5. 来源错误；
6. 条目级错误；
7. 生命周期事件；
8. 来源生命周期事件；
9. 当前归档清单。

表格单元格中的 `|` 会转义，换行会替换为空格；空值渲染为 `-`。没有内容时输出“无。”或用空表占位。

## 9. 排序规则

| 数组 | 排序键 |
|---|---|
| `sources` | `source_id` 升序 |
| `item_issues` | `source_id`、`item_index`、插入 id |
| `events` | `source_id`、`item_key`、`event_type`、`observed_at` |
| `source_events` | `occurred_at`、插入 id |
| `inventory` | `source_id` 升序 |

JSON 使用：

```python
json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
```

因此对象键顺序、数组顺序与末尾换行均稳定。

## 10. 可复现性

在以下条件同时满足时，两次生成的 JSON/Markdown 字节一致：

- 同一个 SQLite 状态；
- 使用相同 `run_id`；
- 注入相同时钟（测试中的 `FixedClock`）；
- 不并发写入。

真实运行时 `generated_at`、run 时间、`duration_ms` 属于时间相关字段，会随执行变化；这是设计上明确允许的差异。不要用真实墙钟断言报告字节相等。
