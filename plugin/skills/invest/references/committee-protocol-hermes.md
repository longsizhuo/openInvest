# 委员会协议 — Hermes / 其他支持子任务委派的 agent

用户**在聊天里当场**说了 **"该不该买/卖 X"** / **"分析一下 X"** / **"跑委员会 X"**，
且你（调用方 agent）没有配置 `LLM_API_KEY`——严格按 6 个 stage 跑，全程零 API
成本（用你自己订阅的模型扮演 4 个角色）。

## ⚠️ 先确认你能走这条路

**只用于交互场景（用户在场，能实时看到你在干什么）**。如果你是被 cron /
定时任务无人值守触发的——**不要用这份协议**，改用 Direct 路径的
`daily_report` / `run_committee`（配 `LLM_API_KEY`，可以是免费额度供应商，
不一定要付费）。2026-07-14 实测过一次无人值守跑这份协议：没有老实调用
`delegate_task`，自己选了别的路子，还撞上"cron 无人值守不能批准危险命令"
的安全拦截，卡住不动——协议依赖你临场决定"调哪个工具、prompt 怎么拼"，
没人能在你走偏时纠正你，无人值守场景下这是真实会发生的失败模式，不是假设。

本文档是 **Coordinator 路径的 Hermes 变体**——你用 `delegate_task` 工具
spawn 隔离子任务扮演各角色，跟 [committee-protocol.md](committee-protocol.md)
（Claude Code 用 `Agent({...})`）逻辑完全一样，只是 spawn 语法不同。

**你需要**：`delegate_task` 工具（或等价的隔离子任务委派能力）+ 终端/shell
执行工具（跑 `run.sh` 命令）。**没有这些**（比如你只能单轮对话，不能 spawn
隔离子任务）→ 改用 **Direct 路径**：

```bash
run.sh run_committee <SYMBOL>
```

一条命令拿到 verdict + CIO memo + transcript，但需要 `DEEPSEEK_API_KEY`。

---

> **背景**：Coordinator 路径里 Macro 不跨资产共享，每次进 Round 1 一起
> spawn → R1 共 3 个 worker。Direct/Cron 路径里 Macro 跨资产共享，R1 只有
> Quant + Risk 2 个 worker——两份 LLM 调用数对照不要混着引用。

## Stage 0：同日检查（避免重复跑）

用终端工具跑：

```bash
ls "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md" 2>/dev/null
```

文件存在 → **直接读它，不要重新跑**，告诉用户："今天已经跑过 <SYMBOL> 了，
verdict 是 X (confidence Y)。要重跑吗？"——一次完整委员会要跑好几分钟 +
若干次 LLM 调用，同一答案别烧两遍。

## Stage 1：拿 brief

```bash
run.sh prepare_committee <SYMBOL>
```

返回 JSON 含所有需要的字段（完整表见 committee-protocol.md Stage 1——两条
路径这步完全一样）。**⚠ `regime_brief` 必须原样塞进 Quant Round 1 + Round 2
的 prompt**，漏了 Quant 会在 range_bound 底部乱喊 bearish（老 bug 路径）。

## Stage 2：Round 1 —— 一次 `delegate_task` 批量 spawn 3 个角色

**用 `tasks` 数组一次性 spawn，不要拆成 3 次独立调用**——`delegate_task`
的批量模式本来就是同步等全部完成才返回，独立调用反而失去并行：

```
delegate_task(tasks=[
  {
    "goal": "<原样粘 prompts.macro_strategist>\n\n# 当前宏观数据:\n<原样粘 macro_data>\n\n你的整段最终回复必须且只能是委员会要求的结构化格式（SIGNAL/STRENGTH/等字段，见上面 prompt 里的格式说明）。不要输出'我做了什么/用了什么工具'这类总结——那不是这个任务要的东西，直接原样交出结构化结果。"
  },
  {
    "goal": "<原样粘 prompts.quant_round1>\n\n# 市场 Regime (确定性算出，必须遵循):\n<原样粘 regime_brief>\n\n# 市场数据:\n<原样粘 market_data>\n\n（同上：整段回复只能是结构化格式，不要总结你做了什么。）"
  },
  {
    "goal": "<原样粘 prompts.risk_round1>\n\n# 用户持仓:\n<原样粘 portfolio_summary>\n\n# 长期模式:\n<原样粘 prior_insights>\n\n（同上：整段回复只能是结构化格式，不要总结你做了什么。）"
  }
])
```

**为什么要加"不要总结你做了什么"这句**：`delegate_task` 的子任务默认系统
提示会要求子 agent 在结尾写"我做了什么/找到了什么/改了什么文件"式总结（面向
写代码场景设计的）。我们要的是子 agent 把**整个回复**变成委员会格式本身，
不是格式外面裹一层"总结"——所以每个 `goal` 末尾都要显式覆盖掉这条默认指令。

`context` 字段（如果你的 `delegate_task` 版本把它和 `goal` 分开传）可以把
数据块放进去，效果一样——保持信息隔离：Macro 看不到持仓，Risk 看不到市场
技术指标，跟 Claude Code 版本的隔离要求一致。

批量默认同步等待（`background` 不传或传 `false`），返回时 `results` 数组
里每个元素对应一个 task 的最终回复。**等这次 `delegate_task` 调用完全返回
再进 Stage 3**——3 个角色是同一次调用里跑的，不需要额外等待逻辑。

## Stage 3：Round 2 —— Cross-challenge（一次 `delegate_task`，2 个角色）

Quant 和 Risk 现在能看到对方的 R1 输出，调整自己。Macro 不需要 Round 2
（它跨资产共享）：

```
delegate_task(tasks=[
  {
    "goal": "<原样粘 prompts.quant_round2_after_risk>\n\n# 市场 Regime (Round 1 给你的事实，Round 2 仍然有效):\n<原样粘 regime_brief>\n\n# Round 1 你自己的输出:\n<quant R1 结果>\n\n# Risk Officer 的报告:\n<risk R1 结果>\n\n（整段回复只能是结构化格式，不要总结你做了什么。）"
  },
  {
    "goal": "<原样粘 prompts.risk_round2_after_quant>\n\n# Round 1 你自己的输出:\n<risk R1 结果>\n\n# Quant 的技术信号:\n<quant R1 结果>\n\n（整段回复只能是结构化格式，不要总结你做了什么。）"
  }
])
```

## Stage 4（可选）：未收敛时跑 Round 3+

同 committee-protocol.md——只在 Quant/Risk 的 SIGNAL 在 R1→R2 之间翻面
**且**翻面后仍严重分歧时才跑 Round 3（再来一次两角色 `delegate_task`）。
收敛规则：两边 SIGNAL 都跟上一轮一样、且 `|STRENGTH delta| ≤ 1.0` → 收敛，
进 Stage 5。

## Stage 5：CIO 综合 —— **你自己写**，不 delegate

CIO 角色是**你**（发起 `delegate_task` 的那个 agent），不要再 spawn 一个
子任务代劳——你已经看到全部 worker 的输出，直接综合。

读完 Macro + Quant R1/R2 + Risk R1/R2 + `portfolio_summary`，按
`prompts.cio` 格式写完整 CIO memo，含必填字段（`VERDICT`/`CONFIDENCE`/
`DOMINANT_VIEW`/`SUGGESTED_ALLOC_CNY`/`EXECUTION_PLAN`/`RISK_PLAN`/
`PERSONAL_NOTE`）+ sanity 自检（完整规则见 committee-protocol.md Stage 5——
两条路径这步完全一样，confidence≥0.95 降到 0.85、alloc>10万 clamp、
crash regime 强制 HOLD/TRIM、worker 严重分歧就诚实给低 confidence）。

## Stage 6：落盘 transcript

```bash
cat <<EOF | run.sh save_committee <SYMBOL> --provider hermes
=== MACRO ===
<macro 角色输出>

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

**`--provider hermes` 必须带**——落盘的 transcript 会准确标注是 Hermes
跑的（不是硬编码成 claude），Dreaming 按 provider 分桶挖掘模式、事后排障
都靠这个字段。

落到 `memory/.committee/<date>/<asset>.md`，schema 和其他两条路径完全一样，
`explain_decision` / `decisions` 等下游工具零改动就能读。

## 出 verdict 之后

同 committee-protocol.md：不要自己写 `memory/`；用户确认成交后用 `buy` /
`sell` / `deposit` / `withdraw` MCP 工具记账；用户对建议表态后用
`record_execution <decision_id>` 回写决策账本（拒绝时先问一句原因）。
