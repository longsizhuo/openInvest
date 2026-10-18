# event-corroboration-2026-10 — 同论点转载归并回放（B6 后半）

**问题**：同一条新闻被多家转载，brief 里以多条出现，`EVENT_STANCE` 按头行计数 → 同一论点被数多次。
`event_store` 的 `event_id` 是整句 claim 的 sha256，措辞差一个词就是新 id，挡不住转载。

**改动**：`format_event_brief` 把同论点聚成一簇，每簇最多保留 `event.max_corroboration`（默认 1，0 = 不归并）条，
保留条 claim 行末标 `(×N 来源)`。同论点 = 桶相同（affected_symbols 集合 + stance + severity + 来源身份）且归一化
完整 claim 的 difflib 相似度 ≥ 0.85 且两句里的数字按出现顺序完全一致、小数不拆（"加息 25bp" 与 "50bp"、"CPI 2.5% vs 预期 2.6%" 与 "2.6% vs 预期 2.5%" 都是两个事实）。每簇留 ts 最新的那条。stance/severity 不同永不合并（立场相反的两条是两条证据）；agent 投喂的事件
（`ingested_by` 非空）只与同一个 agent 的合并、永不与爬虫事件合并（爬虫的 N 不含 agent，保留行沿用 `[agent:<who>]` 标签）。`EVENT_STANCE`
从 brief 头行解析，计数随之同步。`resolve_event_brief_multi` 同时改成先按 `event_id` 合并事件、再一次性
格式化（旧版按段落文本去重，同一事件在两个 symbol 召回里 `↳ supersedes` 行不同就会被列两遍）。

**回放**：零 LLM、零网络。生产 `events.db` 只读 backup 后跑；旧版 `event_brief.py` 从 git `c460c47`（本改动前的
main）现场加载 + `INVEST_EVENT_MAX_CORROBORATION=0`，新版 = 当前源码默认值；同一份快照、同一个 EventStore、
召回参数全同。`as_of` = 每天 02:00Z（北京 10:00），过去 30 天（2026-09-19 → 2026-10-18），4 个关注标的
（输出记为 S1..S4）。走生产链：`resolve_event_brief_multi` → `event_stance_line_for_symbol`。

```bash
uv run python experiments/event-corroboration-2026-10/replay.py \
    --db $INVEST_HOME/db/events.db --watched SYM1,SYM2,SYM3,SYM4 \
    --start 2026-09-19 --end 2026-10-18 > experiments/event-corroboration-2026-10/result.json
```

**去标识**：`result.json` 只含汇总计数；逐日明细（`--with-rows`）对照公开新闻时间线可能反推出标的，只在本地用，不提交。
快照上界 `max(created_at) ≈ 2026-10-18T08:00Z`；`as_of` 路径按 `created_at <= as_of` 截断，过去的日子同快照重跑逐字节一致。

## 结果（`result.json`）

| | 旧 | 新 |
|---|---|---|
| 30 天 brief 条数合计（Macro/CIO 看到的合并 brief） | 672 | **658**（−14，−2.1%；10/30 天有变，单日最多 −2） |
| 带 `(×N 来源)` 的行（天 × 论点） | — | 9 |
| 合并 brief 里同论点残余重复（天 × 条，按同一套归并条件再折叠） | 14 | **0** |
| 市场级 EVENT_STANCE risk / opportunity 合计 | 378 / 221 | 377 / **208**（opportunity −13） |
| 市场级 net 标签变化的天数 | — | 1（neutral → risk：重复的 opportunity 去掉后，risk/opportunity 10/10 → 10/8） |

按标的的 EVENT_STANCE（30 天合计 risk / opportunity，旧 → 新；net 标签变化天数全为 0）：

| | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| risk | 142 → 141 | 49 → 49 | 50 → 50 | 154 → 154 |
| opportunity | 88 → 85 | 16 → 16 | 59 → **49** | 69 → 67 |
| 计数有变的天数 | 4 | 0 | 7 | 2 |

**读法**：归并条件保守（标的集合 / stance / severity / 来源身份必须完全相同，整句相似度 ≥ 0.85，数字一致），
30 天里只折叠了 14 条（约 2%）。它去掉的是明显的重复计数，不是语义去重——措辞差异更大的同论点转载不在此列。
相似度阈值扫描（同一快照，brief 条数 672 → ·）：0.75 → 654、0.80 → 654、**0.85 → 658**、0.90 → 660、0.95 → 670；
人工过目了 0.70–0.95 区间的同桶对子：0.85 以下的多带额外细节，0.85 起基本是纯改写。
S3 的 opportunity 下降主要来自两组各 2 条的改写稿（同一论点在 7 天召回窗里被连续多天重复数）。
