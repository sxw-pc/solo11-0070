# 中断轮次的显式恢复

feedkeep 的每一轮抓取都是“先建 run、逐源记录、最后汇总”。这一顺序意味着进程可能在任意两个来源之间退出，数据库里就会留下：

- `runs.status = 'running'` 的一行；
- 该轮已经写完的若干 `source_runs`；
- 尚未写入的剩余来源；
- 没有最终汇总（`finished_at` 为空，计数为 0）。

这种数据库状态**既不是成功也不是失败**，而是一份“部分事实”。默认报告会跳过它，`--run-id` 显式指定时也会拒绝生成。`feedkeep recover` 是唯一允许改变这一状态、并把处理方式写进审计记录的入口。

## 1. 状态迁移

```text
                      fetch 正常结束
        running ───────────────────────> ok | partial | failed
           │
           │ recover --mode resume（配置与完整性校验通过）
           ├──────────────────────────────────────────────> ok | partial | failed
           │                                                    （同一个 run id）
           │
           │ recover --mode finalize
           └─────────> interrupted
                            │
                            └── 另建新 run ───────────────> ok | partial | failed
                                （新 run id，prior_run_id 指向旧轮）
```

- `running`：正在执行；只有 `recover` 能离开该状态。
- `interrupted`：已被定格。它的 `source_runs` 是当时的真实快照，汇总由这些行重算，不再变化。
- `ok / partial / failed`：终态，和普通 `fetch` 完全一致。

`latest_run_id` 只选择 `ok / partial / failed`。中断轮次必须通过 `--run-id` 或 `recover` 显式引用，避免旧的部分快照悄悄变成“最近一次报告”。

## 2. 命令

```text
feedkeep recover --mode resume   [--run-id RUN] [--config ...] [--policy ...] [--db ...]
feedkeep recover --mode finalize [--run-id RUN] [--config ...] [--policy ...] [--db ...]
```

- `--run-id` 省略时选择最近一个 `running` 轮次；没有则退出码 2。
- `--mode` 必填，没有默认值：两种路线的业务语义不同，必须由用户显式选择。
- `--quiet` 只输出 `run_id mode reused=N processed=M`。
- 退出码与 `fetch` 对齐：`0=ok`，`1=partial`，`2=failed/无可恢复轮次/数据库不存在`，`3=配置错误`，`5=恢复前提校验失败`。

### 2.1 `--mode resume`：继续同一轮

前提（全部满足才执行）：

1. 目标 run 的 `status = 'running'`；
2. 旧轮保留了来源与策略快照（`runs.snapshot_json` / `fingerprint`）；
3. 当前配置的来源集合、**顺序**与策略摘要和旧快照完全一致；
4. 已写入的每个 `source_runs` 行都带完整性摘要，且摘要与行内容一致；
5. 已记录的来源恰好是配置顺序的连续前缀，未完成位置唯一确定。

满足时：

- 复用旧 `run_id`；
- 跳过已有 `source_runs` 的来源，**不重新抓取**；
- 只处理剩余来源并逐源落库；
- 最终汇总由**持久化记录重算**（不是增量累加），所以重复恢复不会把计数翻倍；
- 写入 `recovery_action='resume'`、`recovery_at`、`resume_count`，以及沿用/本次处理的来源列表。

任何一条前提不满足都会抛 `RecoveryError`，不写任何来源、不改状态：

| 错误码 | 含义 |
|---|---|
| `RECOVERY_RUN_NOT_FOUND` | 找不到目标 run |
| `RECOVERY_NOT_RUNNING` | 目标不是 `running`（已结束或已定格） |
| `RECOVERY_PLAN_MISSING` | 旧轮没有可核对快照（例如 v1 时代遗留的 running 行） |
| `RECOVERY_PLAN_MISMATCH` | 来源集合/顺序或策略摘要变化 |
| `RECOVERY_SOURCE_STATE_INVALID` | 已有来源摘要缺失/不符、状态非法、不是连续前缀 |

注意：策略摘要包含 `max_items_per_feed`、`max_body_bytes`、`max_retries`、超时等会改变“来源结果含义”的字段。只改 report 输出路径或 `config_path` 字符串不影响恢复；改请求边界就必须走 `finalize`。

### 2.2 `--mode finalize`：定格并重开

适用于配置已变化、无法验证旧快照，或者用户明确希望留下一份“完整的新轮”：

1. 在一个事务里把旧轮标为 `interrupted`：写入 `finished_at`、`interrupted_at`、`successor_run_id`，汇总由已有 `source_runs` 重算；
2. 同时创建新的 `running` run，`prior_run_id` 指向旧轮；
3. 新轮按当前配置**完整执行**所有启用来源，包括旧轮已完成的来源；
4. 旧轮和新轮各自带完整审计事实，绝不混在同一 `run_id` 下。

新轮会重新抓取旧轮已完成的来源。如果该来源的条件请求缓存仍然有效，通常会得到 `304 not_modified`，这属于真实的新轮结果，而不是漏跑。

`finalize` 不要求旧快照存在，也不要求配置一致——这正是它的用途。但它同样要求目标 run 是 `running`。

## 3. 报告

报告 schema 版本升到 2，新增 `recovery` 与 `progress` 两个顶层对象。

`recovery`：

| 字段 | 说明 |
|---|---|
| `recovery_action` | `resume` / `finalize` / `null` |
| `recovery_at` | 最近一次恢复操作的时刻 |
| `interrupted_at` | 被定格为中断轮次的时刻（仅 `interrupted`） |
| `resume_count` | 续跑次数 |
| `prior_run_id` | `finalize` 新轮指向的旧轮 |
| `successor_run_id` | 被定格旧轮指向的新轮 |
| `reused_sources` | 续跑时沿用旧结果的来源 |
| `processed_sources` | 本次实际处理的来源 |

`progress`：

| 字段 | 说明 |
|---|---|
| `interrupted` | 本轮是否涉及中断处理 |
| `reason` | 面向人的中断/处理说明 |
| `handling` | 处理方式（`resume` / `finalize` / `null`） |
| `configured_sources` | 本轮计划处理的来源（配置顺序） |
| `completed_sources` | 已有 `source_runs` 的来源 |
| `pending_sources` | 计划中尚无记录的来源 |

`sources[]` 每行新增 `processing`：

- `processed`：本轮正常处理的来源；
- `resumed`：续跑时沿用旧记录的来源；
- `recorded`：被定格轮次里留下的来源事实。

Markdown 报告在涉及中断/恢复时，会在元信息后插入“中断与恢复”小节，列出处理方式、前序/后续 run、已完成与未完成来源、沿用与本次处理的来源；来源明细表新增“处理”列。

对 `running` 轮次仍拒绝生成报告，错误信息会提示使用 `feedkeep recover`。

## 4. 完整性摘要

每条 `source_runs` 行在写入时计算 `record_digest = sha256(canonical_json({...行字段, run_id}))`，覆盖所有会影响汇总的字段以及来源 id、状态、时间。续跑前逐行重算：

- 摘要缺失（v1 迁移上来的旧行）→ 拒绝续跑；
- 摘要不符（手工改库、半写）→ 拒绝续跑。

这保证“已完成的来源事实”不是被猜测出来的。`finalize` 不依赖摘要，它保留旧行原样并重算汇总。

## 5. 兼容与迁移

- `schema_meta.schema_version` 从 1 升到 2；
- 打开 v1 数据库时自动 `ALTER TABLE` 增加 `runs` 的恢复列与 `source_runs.record_digest`，不重建表、不丢数据；
- 更高版本仍直接拒绝；
- v1 遗留的 `running` 行没有快照和摘要：`resume` 会以 `RECOVERY_PLAN_MISSING` 拒绝，请用 `finalize` 定格。

## 6. 边界

- `fetch` 永远创建新轮，不会隐式续跑或覆盖旧的 `running` 行；
- `recover` 不联网做任何“探测性”验证，所有判断都基于持久化数据和当前配置；
- 恢复过程不做跨轮合并；`resume` 的语义是“同一轮继续”，`finalize` 的语义是“旧轮定格 + 新轮完整执行”，不存在第三种混合语义。
