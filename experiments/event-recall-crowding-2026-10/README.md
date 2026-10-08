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
3. 取 top_k / supersedes / 返回顺序按 eff_ts 的**真实时刻**（`julianday`）排，不按字符串。生产 ts 混着
   `+00:00` / `+08:00` / `Z` / naive，`+08:00` 串比同一时刻的 UTC 串"新"8h——修 2 之后时间序是唯一的
   挑选依据，按串排会让 7–8h 前的 `+08:00` 事件挤掉 1h 前的 UTC 事件（本分支第二版，评审指出）。

零 LLM、零网络。生产库只读打开后 backup 到临时目录再跑。新旧两版各按自己的生产调用：旧版
`embed_text(symbol)` hash 精排，新版 `embed_query(symbol)`（hash 下 = 不精排）；共同：
`aliases = proxy_symbols_for(symbol)`、7 天窗 / mid / top_k 8、`as_of` = 每个工作日 02:00Z
（北京 10:00，daily_report 时刻）。旧版从 git `a4d6fc2`（修复前的 main）现场加载源码。

## 跑法（仓库根，`INVEST_EMBEDDING_PROVIDER` 不设 = 生产默认 hash）

```bash
DB=$INVEST_HOME/db/events.db; W=<holdings ∪ target_assets，逗号分隔>
R="uv run python experiments/event-recall-crowding-2026-10/replay.py --db $DB --watched $W --start 2026-09-01 --end 2026-10-07"
O=experiments/event-recall-crowding-2026-10
$R                   > $O/result.json              # 生产口径：旧（hash 精排）vs 新（时间序）
$R --new-hash-rerank > $O/result_hash_rerank.json  # 对照：新版也 hash 精排（本分支第一版，被否掉）
```

- **去标识**：关注 symbol 按 `--watched` 顺序记为 S1..S4；提交的结果文件只含汇总计数（逐日明细与今天的实时计数
  对照公开新闻时间线可能反推出标的，只在本地用 `--with-rows` / `--default-path` 看，不提交）。
- **新鲜度（fresh）**：新版口径下合格、且 eff_ts 落在 as_of 前 24h 内的事件；数其中有多少进了 brief。
- **陈旧槽位（stale）**：brief 里比"真·第 8 新"合格事件还老 1h 以上的条数；真·最新 8 条在 Python 里按
  解析后的时刻排（不信 SQL 的排序）。新版应恒为 0。
- 快照上界：`max(created_at) ≈ 2026-10-08T06:16Z`（约 2.5 万行）（库还在长；as_of 路径按 `created_at <= as_of` 截断，
  过去的日子同快照重跑逐字节一致）。
- as_of 只截"事件存不存在"：同 event_id 后续 upsert 原地改写的 severity / affected_symbols 还原不了（#196 已知限制）。

## 结果（2026-09-01 → 2026-10-07，27 个工作日 × 4 个关注标的）

`result.json`（旧 → 新，生产口径）：

| | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| 召回事件数（27 天合计，上限 8×27=216） | 152 → **216** | 216 → 216 | 5 → **18** | 26 → **83** |
| 0 条 → 有事件 的天数 | 2 | 0 | 10 | 15 |
| 有事件 → 0 条 的天数 | 0 | 0 | 0 | 0 |
| 近 24h 合格事件进 brief 的条数（合格总数） | 89 → **113**（126） | 128 → **210**（941） | 2 → 2（2） | 12 → 14（16） |
| 其中 high（合格总数） | 23 → 23（28） | 43 → **56**（308） | 1 → 1（1） | 0 → 0（0） |
| brief 里有近 24h 事件的天数 / 有近 24h 合格事件的天数 | 21 → 21 / 21 | 27 → 27 / 27 | 2 → 2 / 2 | 5 → 5 / 5 |
| 召回里的未来 ts 事件 | 0 → 0 | **17 → 0** | 0 → 0 | 0 → 0 |
| 陈旧槽位（比真·第 8 新老 >1h）条数 / 天数 | 29 / 13 → **0** | 161 / 27 → **0** | 0 → 0 | 2 / 1 → **0** |
| 召回事件距 as_of 小时数中位 | 20.4 → 21.9 | 17.3 → **4.0** | 24.5 → 63.2 | 26.5 → 71.0 |

对照 `result_hash_rerank.json`（新版也 hash 精排）：近 24h 进 brief S1 89 → **35**、S2 128 → **40**，
high S1 23 → 6、S2 43 → 15；brief 里有近 24h 事件的天数 S1 21 → 15、S2 27 → 20；年龄中位
S1 92.3h、S2 70.2h。→ 合格集变大后 hash 精排就是在一周里随机抽 8 条，所以生产 hash 下关掉精排。

第二版（不精排、但按 eff_ts **字符串**排）对第三版（按真实时刻排），同一回放（不提交，`--old-rev e9ef859`
且旧侧也走 `embed_query`）：陈旧槽位 S1 8 条 / 7 天、S2 45 / 20、S4 1 / 1 → 全 0；S2 近 24h high
76 → 56。差的 20 条不是丢了新闻：S2 的 `+08:00` 事件 high 占比 50%（499 条里 250），其他写法 27%
（1106 条里 295），按串排把 7–8h 前的 `+08:00` high 虚抬进了最新 8 条，挤掉的是真正更新、多为 mid 的事件。

人话：

- **低频标的最受益**：S3/S4 旧版常常 0 条（别的标的一天几百条把它们挤出前 200），新版 27 天里
  分别有 10 / 15 天从"没事件"变成"有事件"；S1 也有 2 天。没有一天从有变无。
- **高频标的拿到的是最新的**：S2 近 24h 事件进 brief 128 → 210 条，年龄中位 17h → 4h；旧版 27 天里
  召回过 17 次未来 ts 事件（错标成 12 月的旧新闻，入库后一直钉着），新版 0 次。
- **代价**：按时间序不看 severity。近 24h 合格事件超过 8 条的日子，最新 8 条可能 mid 居多、把稍早的 high
  挤掉：108 个标的·日里有 6 个新版 high 比旧版少（S1 两天 3 → 1、1 → 0；S2 四天 4 → 2、1 → 0、4 → 1、
  1 → 0，这 6 天近 24h 合格事件都有 10–61 条），17 个更多；合计 S1 high 23 → 23、S2 43 → 56。低频标的（S3/S4）的年龄中位变大是因为多出来的事件来自一周里更早的
  日子（旧版这些天是 0 条），近 24h 的事件一条没少。

## 下游：情绪表盘里的「事件净情绪」行也跟着变

brief 换了，`session.py` 由 brief 算出的逐资产 `EVENT_STANCE(sym)` 行（net risk / neutral / opportunity）
也会变——这行进的是 debate 注入的「MARKET SENTIMENT 表盘（确定性事实，必须纳入）」。同一 27 个工作日，
按生产多标的路径（`resolve_event_brief_multi`，as_of = 当日 02:00Z）新旧 brief 各喂
`event_stance_line_for_symbol`：

| | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| 净情绪标签变了的天数（/27） | 9 | 7 | 9 | 15 |
| 其中 risk ↔ opportunity 方向翻转 | 0 | **5（risk → opportunity）** | 0 | 0 |
| 主要变化 | neutral → risk 5 天 | risk → neutral 2 天 | 无此行 → risk 6 天 | 无此行 → risk 8 / opportunity 7 天 |

合计 108 个标的·日里 40 个变了。S3/S4 的变化几乎都是「旧版 0 条事件所以没有这一行 → 新版有」；
S2 的 5 天方向翻转来自旧版 hash 随机抽到的偏旧风险新闻被换成最新事件（opportunity 还会带上
"短线反向指标"提示）。复算脚本含真实标的未提交，口径同上表即可复现。

## 性能（快照 25k 事件，`EXPLAIN QUERY PLAN`）

- 新查询钉 `idx_events_ts`（`INDEXED BY`）：不钉时无 stat 的规划器改走 `idx_events_severity`
  扫大半张表（单次 7.5ms vs 1.9ms）。不需要新索引。
- 按 `julianday(eff_ts)` 排序不改计划（仍 `SEARCH idx_events_ts (ts>?)` + `TEMP B-TREE FOR ORDER BY`）。
- 端到端单次 recall：纯 SQL 路径 2ms → 2–4ms（hash 下生产走这条）。真 embedding 精排路径按候选数线性
  （vec0 `rowid IN (...)`，最多 200 个），快照上 25–96ms。每次委员会每标的一次，可忽略。
