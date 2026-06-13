# 数据字典

## `data/<window>_<model>.jsonl` — Phase A 分析师报告（一行一个决策点×资产×分析师）

| 字段 | 类型 | 含义 |
|---|---|---|
| `key` | str | `"{date}\|{symbol}\|{analyst}"`，唯一键（断点续跑去重用）|
| `date` | str | 决策日 `YYYY-MM-DD`（周频）|
| `symbol` | str | `GC=F`(COMEX 黄金) 或 `NDQ.AX`(纳指 100 ETF) |
| `analyst` | str | `fundamental` / `news` / `sentiment` / `combined`（三包合一）|
| `stance` | str | LLM 强制表态 `bullish`/`bearish`/`neutral`（从 report 确定性解析）|
| `confidence` | str | LLM 自报 `low`/`medium`/`high` |
| `det_stance` | str | **基线③**：同份输入数据的机械映射 stance（无 LLM；news 无此项=neutral）|
| `report` | str | LLM 完整报告原文（含 `STANCE:`/`CONFIDENCE:`/`ONE_LINER:` 尾标）|
| `tokens_in/out` | int | 该次调用 token 计数 |

文件命名：`<window>_<model>.jsonl`，window ∈ {2024(2024-01→2026-04 牛市), 2022(熊市)}，
model ∈ {mimo-flash(生产同款 mimo-v2.5-pro), deepseek-reasoner, deepseek-chat}，
`combined_` 前缀 = 三源联合分析师。

## `inputs/decision_dates_<window>.json`

```json
{"dates": ["2024-01-01", ...], "assets": ["NDQ.AX", "GC=F"]}
```
决策日窗口定义（周频，与历史 A/B/C 消融同款）。零前视靠序列截断到 asof。

## `inputs/cot_gold_2021-2026.csv`

CFTC COT 黄金期货非商业持仓周报（deacot 年档拼接）。字段：`as_of`(报告日) +
非商业多/空/净持仓。as_of+3 日发布滞后对齐（point-in-time，零前视）。

## `inputs/news_cache_gdelt.jsonl` — GDELT 头条缓存（一行一个 `{date}|{symbol}`）

| 字段 | 含义 |
|---|---|
| `key` | `"{asof}\|{symbol}"` |
| `articles[]` | `{title, domain, seendate}`——**仅标题无正文**（GDELT DOC API 限制），决策日前 7 天窗 |
