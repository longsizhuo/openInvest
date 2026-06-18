---
type: wiki-chapter
title: Dreaming 记忆整合
tags: [dreaming, memory, committee, regime, insights]
intent: 记忆整合
schema_source:
  - jobs/dreaming.py:light_sleep
  - jobs/dreaming.py:rem_sleep
  - jobs/dreaming.py:deep_sleep
  - core/runner/loaders.py:load_prior_insights
documents:
  endpoints: []
  config_keys: []
  symbols: [light_sleep, rem_sleep, deep_sleep, load_prior_insights]
---

# Dreaming 记忆整合

> LLM 没有跨会话记忆。Dreaming 是 OpenClaw 风格的三阶段后台整合，
> 让"委员会过去在下跌趋势里反复 HOLD、结果踏空"这件事，今天的 CIO 仍然知道。

[← 02-agents](02-agents.md) · [Wiki 索引](README.md) · [04-execution-paths →](04-execution-paths.md)

---

## 问题

每次跑委员会，4 个 LLM 角色都是"全新出生"：

- Quant 不知道上次在 RSI>70 时给的信号事后对不对
- Risk Officer 不知道半年前被警告过集中度
- CIO 不知道哪些 verdict 历史命中率低、哪类决策反复踏空

**结果**：永远在重复犯同样的错。

## 学什么：委员会自己的 verdict vs 事后实际盘（2026-05-26 换源）

> **重要变更**：Dreaming 早期学的是**用户实际成交**（`portfolio_history.jsonl`）。
> 但单人用户交易量太小（个位数～十几笔），从成交里几乎学不到任何可统计的模式。
>
> 现在改成学 **委员会自己出的 verdict + 事后市场涨跌**。委员会每天对每个资产都
> 出 verdict，每条 verdict + 事后 7d/30d 行情就是一个样本——**与用户交不交易无关**，
> 样本量随系统运行天然增长。

数据源是 `jobs/verdict_review.py` 产出的 `memory/.dreams/verdict_review.jsonl`，
每行一条 verdict 的事后复盘（已做 proxy-aware 黄金 CNY/克换算 + forward window
成熟度过滤，见 [05-data-model](05-data-model.md)）。

## 三阶段后台整合

每天凌晨 03:00 跑（`jobs/dreaming.yml`）：

```
                              ┌─────────────────┐
   verdict_review.jsonl   →   │  Light Sleep    │  → short-term-recall.json
   + 决议日 regime            │  (摄入信号)      │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │  REM Sleep      │  → candidates.json
                              │  (按 verdict×    │
                              │   regime 聚合)   │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
        阈值门 score≥0.8       │  Deep Sleep     │  → insights/*.md
              count≥3      →  │  (固化模式)      │     + MEMORY.md 索引
                              └─────────────────┘
                                       │
                                       ▼
                          下一次委员会的 CIO context
                          注入 insights（含告诫）
```

**核心思想**：阈值门保证只有**反复出现且证据充分**的模式才进入长期记忆，避免 LLM
把噪音当信号。所有数值统计都是确定性算出，**LLM 不能改**。

---

## 1. Light Sleep（摄入信号）

源：`jobs/dreaming.py:light_sleep`

读 `verdict_review.jsonl`，每条 verdict 补上**决议日的市场 regime**
（用 committee 同款 `core.regime.classify_regime`，截断到决议日防穿越）。
只收 outcomes 已成熟（至少一个 window 有 hit）的样本。

输出 `.dreams/short-term-recall.json`，每条信号形如：
`{decision_date, asset, verdict, regime, outcomes{7d,30d}, hits, macro_shock, source}`。

---

## 2. REM Sleep（按 verdict × regime 聚合）

源：`jobs/dreaming.py:rem_sleep`

按 `(asset, verdict, regime)` 分桶，统计每个组合 7d/30d 的命中率。例如：

- "range_bound 里委员会 TRIM 黄金 → 7d 命中率 80%（n=5）"
- "downtrend 里委员会 HOLD 纳指 → 30d 实际涨 +12%，踏空 100%（n=11）"

**两个纪律**：
- **黑天鹅免责**：`macro_shock` 标记的样本剔除，不让委员会因不可预测的突变被记成"判断差"。
- **只看 7d/30d，不看 1d**：1 天内几乎不动，HOLD 会被自动判命中，是 horizon 假象。

### HOLD 的机会成本感知（关键设计，2026-05-26）

HOLD 的"命中"用**波动率感知阈值**判定，不是写死的 3%：

> "没动" = `K_FLAT × 该资产 atr_pct × √窗口天数`，再封顶 8%（`K_FLAT=1.0` 固定常数）

为什么不写死 3%：黄金一周波动 ~1%、纳指 ~1.8%、加密 ~3-5%，统一阈值对黄金太松、
对加密太紧。改成"小于该资产平时这段时间正常会动的幅度"才算 HOLD 命中。

> **关键原则（防 reward hacking）**：适应的是**测量值**（`atr_pct` 实时从行情算），
> 固定的是**规则**（`K_FLAT` 常数）。**绝不让系统自学习这把"给自己打分的尺子"**——
> 否则它有动机把及格线挪低，把踏空的 HOLD 都算成命中，越学越保守（Goodhart 定律）。
> 真要调 `K_FLAT`，只能在 Phase 3 有 holdout + 足够样本后**离线 backtest 选、人拍板**。

HOLD 没命中时再分方向：

| 情况 | 含义 | 处理 |
|---|---|---|
| 市场涨（`missed_up`）| 子弹揣兜里踏空了 | **反保守告诫**，标 `kind=caution` |
| 市场跌（`avoided_down`）| 揣着子弹躲过下跌 | 中性，不罚 |
| 没动（flat）| HOLD 真的对了 | 命中，`kind=reliable` |

踏空（`missed_up_rate`）比命中还频繁的 HOLD 桶 → 标 `caution`，固化成告诫型 insight。

**输出**：`.dreams/candidates.json`

---

## 3. Deep Sleep（固化）

源：`jobs/dreaming.py:deep_sleep`

**阈值门**：`score ≥ 0.8` 且 `count ≥ 3`。评分（`_score`）：

- `reliable` 模式：`命中率 × 0.7 + 样本量 × 0.3`（方向判断越准越有价值）
- `caution` 模式：`踏空率 × 0.7 + 样本量 × 0.3`（踏空越频繁，告诫越有价值）

通过门的写入 `memory/insights/*.md`（一条一文件）+ 更新 `MEMORY.md` 索引。
可选 LLM 验伪（`INVEST_DREAMING_LLM_VERIFY=1`）再过滤一道 spurious correlation。

### 固化出的 insight 例子（caution）

```markdown
# ⚠️ 告诫: downtrend 里对 NDQ.AX HOLD 频繁踏空

## 统计
- 样本数: 11
- 30天后命中率: 0.0%
- 平均 30天后实际涨跌: +12.02%
- 踏空率(HOLD 但市场涨): 100.0% / 躲跌率: 0.0%

## 解读
历史上当市场处于 downtrend 时，委员会对 NDQ.AX 给出 HOLD，但 30 天后有 100%
的情况市场明显上涨（平均 +12.02%，n=11）——子弹揣在兜里踏空了。

> 行动建议：该 regime 下应降低 HOLD 倾向，更积极考虑 BUY/ACCUMULATE。
```

CIO 在下一次决策时通过 `MEMORY.md` 索引看到这条 → 在该 regime 下更不容易无脑 HOLD。
**这就是"系统在学习"对抗过度保守的具体机制**——不是 LLM 自己悟，是统计 + 阈值门固化。

---

## 4. 怎么被 CIO / Risk Officer 看到

`core/runner/loaders.py:load_prior_insights` 在跑委员会前读 `memory/insights/*.md`，
注入到 prompt 上下文（`prior_insights`）。这是 [三路径单一可信源](04-execution-paths.md)
的一部分——Skill / Web / Cron 三条路径都经 `run_committee_session` 注入，不漏。

---

## 5. 与 DSPy 后验训练的关系

Dreaming（行为 insight）和 DSPy oracle 训练（[11-rl-training](11-rl-training.md)）是
**同一个"机会成本"哲学的两个注入口**：

- **Dreaming** → 提炼"在 X regime 下委员会这类判断准不准/踏不踏空"的可读 insight，注入 CIO prompt。
- **DSPy oracle** → 对历史每个时点算"事后最该做的 verdict"，训练 few-shot 校准委员会。

两者都在教委员会"市场给机会时别缩手"，互相印证。

---

## 6. GUI 透视

`/system` → "Dreams" tab 看 `short-term-recall.json` 规模 + 最近 events 流；
"长期模式" tab 看 `insights/*.md` 所有已固化模式。

---

## 7. 失败模式 / 已知坑

| 坑 | 缓解 |
|----|------|
| 样本量小，模式不稳 | 阈值门 `count≥3` + 黑天鹅免责；可用更多委员会运行/backtest 扩样 |
| Deep Sleep 写入并发（cron + 手动同时跑）| `core/consolidation_lock.py` 提供独占锁 |
| insights 文件越积越多 | 计划加"老化机制"：N 天未触发 → archive；目前未实现 |
| `K_FLAT` 等阈值过拟合 | 现为固定常数；真要调走 Phase 3 holdout + 离线 backtest + 人拍板 |

---

## 下一步

→ [02-agents.md](02-agents.md) 看 insights 在 prompt 里的具体位置
→ [11-rl-training.md](11-rl-training.md) 看 DSPy oracle 后验训练
→ 论文级背景：[OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)
