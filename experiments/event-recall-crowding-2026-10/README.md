# event-recall-crowding-2026-10 — 事件召回被别的标的挤掉 + 未来 ts 钉榜首 回放

**问题**：`EventStore.recall` 旧版 `... WHERE ts >= ? AND severity >= ? ORDER BY ts DESC LIMIT 200`
之后才在 Python 里按 symbol / 代理 symbol 过滤。生产 7 天窗内合格事件 680–1250 条（回放 27 天），最新 200 条
只覆盖 0.7–3.7 天（中位 1.5 天），持仓标的稍早的事件被别的标的挤掉。另有 LLM 把预告日期当发生时刻的未来 ts
（快照里 19 条 ts 晚于快照时刻，最远 2026-12-07），`ORDER BY ts DESC` 让它们入库后几个月一直钉在榜首、不出窗。

**修法**（`src/openinvest/db/event_store.py`）：symbol/alias/tag 匹配下推进 SQL、先于 `LIMIT 200`；
ts 晚于 `created_at` 的按 `created_at` 算（窗口 / 排序 / 返回的 ts），存量行读侧兜底不迁移，新入库的
写侧直接存 `created_at`。向量精排语义不变（只在合格集里排）。

零 LLM、零网络。生产库只读打开后 backup 到临时目录再跑。新旧两版都按生产口径调：
`embed_text(symbol)` 向量精排（默认 hash provider）、`aliases = proxy_symbols_for(symbol)`、
7 天窗 / mid / top_k 8、`as_of` = 每个工作日 02:00Z（北京 10:00，daily_report 时刻）。
旧版从 git `a4d6fc2`（修复前的 main）现场加载源码。

## 跑法（仓库根）

```bash
DB=$INVEST_HOME/db/events.db; W=<holdings ∪ target_assets，逗号分隔>
R="uv run python experiments/event-recall-crowding-2026-10/replay.py --db $DB --watched $W --start 2026-09-01 --end 2026-10-07"
O=experiments/event-recall-crowding-2026-10
$R --default-path > $O/result.json            # 生产口径（向量精排）
$R --no-rerank    > $O/result_no_rerank.json  # 对照：去掉精排，纯时间倒序取 top_k
```

- **去标识**：关注 symbol 按 `--watched` 顺序记为 S1..S4，结果文件只有计数和事件年龄，没有 ticker / 事件文本。
- 快照上界：`max(created_at) = 2026-10-08T05:30:19Z`（库还在长；as_of 路径按 `created_at <= as_of` 截断，
  过去的日子同快照重跑逐字节一致）。`default_path`（as_of=None，生产现行路径）随运行时刻变，带 `run_at`。
- as_of 只截"事件存不存在"：同 event_id 后续 upsert 原地改写的 severity / affected_symbols 还原不了（#196 已知限制）。

## 结果（2026-09-01 → 2026-10-07，27 个工作日 × 4 个关注标的）

| | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| 召回事件数 旧 → 新（27 天合计，上限 8×27=216） | 152 → **216** | 216 → 216 | 5 → **18** | 26 → **83** |
| 0 条 → 有事件 的天数 | 2 | 0 | 10 | 15 |
| 有事件 → 0 条 的天数 | 0 | 0 | 0 | 0 |
| 召回集合有变化的天数 | 27 | 27 | 12 | 21 |
| 新版新进的事件条数（合计） | 165 | 153 | 13 | 61 |
| 召回里的未来 ts 事件 旧 → 新 | 0 → 0 | **17 → 0** | 0 → 0 | 0 → 0 |
| 召回事件距 as_of 小时数中位 旧 → 新 | 20.4 → 92.3 | 17.3 → 71.5 | 24.5 → 63.2 | 26.5 → 75.5 |

今天（`default_path`，run_at 2026-10-08T05:55Z）：S1 8 → 8（换 3 条）、S2 8 → 8（换 4 条）、S3 0 → 0、
**S4 1 → 8**。

人话：

- **低频标的最受益**：S3/S4 旧版常常 0 条（别的标的一天几百条把它们挤出前 200），新版 27 天里
  分别有 10 / 15 天从"没事件"变成"有事件"；S1 也有 2 天。没有一天从有变无。
- **高频标的 S2 条数不变但内容换了**：旧版 27 天里召回过 17 次未来 ts 事件（错标成 12 月的旧新闻，
  入库后一直钉着），新版 0 次。
- **年龄中位变大是向量精排的后果，不是 bug**：新版合格集覆盖整个 7 天窗（旧版实际只有 ~1.5 天），
  而默认 hash embedding 对不同文本的余弦距离全挤在 1.0 附近（实测 0.95 以上），精排在合格集里近似随机选 8 条
  → 一周里任意一天的事件都可能入选。对照组 `result_no_rerank.json`（去掉精排、纯时间倒序）：
  年龄中位 S1 16.9h → 23.1h、S2 **−422h → 4.4h**（旧版 216 个槽位里 144 个是未来 ts 钉榜）。
  要不要在 hash provider 下跳过精排、改按新近度取，是单独要拍板的旋钮，这里不改。

## 性能（快照 25k 事件，`EXPLAIN QUERY PLAN`）

- 新查询钉 `idx_events_ts`（`INDEXED BY`）：不钉时无 stat 的规划器改走 `idx_events_severity`
  扫大半张表（单次 7.5ms vs 1.9ms）。不需要新索引。
- 端到端单次 recall：纯 SQL 路径 2ms → 2–4ms；向量精排路径 13–25ms → 25–96ms（合格集变大，
  vec0 `rowid IN (...)` 按候选数线性，最多 200 个；S2 每天都顶 200）。每次委员会每标的一次，可忽略。
