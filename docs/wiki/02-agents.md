---
type: wiki-chapter
title: 4 角色委员会
tags: [committee, agents, regime, llm, cross-challenge]
intent: 委员会角色设计与辩论协议
schema_source:
  - core/regime.py:THRESHOLDS
  - core/committee/debate.py:run_committee
  - core/committee/cio_parse.py:parse_cio_memo
documents:
  endpoints:
    - GET /api/regime_rules
  config_keys:
    - verdict.risk_profile
    - verdict.buy_confidence_overdrive
  symbols:
    - run_committee
    - run_committee_for_symbol
    - parse_cio_memo
    - _check_convergence
    - THRESHOLDS
---

# 4 角色委员会

> 4 个独立 LLM session，信息隔离 + cross-challenge 多轮辩论 + REGIME 中性概率口径（仅 crash/unknown 保留约束）。
> 这一章解释**他们各自看到什么、被什么约束、怎么互相挑战**。

[← 01-architecture](01-architecture.md) · [Wiki 索引](README.md) · [03-dreaming →](03-dreaming.md)

---

## 角色矩阵

| Role | 能看到 | 被屏蔽 | 输出格式 | 文件 |
|------|--------|--------|----------|------|
| **Macro Strategist** | VIX / TNX / 全球宏观 brief + **盘中事件上下文 event_brief（仅 Macro 看得到，事件 RAG 严格隔离）** | 用户持仓、技术指标 | `SIGNAL` + `STRENGTH` + `SCORE` | `agents/macro_strategist.py` |
| **Quant Analyst** | 技术指标（Wilder RSI / MA / 真百分位 / 真 TR ATR / RVOL）+ REGIME 中性概率口径 + 确定性事实块（估值/情绪） | 用户持仓、Dreaming insights | 看涨/看跌 + 信号强度 | `agents/quant.py` |
| **Wealth Context Officer**（第 5 角色，跨资产共享） | user.md 的 wealth_context（家族 backup / 应急金 / 账户性质）+ portfolio cash | 技术指标、Macro view、个股持仓明细 | 真实流动性评级（给 Risk + CIO 用） | `agents/wealth_context_officer.py` |
| **Risk Officer** | 持仓集中度 / 浮盈缓冲 / Dreaming insights + **Wealth Context Officer 真实流动性视角** | 技术指标、Macro view | 风险等级 + 集中度评分 | `agents/risk_officer.py` |
| **CIO** | 三人完整 transcript + Wealth Context 视角 + 用户 risk_level（含在 portfolio_summary 文本里） | — | 5 选 1 verdict + confidence + alloc | `agents/cio.py` |

**信息隔离的目的**：避免 LLM 在同一 context 里相互污染观点。Quant 不知道用户亏了多少（不会偏向"赶紧补仓"），Risk Officer 不知道 RSI（不会被技术信号牵着鼻子走）。**注意这种"物理隔离"只在 Round 1 严格成立**——见下文 [Cross-Challenge 协议](#cross-challenge-协议) 的轮次说明。

---

## 一次完整跑 6 步（Direct 路径视角）

下面的 round 划分**对应 Direct 路径**（`core/committee/` 包，包括 cron / Web GUI / skill `run_committee`）。Coordinator 路径（Claude Code spawn 4 subagent）分法略不同——见本节末"两条路径 LLM 调用数对照"。

```
Round 0  ─ Macro 1 LLM call（跨资产共享，每次跑只 1 次）
            ↓
Round 1  ─ Quant + Risk 2 LLM call (并行，独立陈述)
            ↓
Round 2  ─ Quant 看 Risk 上轮 + Risk 看 Quant 上轮（cross-challenge）
            ↓
Round 3  ─ 同上（如果未收敛）
            ↓
[收敛检测 SIGNAL+STRENGTH 两轮稳定 → 提前退出]
            ↓
Round 4  ─ CIO 看完整 transcript，1 LLM call 出 verdict
```

实测耗时（DeepSeek-Chat + 2 资产并行 + 收敛退出）：**~16 秒**（journal log 实证）。

文件：`core/committee/debate.py:run_committee` 是入口；`core/runner/session.py:run_committee_for_symbol` 是端到端封装。

### 两条路径 LLM 调用数对照（避免歧义）

容易混淆点：**4 个 prompt 模板（角色）≠ LLM 调用数**。Round 2..N 重新调
Quant 和 Risk（同样的 *角色*，新的 prompt）。

| 路径 | 场景 | Macro | R1 | R2..N | CIO | 单资产典型总数 |
|------|------|-------|-----|-------|-----|--------------|
| **Direct** | 多资产并行优化 / 任意 agent 单资产 | 1（**跨资产共享**）| 2 (Quant+Risk) | 2/轮 | 1 | 5 (收敛) ~ 9 (4 轮) per asset |
| **Coordinator** | Claude Code 单资产 spawn subagent | 1（**进 Round 1 一起 spawn**）| 3 (Macro+Quant+Risk) | 2/轮 | 1 | 6 (1 R2) ~ 10 (3 R2) |

**为什么 Coordinator 路径不共享 Macro**：用户每次问一个资产，没有"跨资产复用"的池子；
让 Macro 进 Round 1 一起 spawn 反而更并行（Claude `Agent({...})` 同消息发 3 个就并发跑）。

**Direct 路径多资产实测**：2 资产收敛 = 1 + 5×2 = 11 calls；2 资产 max=4 不收敛 = 1 + 9×2 = 19 calls；
journal 实证 16 calls 介于两者间。

引用本图前先想清楚是哪条路径——Coordinator 文档（[skills/invest/references/committee-protocol.md](https://github.com/longsizhuo/openInvest/blob/main/skills/invest/references/committee-protocol.md)）按 Coordinator 路径写，本文按 Direct 路径写，**两份各自正确**但前提不同。

---

## REGIME 分类与中性概率口径

**演进（2026-05-31 拆方向锁）**：旧版把 regime 翻译成方向预设喂给 Quant（"uptrend
禁 bearish / 不抄底 / 逢高减"）。同源 draw 消融证明：**整个方向锁层 = 1 个杠杆旋钮
（uptrend 锁）+ 2 个摆设（downtrend 锁回测 no-op、crash 锁永不触发）**，不是 alpha。
现版 regime 是**事实背景**：STRATEGY_HINT 中性引用该 (asset, regime) 的 OHLC 30d
forward return 概率口径（中位 / 跌破现价概率 / 样本数），方向判断交回 LLM + 数据。
uptrend 杠杆效应保留为显式 `verdict.risk_profile=aggressive` 开关（默认 steady 不开）。
快崩防御独立于 regime（VIX 哨兵 + ATR 波动突变比 → parse_cio_memo 确定性买侧降级）。

### 6 类 regime（uptrend / downtrend / range_bound / crash / recovery / unknown）

源：`core/regime.py:THRESHOLDS`（默认阈值 + `ASSET_OVERRIDES` per-asset 覆盖）

| Regime | 触发条件 | 给 Quant 的口径 |
|--------|---------|-------------------|
| `uptrend` | `(MA20 − MA120) / MA120 ≥ +trend_ma_spread_pct%`（默认 3%，per-asset 覆盖：金 5% / 纳指 4% / 加密 8%） | 中性概率口径（30d 中位/跌破概率/n），方向自决 |
| `downtrend` | `(MA20 − MA120) / MA120 ≤ −trend_ma_spread_pct%` | 中性概率口径，方向自决 |
| `range_bound` | MA 纠缠（spread 在 ±阈值内）+ 波动正常 | 中性概率口径 + 当前分位，方向自决 |
| `crash` | **双触发器（任一即触发）**：① 急跌 `atr_pct ≥ crash_atr_pct_min`（per-asset）**且** `return_30d ≤ −20%`；② 深跌 `return_30d ≤ −30%`（不看波动） | **强制 neutral**（可执行性约束：崩盘期任何方向都难理性执行） |
| `recovery` | crash 未触发 + 从近 30 日低点反弹 ≥ 10% + 价格仍在低位（真百分位 < 50%） | 中性概率口径，方向自决 |

> 说明（与 `core/regime.py` 实现对齐，2026-05-26）：
> - 趋势判断用的是 **MA20 对 MA120 的偏离度**（单一 spread 阈值），**不是** MA20>MA50>MA200 三线多头排列；代码里只算 MA20/MA120/MA250，没有 MA50/MA200。
> - `crash` 是 **双触发器（OR）**：路径一急跌（波动腿 AND 20% 跌幅腿）+ 路径二深跌（30 日跌 ≥ 30%，不看波动）。路径二专门抓"低波动慢阴跌"——深跌本身即 crash，无需高 ATR 佐证。
> - `recovery` 为无状态实现：用"已从近 30 日低点反弹 ≥ 10% 且分位 < 50%"近似"刚走出 crash"。
> - 优先级：`crash`（最高）→ `recovery` → `uptrend`/`downtrend` → `range_bound` → `unknown`（缺数据）。

REGIME brief 在 Quant 的 prompt 里以**最高优先级**呈现（开头第一段）。

### CIO Sanity Check

CIO 输出 verdict 后，`parse_cio_memo()` 自动校验：

| 异常 | 自动纠正 |
|------|----------|
| `BUY` 且 `confidence ≥ buy_confidence_overdrive`(0.95) | 降级 ACCUMULATE + conf 0.6（防过度自信 / prompt injection）|
| `alloc_cny` 超 ceiling | clamp（防 LLM 报天文数字）|
| brief 含 `[WORKER_UNAVAILABLE]` | 强制 HOLD + conf 封顶（garbage in 不出高置信决议）|
| SOLVENCY=strong + TRIM(concentration) | 强制 HOLD（兜底充足，集中度不触发减仓）|
| TRIM 但买回点缺失 / ≥ 现价 | 降级 HOLD（卖了买不回更低 = 纯亏）|
| 快崩防御触发（VIX 哨兵 OR ATR 突变比）| BUY→ACCUMULATE / ACCUMULATE→HOLD（确定性买侧降级，独立于 regime）|
| `risk_profile=aggressive` + uptrend + HOLD | 升级 ACCUMULATE（显式杠杆档，默认 steady 不动）|

源：`core/committee/cio_parse.py:parse_cio_memo`（Sanity 0-5 + Defense + risk_profile，全部确定性后处理）。

---

## Cross-Challenge 协议

### Round 1 输入（独立陈述）

**Quant 看到的**：
```
# 资产: BetaShares Nasdaq 100 ETF (NDQ.AX)
# 市场 Regime（事实背景 + 历史概率参考）：
  uptrend · MA20 高于 MA120 +8.5% (≥ 4%) · 价格百分位 78%
  → 该 regime 历史 30d forward return：中位 +1.4%、跌破现价概率 35%、n=1423
    （结合分位 / RSI 自行判断方向，不预设方向）
# 市场数据（技术指标 + 多周期）：
  RSI(14, Wilder): 67, MA120: 33.8, MA250: 30.1, 价格百分位(2y): 78%, RVOL(20): 0.9x, ...
请按 Quant Analyst 格式输出技术信号。
```

**Risk Officer 看到的**：
```
# 资产: BetaShares Nasdaq 100 ETF (NDQ.AX)
# 用户当前持仓：
  CNY: ¥50,000 / AUD: $1,000 / NDQ.AX 50 股 @ A$38.50（浮盈 +12%）
# 长期模式（Dreaming，学委员会自己的 verdict vs 实际盘）：
  ⚠️ downtrend 里对 NDQ.AX 反复 HOLD，30 天后 100% 踏空（平均 +12%，n=11）→ 降低 HOLD 倾向
请按 Risk Officer 格式输出风险评估。
```

注意：**Quant 不知道用户持仓**，**Risk 不知道 RSI**。这是物理隔离。

> ⚠️ **轮次 nuance（重要）**：物理隔离**只在 Round 1 成立**。进入 Round 2+ 的
> cross-challenge 后，每个 agent 会看到对方**上一轮的完整 transcript**（见下一节），
> 因此 Risk 的持仓数字会经 Risk 的发言进入 Quant 的 context、Quant 的技术数字会经
> Quant 的发言进入 Risk 的 context——**R2+ 不再是物理隔离，靠 prompt 约束兜底**：
> `quant/SKILL_rebuttal.md` 和 `risk_officer/SKILL_rebuttal.md` 明确规定"只引用对方的
> *结论*（SIGNAL/STRENGTH），不要拿对方的原始数字（RSI/分位/集中度）再算一遍"。

### Round 2..N 输入（cross-challenge）

每个 agent 看到对方上一轮的输出，被要求**回应**而不是重新独立陈述：

```
# 现在是第 2 轮 cross-challenge（最多 4 轮）
# 上一轮 Quant 说：
  [Quant Round 1 全文]
# 上一轮 Risk 说：
  [Risk Round 1 全文]

请重新评估：
- 你坚持原判断还是被对方说服？为什么？
- 哪些数据是对方忽略的？
- 输出新一版你的 SIGNAL + STRENGTH（如果改了，说明改的依据）
```

这一步让 LLM **真讨论**，不是各说各话。

### 收敛检测

实现：`core/committee/debate.py:_check_convergence`

```python
# 连续两轮，Quant 和 Risk 各自的 SIGNAL + STRENGTH 都稳定
if (quant_signal_N == quant_signal_N-1 and
    abs(quant_strength_N - quant_strength_N-1) < 1.0 and
    risk_signal_N == risk_signal_N-1 and
    abs(risk_strength_N - risk_strength_N-1) < 1.0):
    converged = True  # 提前退出，不浪费 token
```

实测大多数 committee 在 Round 2-3 收敛（实证：`final_round=3, converged=True` 是常见值）。

---

## CIO 的 5 个 verdict 选项

CIO 不能编造，必须从这 5 个里选：

| Verdict | 含义 | 典型 alloc 区间 |
|---------|------|----------------|
| `BUY` | 强烈推荐买入 | +3000 ~ +50000 CNY |
| `ACCUMULATE` | 缓慢加仓（建议分批）| +1000 ~ +5000 CNY |
| `HOLD` | 不动 | 0 |
| `TRIM` | 部分减仓（释放子弹）| -3000 ~ -20000 CNY |
| `SELL` | 全部卖出（极少见）| -20000 ~ -100000 CNY |

CIO 同时输出：
- `confidence: 0.0~1.0`（被 sanity check clamp）
- `alloc_cny: int`（建议金额）
- `dominant_view: macro|quant|risk`（哪一方说服了 CIO）
- `execution_plan` + `risk_plan`（详细执行 + 止损）

---

## Prompt 在线查看

GUI `/committee` → "4 角色 + 规则" tab 直接展示所有 4 个角色的完整 system prompt + REGIME 阈值表。
任何人都能看到 LLM 被怎么约束的，无黑盒。

后端端点：`GET /api/regime_rules` → 返回完整 prompt + REGIME thresholds + CIO sanity check 清单。

---

## 双执行路径

注意：相同的 4 个 prompt + 相同的 cross-challenge 协议，**有两套执行实现**：

| 路径 | 协调者 | Worker 实现 | 模型 |
|------|--------|-------------|------|
| **Skill** | 用户的 Claude | Claude `Agent({subagent_type})` 真 spawn 4 个 subagent | Claude 4 |
| **Web/Cron** | `core/committee/` | 4 个 `SDKAgent` + ThreadPoolExecutor 同进程多线程 | DeepSeek |

详见 [04-execution-paths.md](04-execution-paths.md) 和 [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md)。

---

## 下一步

→ [03-dreaming.md](03-dreaming.md) — 长期记忆怎么沉淀（Risk Officer 看到的"历史行为模式"哪来的）

→ [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md) — 为什么保留双路径不合并

→ [07-extending.md#加新-agent-角色](07-extending.md#加新-agent-角色) — 想加 ESG 分析师怎么改
