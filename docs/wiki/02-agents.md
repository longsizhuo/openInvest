# 4 角色委员会

> 4 个独立 LLM session，信息隔离 + cross-challenge 多轮辩论 + REGIME 硬约束。
> 这一章解释**他们各自看到什么、被什么约束、怎么互相挑战**。

[← 01-architecture](01-architecture.md) · [Wiki 索引](README.md) · [03-dreaming →](03-dreaming.md)

---

## 角色矩阵

| Role | 能看到 | 被屏蔽 | 输出格式 | 文件 |
|------|--------|--------|----------|------|
| **Macro Strategist** | VIX / TNX / USDCNY / 全球宏观 brief | 用户持仓、技术指标 | `SIGNAL` + `STRENGTH` + `SCORE` | `agents/macro_strategist.py` |
| **Quant Analyst** | 技术指标（RSI/MA/分位）+ REGIME 硬约束表 | 用户持仓、Dreaming insights | 看涨/看跌 + 信号强度 | `agents/quant.py` |
| **Risk Officer** | 持仓集中度 / 浮盈缓冲 / Dreaming insights | 技术指标、Macro view | 风险等级 + 集中度评分 | `agents/risk_officer.py` |
| **CIO** | 三人完整 transcript + 用户 risk_level | — | 5 选 1 verdict + confidence + alloc | `agents/cio.py` |

**信息隔离的目的**：避免 LLM 在同一 context 里相互污染观点。Quant 不知道用户亏了多少（不会偏向"赶紧补仓"），Risk Officer 不知道 RSI（不会被技术信号牵着鼻子走）。

---

## 一次完整跑 6 步

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

文件：`core/committee.py:run_committee` 是入口；`core/committee_runner.py:run_committee_for_symbol` 是端到端封装。

---

## REGIME 硬约束

**为什么需要硬约束**：LLM 在牛市顶部还能 hallucinate 出 "BUY"。把可确定性算法的部分（趋势 / 分位）从 LLM 里拿出来，写成确定性规则喂给 LLM 当**强制前置**。

### 5 类 regime + 5 阈值

源：`core/regime.py:THRESHOLDS`

| Regime | 触发条件 | 对 Quant 的硬约束 |
|--------|---------|-------------------|
| `uptrend` | MA20 > MA50 > MA200 + 价格分位 ≥ 60% | **禁** bearish 信号 |
| `downtrend` | MA20 < MA50 < MA200 + 价格分位 ≤ 40% | **禁** bullish 信号 |
| `range_bound` | MA 多空胶着 + 分位 20-80% | 跟随分位：≤20% 偏 bullish，≥80% 偏 bearish |
| `crash` | 30 日跌幅 ≥ 20% + ATR > 2× 历史中位数 | **强制 neutral**（任何方向不可执行） |
| `recovery` | 从 crash 反弹 ≥ 10% + 分位 < 50% | 允许 cautious bullish |

REGIME brief 在 Quant 的 prompt 里以**最高优先级**呈现（开头第一段）。

### CIO Sanity Check

CIO 输出 verdict 后，`parse_cio_memo()` 自动校验：

| 异常 | 自动纠正 |
|------|----------|
| `confidence ≥ 0.95` 且 alloc 不是 ¥0 | 降到 0.85（防 LLM 过度自信）|
| `alloc_cny > 100_000` | clamp 到 100_000（防 LLM 报天文数字）|
| verdict=`BUY` 但 regime=`crash` | 降到 `HOLD`（硬约束打架，REGIME 优先）|
| confidence < 0.3 | 视为"无效决议"，触发 retry |

源：`core/committee.py:parse_cio_memo` + `_sanity_check`。

---

## Cross-Challenge 协议

### Round 1 输入（独立陈述）

**Quant 看到的**：
```
# 资产: BetaShares Nasdaq 100 ETF (NDQ.AX)
# 市场 Regime（确定性算出，必须遵循）：
  uptrend · MA20>MA50>MA200 · 价格分位 78%
  → 禁 bearish 信号
# 市场数据（技术指标 + 多周期）：
  RSI: 67, MA20: 35.2, MA50: 33.8, ...
请按 Quant Analyst 格式输出技术信号。
```

**Risk Officer 看到的**：
```
# 资产: BetaShares Nasdaq 100 ETF (NDQ.AX)
# 用户当前持仓：
  CNY: ¥50,000 / AUD: $1,000 / NDQ.AX 50 股 @ A$38.50（浮盈 +12%）
# 长期行为模式（Dreaming）：
  历史在 RSI>70 时加仓，事后 30 天平均 -3.2%（4/5 次）
请按 Risk Officer 格式输出风险评估。
```

注意：**Quant 不知道用户持仓**，**Risk 不知道 RSI**。这是物理隔离。

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

实现：`core/committee.py:_check_convergence`

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
| **Web/Cron** | `core/committee.py` | 4 个 `SDKAgent` + ThreadPoolExecutor 同进程多线程 | DeepSeek |

详见 [04-execution-paths.md](04-execution-paths.md) 和 [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md)。

---

## 下一步

→ [03-dreaming.md](03-dreaming.md) — 长期记忆怎么沉淀（Risk Officer 看到的"历史行为模式"哪来的）

→ [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md) — 为什么保留双路径不合并

→ [07-extending.md#加新-agent-角色](07-extending.md#加新-agent-角色) — 想加 ESG 分析师怎么改
