# event-recall-crowding-2026-10 — 事件召回被别的标的挤掉 + 未来 ts 钉榜首 回放

**问题**：`EventStore.recall` 旧版 `... WHERE ts >= ? AND severity >= ? ORDER BY ts DESC LIMIT 200`
之后才在 Python 里按 symbol / 代理 symbol 过滤。生产 7 天窗内合格事件 680–1250 条（回放 27 天），最新 200 条
只覆盖 0.7–3.7 天（中位 1.5 天），持仓标的稍早的事件被别的标的挤掉。另有 LLM 把预告日期当发生时刻的未来 ts
（快照里 19 条 ts 晚于快照时刻，最远 2026-12-07），`ORDER BY ts DESC` 让它们入库后几个月一直钉在榜首、不出窗。

**修法**：

1. `src/openinvest/db/event_store.py`：symbol/alias/tag 匹配下推进 SQL、先于 `LIMIT 200`；
   ts 晚于 `created_at` 的按 `created_at` 算（窗口 / 排序 / 返回的 ts），存量行读侧兜底不迁移，新入库的
   写侧直接存 `created_at`。`recall` 本身的精排语义不变（有 query 向量就只在合格集里排）。
2. `src/openinvest/services/embeddings.py::embed_query`：默认 hash provider 下不给 query 向量 → 召回按
   时间倒序取 top_k。hash 向量无语义（不同文本余弦距离全挤在 ~1.0），拿它精排 = 确定性随机洗牌；
   修 1 之后合格集覆盖整个 7 天窗，随机挑 8 条会把近一天的新闻挤掉（见下面对照组）。真 embedding
   （`INVEST_EMBEDDING_PROVIDER=openai`）照常精排。

零 LLM、零网络。生产库只读打开后 backup 到临时目录再跑。新旧两版各按自己的生产调用：旧版
`embed_text(symbol)` hash 精排，新版 `embed_query(symbol)`（hash 下 = 不精排）；共同：
`aliases = proxy_symbols_for(symbol)`、7 天窗 / mid / top_k 8、`as_of` = 每个工作日 02:00Z
（北京 10:00，daily_report 时刻）。旧版从 git `a4d6fc2`（修复前的 main）现场加载源码。

## 跑法（仓库根，`INVEST_EMBEDDING_PROVIDER` 不设 = 生产默认 hash）

```bash
DB=$INVEST_HOME/db/events.db; W=<holdings ∪ target_assets，逗号分隔>
R="uv run python experiments/event-recall-crowding-2026-10/replay.py --db $DB --watched $W --start 2026-09-01 --end 2026-10-07"
O=experiments/event-recall-crowding-2026-10
$R --default-path    > $O/result.json              # 生产口径：旧（hash 精排）vs 新（时间序）
$R --new-hash-rerank > $O/result_hash_rerank.json  # 对照：新版也 hash 精排（本分支第一版，被否掉）
```

- **去标识**：关注 symbol 按 `--watched` 顺序记为 S1..S4，结果文件只有计数和事件年龄，没有 ticker / 事件文本。
- **新鲜度（fresh）**：新版口径下合格、且 eff_ts 落在 as_of 前 24h 内的事件；数其中有多少进了 brief。
- 快照上界：`max(created_at) ≈ 2026-10-08T06:16Z`（约 2.5 万行）（库还在长；as_of 路径按 `created_at <= as_of` 截断，
  过去的日子同快照重跑逐字节一致）。`default_path`（as_of=None，生产现行路径）随运行时刻变，带 `run_at`。
- as_of 只截"事件存不存在"：同 event_id 后续 upsert 原地改写的 severity / affected_symbols 还原不了（#196 已知限制）。

## 结果（2026-09-01 → 2026-10-07，27 个工作日 × 4 个关注标的）

`result.json`（旧 → 新，生产口径）：

| | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| 召回事件数（27 天合计，上限 8×27=216） | 152 → **216** | 216 → 216 | 5 → **18** | 26 → **83** |
| 0 条 → 有事件 的天数 | 2 | 0 | 10 | 15 |
| 有事件 → 0 条 的天数 | 0 | 0 | 0 | 0 |
| 近 24h 合格事件进 brief 的条数（合格总数） | 89 → **111**（126） | 128 → **210**（941） | 2 → 2（2） | 12 → 13（16） |
| 其中 high（合格总数） | 23 → 22（28） | 43 → **76**（308） | 1 → 1（1） | 0 → 0（0） |
| brief 里有近 24h 事件的天数 / 有近 24h 合格事件的天数 | 21 → 21 / 21 | 27 → 27 / 27 | 2 → 2 / 2 | 5 → 5 / 5 |
| 召回里的未来 ts 事件 | 0 → 0 | **17 → 0** | 0 → 0 | 0 → 0 |
| 召回事件距 as_of 小时数中位 | 20.4 → 23.1 | 17.3 → **4.4** | 24.5 → 63.2 | 26.5 → 71.0 |

今天（`default_path`，run_at 2026-10-08T06:18Z）：S1 8 → 8 条（近 24h 4 → 8）、S2 8 → 8（近 24h 6 → 8，
8 条全换）、S3 0 → 0、**S4 1 → 8**。

对照 `result_hash_rerank.json`（新版也 hash 精排）：近 24h 进 brief S1 89 → **35**、S2 128 → **40**，
high S1 23 → 6、S2 43 → 15；brief 里有近 24h 事件的天数 S1 21 → 15、S2 27 → 20；年龄中位
S1 92.3h、S2 71.5h。→ 合格集变大后 hash 精排就是在一周里随机抽 8 条，所以生产 hash 下关掉精排。

人话：

- **低频标的最受益**：S3/S4 旧版常常 0 条（别的标的一天几百条把它们挤出前 200），新版 27 天里
  分别有 10 / 15 天从"没事件"变成"有事件"；S1 也有 2 天。没有一天从有变无。
- **高频标的拿到的是最新的**：S2 近 24h 事件进 brief 128 → 210 条，年龄中位 17h → 4h；旧版 27 天里
  召回过 17 次未来 ts 事件（错标成 12 月的旧新闻，入库后一直钉着），新版 0 次。
- **代价**：按时间序不看 severity。近 24h 合格事件超过 8 条的日子，最新 8 条可能全是 mid、把稍早的 high
  挤掉：108 个标的·日里有 4 个新版 high 比旧版少（S1 两天 3 → 0、1 → 0；S2 两天 4 → 1、1 → 0），
  合计 S1 high 23 → 22、S2 43 → 76。低频标的（S3/S4）的年龄中位变大是因为多出来的事件来自一周里更早的
  日子（旧版这些天是 0 条），近 24h 的事件一条没少。

## 性能（快照 25k 事件，`EXPLAIN QUERY PLAN`）

- 新查询钉 `idx_events_ts`（`INDEXED BY`）：不钉时无 stat 的规划器改走 `idx_events_severity`
  扫大半张表（单次 7.5ms vs 1.9ms）。不需要新索引。
- 端到端单次 recall：纯 SQL 路径 2ms → 2–4ms（hash 下生产走这条）。真 embedding 精排路径按候选数线性
  （vec0 `rowid IN (...)`，最多 200 个），快照上 25–96ms。每次委员会每标的一次，可忽略。
