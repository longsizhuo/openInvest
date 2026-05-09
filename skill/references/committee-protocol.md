# 委员会协议（用户问"该不该买/卖 X"时读这个）

用户说了 **"该不该买/卖 X"** / **"分析一下 X"** / **"跑委员会 X"**——严格按
6 个 stage 跑。

> **路径前提**：本文档描述的是 **Skill 路径**（你在 Claude Code 里 spawn
> subagent）。Skill 模式下 Macro 不共享，每次进 Round 1 一起 spawn → R1 共
> 3 个 worker。Web/Cron 路径里 Macro 跨资产共享，R1 只有 Quant + Risk 2 个
> worker，详见 [docs/wiki/02-agents.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/02-agents.md#两条路径-llm-调用数对照)。
> 不要混着引用两份。

## Stage 0：同日检查（避免重复跑）

```bash
ls "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md" 2>/dev/null
```

如果文件存在，**直接读它，不要重新跑**。告诉用户：
> "今天已经跑过 <SYMBOL> 了，verdict 是 X (confidence Y)。要重跑吗？"

一次完整委员会要消耗用户 ~15-60s 的 Claude budget——同一答案别烧两遍。

## Stage 1：拿 brief

```bash
~/.claude/skills/invest/scripts/run.sh prepare_committee <SYMBOL>
```

返回 JSON 含所有需要的字段：

| 字段 | 用在 |
|------|------|
| `asset` | 4 个 worker 都引用 |
| `portfolio_summary` | Risk Officer prompt |
| `macro_data` | Macro Strategist prompt |
| `market_data` | Quant prompt |
| `regime_brief` | **关键** —— Quant Round 1 + Round 2 prompt 都要塞（见警告）|
| `prior_insights` | Risk Officer prompt（如果 Dreaming 没跑过会是空）|
| `prompts.{...}` | `agents/*.py` 里的 prompt 模板（原样用）|
| `instructions` | 单资产 orchestration tip（**读它**！）|

**⚠ regime_brief 警告**：这是 Python 算出来的市场 regime（uptrend / downtrend /
range_bound / crash / recovery）+ REGIME 硬约束（如 "uptrend 禁 bearish"）。
**忘了塞给 Quant，就会回退到老 bug 路径**——Quant 在 range_bound 底部乱喊
bearish。Round 1 + Round 2 Quant prompt **都要原样塞**进去。

## Stage 2：Round 1 —— 3 个 worker 并行

**3 个 `Agent({...})` 调用必须在一条消息里发**——这样它们真正并行跑。
每个 worker 自己一个 context window，信息物理隔离：

```javascript
Agent({
  description: "Macro analysis",
  subagent_type: "general-purpose",
  prompt: "<原样粘 prompts.macro_strategist>\n\n# 当前宏观数据:\n<原样粘 macro_data>"
})

Agent({
  description: "Quant analysis (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<原样粘 prompts.quant_round1>\n\n# 市场 Regime (确定性算出，必须遵循):\n<原样粘 regime_brief>\n\n# 市场数据:\n<原样粘 market_data>"
})

Agent({
  description: "Risk Officer (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<原样粘 prompts.risk_round1>\n\n# 用户持仓:\n<原样粘 portfolio_summary>\n\n# 长期模式:\n<原样粘 prior_insights>"
})
```

每个 worker 通过 `<task-notification>` 返回。**等 3 个全回来再进 Stage 3**。

## Stage 3：Round 2 —— Cross-challenge（2 个 worker 并行）

Quant 和 Risk 现在能看到对方的 R1 输出，调整自己。Macro 不需要 Round 2
（它跨资产共享）。两个 Agent 调用一条消息发：

```javascript
Agent({
  description: "Quant Round 2 (sees Risk's report)",
  subagent_type: "general-purpose",
  prompt: "<原样粘 prompts.quant_round2_after_risk>\n\n# 市场 Regime (Round 1 给你的事实，Round 2 仍然有效):\n<原样粘 regime_brief>\n\n# Round 1 你自己的输出:\n<quant R1 result>\n\n# Risk Officer 的报告:\n<risk R1 result>"
})

Agent({
  description: "Risk Round 2 (sees Quant's signals)",
  subagent_type: "general-purpose",
  prompt: "<原样粘 prompts.risk_round2_after_quant>\n\n# Round 1 你自己的输出:\n<risk R1 result>\n\n# Quant 的技术信号:\n<quant R1 result>"
})
```

## Stage 4（可选）：未收敛时跑 Round 3+

Web/Cron 路径自带收敛检测，最多跑到 `max_debate_rounds=4`。Skill 模式实践中
很少需要超过 2 轮——只在以下两个**同时满足**才跑 Round 3：

- Quant 和 Risk 的 SIGNAL 在 R1→R2 之间翻面了（被对方说服），并且
- 翻面后的新 SIGNAL+STRENGTH 还互相严重分歧

否则跳到 Stage 5。

**收敛规则**（什么时候停辩论）：
- Quant SIGNAL 和上一轮一样，且 |STRENGTH delta| ≤ 1.0
- Risk SIGNAL 和上一轮一样，且 |STRENGTH delta| ≤ 1.0
- 两个都满足 → 收敛，进 CIO

## Stage 5：CIO 综合 —— **你来写**，不 delegate

CIO 角色是**你**（orchestrator）。按 Claude Code Coordinator Mode 的原则：

> "You are a coordinator. Synthesize results and communicate with the user.
> Never write 'based on your findings' — that delegates understanding."

读完所有 worker 输出（Macro + Quant R1/R2 + Risk R1/R2）+ `portfolio_summary`，
按 `prompts.cio` 格式写完整 CIO memo。

### CIO 输出必填字段

- `VERDICT`：`BUY` / `ACCUMULATE` / `HOLD` / `TRIM` / `SELL` 五选一
- `CONFIDENCE`：0.0–1.0
- `DOMINANT_VIEW`：哪一方说服了你（`macro` / `quant` / `risk`）
- `SUGGESTED_ALLOC_CNY`：整数（正 = 买更多，负 = 减仓）
- `EXECUTION_PLAN`：怎么实际执行（lump-sum / DCA / grid）
- `RISK_PLAN`：止损触发条件 + 最坏 PnL 估算
- `PERSONAL_NOTE`：bullet 给用户的话

### CIO sanity 自检（输出前过一遍）

| 规则 | 为什么 |
|------|--------|
| `confidence ≥ 0.95` → 降到 0.85 | 防过度自信。LLM 在模糊信号上爱 over-commit |
| `alloc_cny > 100_000` → clamp 到 100_000 | 单笔交易上限。逼用户在更大动作上慎重 |
| REGIME = `crash` → 强制 `HOLD` 或 `TRIM` | REGIME 优先于信号。crash = 不确定性太高，不能加风险 |
| Worker 严重分歧 → `confidence: 0.4-0.5` | 别假装共识。诚实低 confidence > 假装高 confidence |

## Stage 6：落盘 transcript

```bash
cat <<EOF | ~/.claude/skills/invest/scripts/run.sh save_committee <SYMBOL>
=== MACRO ===
<macro worker 输出>

=== QUANT_R1 ===
<quant R1 输出>

=== RISK_R1 ===
<risk R1 输出>

=== QUANT_R2 ===
<quant R2 输出>

=== RISK_R2 ===
<risk R2 输出>

=== CIO ===
<你写的 CIO memo>
EOF
```

落到 `memory/.committee/<date>/<asset>.md`，schema 和 DeepSeek cron 路径完全一样，
只是带了 `Provider: claude (skill mode)` 标记，让 Dreaming 之后能区分两条路径
的 transcript。

## 出 verdict 之后

如果用户同意：
1. **不要自己写 `memory/`**（见 SKILL.md Constraints）。
2. 告诉用户 NapCat 命令（如 `/gold_buy 5g @1040`）——执行环节走他个人 QQ bot
   留 audit trail。
3. 非黄金/非现金的交易（任何其他 yfinance symbol），让用户走 Web GUI 的
   HoldingDialog 或 `POST/PUT /api/holdings/{symbol}`。NapCat 专用命令只覆盖
   黄金 + 现金。
