# macro-mom-fix-2026-10 — 宏观涨跌实为 ~3 年（get_history_data period 根因）

## 结论（一句话信号影响）

修复后委员会看到的美元 / 实际利率 / 美债 / VIX 涨跌从"约 3 年"变回所标的真实窗口——宏观块
「MoM」变成真 1 个月，Macro agent 自己用工具查的「3mo/6mo」变成真 3/6 个月：过去 63 个工作日旧口径
**天天**告诉 Macro「美元走弱 + 实际利率下行 = 黄金双顺风」，按真实窗口宏观块有 59 天、工具 3mo/6mo
**全部 63 天**至少一腿方向相反，分别有 **25 / 42 / 53 天**实为「美元走强 + 实际利率上行 = 黄金双逆风」；
regime 标签 / brief、技术指标、VIX 分位与情绪表盘逐字不变。

## 根因与两个通道

`utils/exchange_fee.get_history_data(symbol, period)` 的 DB 缓存路径直接返回
`MarketStore.get_history_df(symbol)`（默认 `tail(730)` ≈ 3 年），`period` 形同虚设。委员会经两个通道吃到它：

1. **宏观块 "MoM"**：`get_macro_data` / `_safe_last_change`（period="1mo"）取 `iloc[0]` 当"1 个月前"，
   进 Macro prompt 与日报；MCP `get_macro_snapshot.tip_1mo_pct` 同源。
2. **Macro agent 的 `get_history_data` 工具**（`capabilities/tools.py`）：LLM 自选 period，生产
   `memory/.state/tool_calls.jsonl` 自 2026-05-06 起 33,909 次调用（3mo 24,000 / 6mo 9,557，标的 ^VIX /
   ^TNX / DX-Y.NYB / TIP）。返回的 `first_close`、`n_days`、`cumulative_return_pct` 标着 3mo/6mo，实为同一段 730 行。

修复：`get_history_data` 读全历史 → `_apply_cutoff`（as_of）→ `calc.timeframe_analysis._apply_period`
（yfinance 词表，d=最近 N 根，mo/y=相对最后一根 bar 的日历回看）。回测 patch 同函数。

## 复现

只读生产库（`sqlite ... mode=ro`），不写任何东西：

```bash
uv run python experiments/macro-mom-fix-2026-10/replay_macro_mom.py \
  --db "$INVEST_HOME/db/market_data.db" \
  --daily-dir "$INVEST_HOME/memory/daily" \
  --tool-calls "$INVEST_HOME/memory/.state/tool_calls.jsonl" \
  --json experiments/macro-mom-fix-2026-10/result.json
```

- `old` = 截到 D → `tail(730)` → 首尾涨跌（修复前两个通道都是这一个数）
- `new_P` = 截到 D → `_apply_period(P)` → 首尾涨跌，P ∈ 1mo（宏观块）/ 3mo / 6mo（工具）
- `result.json` 为 2026-10-07 15:42 UTC 跑生产库的冻结结果（仅公开行情数字，无持仓 / PII）；库随行情
  更新，重跑时末几天数字会随新 bar 微动。

## 结果（2026-07-13 ~ 2026-10-07，63 个工作日）

方向（符号）与 old 相反的天数：

| 指标 | 宏观块 1mo | 工具 3mo | 工具 6mo |
|---|---|---|---|
| DXY 翻转 | 25 | 42 | 55 |
| TIP 翻转 | 59 | **63** | 61 |
| DXY 或 TIP 翻转（黄金货币因素至少一腿说反） | **59** | **63** | **63** |
| DXY 和 TIP 同时翻转 | 25 | 42 | 53 |
| 真实窗口"双顺风"（DXY↓ + TIP↑），old 为 63/63 | 4 | 0 | 0 |
| old 双顺风、真实却是双逆风（DXY↑ + TIP↓） | **25** | **42** | **53** |
| TNX / VIX 翻转 | 6 / 22 | 5 / 25 | 5 / 20 |

平均绝对偏差（百分点）：宏观块 TNX 4.20 / VIX 13.25 / DXY 4.87 / TIP 12.41；工具 3mo 4.96 / 14.19 / 5.76 / 13.89；
工具 6mo 8.60 / 21.41 / 6.73 / 13.52。

例：2026-10-07，日报 "DXY 102.00 (MoM -2.29%)"、"TIP +8.11% → gold tailwind"，真实 1 月（同一根 bar）DXY +3.26%、
TIP -2.68%；Macro agent 当天查 TIP "3mo" 拿到 +7.75%（730 行），真实 3 个月 -3.69%。

### 对账：委员会当天真的看到了 old 列吗？

通道 1，`memory/daily/<D>.md` 宏观块原文（≤D / <D 任一吻合；DXY 若是盘中价则用日报自己落的价位 / 同一基准价复算）：

| 指标 | 日报条数 | old（3 年）复现 | new（1 月）复现 |
|---|---|---|---|
| TNX | 63 | 63 | 1（价位 2 位小数舍入容差下的巧合） |
| VIX | 63 | 63 | 0 |
| DXY | 63 | 63 | 0 |
| TIP | 63 | 55 | 0 |

TIP 余下 8 天是日报跑在盘中 bar 上、事后被收盘价覆盖（TIP 行不落价位，无法重新锚定）。

通道 2，`tool_calls.jsonl` 里 end_date 落在窗口内的 3mo/6mo 调用（按返回的 end_date 截断复算 `first_close`）：

| 调用 | 条数 | n_days=730 | old 基准价复现 | new 基准价复现 |
|---|---|---|---|---|
| DXY 3mo | 1,173 | 1,173 | 1,173 | 0 |
| TIP 3mo | 991 | 991 | 991 | 0 |
| VIX 3mo | 983 | 983 | 983 | 0 |
| TNX 6mo | 1,111 | 1,111 | 1,111 | 0 |
| TNX 3mo | 28 | 28 | 28 | 0 |

## 不变量（同一修复的另一面）

喂 `compute_metrics` / `analyze_multi_timeframe` / VIX 分位的调用方改拉 `METRICS_PERIOD="5y"`
（⊇ 旧 730 行窗；日历 "2y" 只有 ~484–502 根 < `TRADING_DAYS_2Y=504`）。在生产库副本上对
GC=F / 510300.SS / 1024.HK / AAPL / ^GSPC / TLT / BTC-USD / 0700.HK 前后对比：regime 标签、
regime brief、多周期分析文本、sentiment brief、VIX 分位、dreaming 决议日 regime、回测
（2024-05-01 / 2026-07-13）**逐字相同**；只有全样本统计 `volatility_annualized` /
`max_drawdown` / `n_samples`（无任何下游消费，仅 `/api/regime` 原样透出）随窗口变长而变。
