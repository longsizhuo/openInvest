---
type: wiki-chapter
title: 双执行路径
tags: [committee, execution-paths, coordinator, direct, architecture]
intent: 双路径架构说明
documents:
  endpoints:
    - POST /api/committee/run
    - GET /api/committee/{task_id}
    - GET /api/committee/live/{task_id}
  config_keys: []
  symbols:
    - run_committee
    - run_committee_session
    - SDKAgent
---

# 双执行路径

> 同一套委员会逻辑，**两个独立实现**：Coordinator 路径让用户的 Claude 当协调者真 spawn 4 个 subagent，
> Direct 路径用 DeepSeek + ThreadPool 同进程多线程。结果可能不同——这是 feature。

[← 03-dreaming](03-dreaming.md) · [Wiki 索引](README.md) · [05-data-model →](05-data-model.md)

> **2026-05 重命名**：旧文档叫 "Skill 路径" vs "Web/Cron 路径"。但 skill 现在
> 同时支持两条（`prepare_committee` + spawn subagent 走 Coordinator；
> `run_committee` 走 Direct，任意非 Claude agent 也能用），所以统一改叫
> **Coordinator** vs **Direct**。Direct 包含原 cron 路径 + Web GUI 触发版本 +
> skill 里的 `run_committee` 子命令。

---

## TL;DR

| 路径 | 谁能用 | 触发 | 协调者 | Worker 实现 | 模型 | 真 subagent? | 成本 |
|------|--------|------|--------|-------------|------|-------------|------|
| **Coordinator** | 仅 Claude Code（要 `Agent({...})` 工具）| skill `prepare_committee SYM` | 用户的 Claude | `Agent({subagent_type})` 真 spawn 4 subagent（subprocess 隔离）| Claude 4 | ✅ | 由用户订阅承担（项目 ¥0）|
| **Direct** | 任意 agent（Cursor / Cline / Codex / 普通脚本）+ cron | skill `run_committee SYM` / `POST /api/committee/run` / cron `daily_report` | `core/committee/` | 4 个 `SDKAgent` + `ThreadPoolExecutor` 同进程多线程 | DeepSeek-Chat | ❌（信息分隔但非 subprocess）| ¥0.01-0.03 一次 |

**功能等价**：同一套 prompt，同一套 cross-challenge 协议，同一套 regime 分类 + 中性概率口径。
**模型不同**：verdict 可能不同——这是**对比验证机制**而不是 bug。

---

## 1. Coordinator 路径（仅 Claude Code）

### 触发

```bash
~/.claude/skills/invest/scripts/run.sh prepare_committee NDQ.AX
```

或在 Claude Code 对话里说："帮我跑委员会分析 NDQ"——Claude 会自动调用 skill。

### 实现

`skills/invest/scripts/run.sh prepare_committee` 做的事：

1. 拉 NDQ.AX 行情 + REGIME 算好
2. 把 4 个角色的 system prompt + 输入数据写到 `/tmp/.committee/<task_id>/{macro,quant,risk,cio}.md`
3. 输出一段 instruction 给用户的 Claude，**告诉它去 spawn 4 个 subagent 跑**

用户的 Claude 看到 instruction 后：

```
Agent({
  subagent_type: "general-purpose",
  description: "Macro analysis for NDQ.AX",
  prompt: <macro 角色的 system prompt + 输入>
})
Agent({...})  // quant
Agent({...})  // risk

// 等三个 subagent 完成后
Agent({...})  // cio 综合 transcript
```

**真隔离**：每个 Agent() 都是 subprocess，自带独立 context window。Claude 4 当 worker，能力上限高。

### 优势

- ✅ 项目本身**零 API 成本**（用户已订阅 Claude，跑 100 次也 0 元）
- ✅ Claude 模型能力强，verdict 质量通常高
- ✅ 真 subagent 隔离 = 真 Agent Teams（subprocess，不是同进程绑 token）

### 限制

- ❌ 必须在 Claude Code 里跑，不能后台 cron
- ❌ 用户得在场触发
- ❌ 没有 SSE 直播，结果是一次性输出
- ❌ Skill 跑完后状态不持久化到 invest memory（除非用户手动 fwd）

详见 [skills/invest/SKILL.md](../../skills/invest/SKILL.md)。

---

## 2. Direct 路径（任意 agent / cron / Web GUI）

三个触发入口都走同一份 `core/committee/debate.py:run_committee`：

| 入口 | 谁用 | 备注 |
|------|------|------|
| `~/.claude/skills/invest/scripts/run.sh run_committee SYM` | Cursor / Cline / Codex / 普通脚本 / DeepSeek 本地 / 任意 agent | 一条命令拿 verdict JSON + CIO memo |
| `POST /api/committee/run` | Web GUI 的"触发/直播"按钮 | 异步 + SSE 进度推送 |
| cron `0 3 * * *` 跑 `jobs/daily_report.py` | 服务器自动每日 | 跑全部 target_assets，可选发邮件 |

### 触发

**手动**：`POST /api/committee/run` （Web GUI 「触发/直播」按钮）
**自动**：cron `0 3 * * *` 跑 `jobs/daily_report.py`

### 实现

`core/committee/debate.py:run_committee` 在同一进程内：

```python
# Round 1: Quant + Risk 并行
with ThreadPoolExecutor(max_workers=2) as ex:
    quant_future = ex.submit(_ask, quant_agent, quant_prompt)
    risk_future = ex.submit(_ask, risk_agent, risk_prompt)
    quant_r1 = quant_future.result()
    risk_r1 = risk_future.result()

# Round 2..N: cross-challenge with 收敛检测
for round_idx in range(2, max_debate_rounds + 1):
    quant_rN, risk_rN = _parallel_ask([
        (quant_agent, quant_prompt_rN),
        (risk_agent, risk_prompt_rN),
    ])
    if _check_convergence(...):
        break

# CIO 串行
cio_memo = _ask(cio_agent, cio_prompt_with_full_transcript)
```

每个 `_ask` 调一次 DeepSeek-Chat HTTP API。同一进程多线程并发。

**信息隔离靠规约**：每个 agent 拿到自己的 prompt 字符串，不共享变量。但物理上是同进程同 GIL，不是 subprocess。

### 优势

- ✅ 后台 cron 自动跑（每天 03:00 daily_report）
- ✅ Web GUI SSE 实时直播 stage 进度
- ✅ 多资产真并行（ThreadPoolExecutor 包多个 `run_committee_for_symbol`）
- ✅ 状态持久化（`.committee/<task_id>/status.json` + `daily/<date>/<sym>.md`）
- ✅ DeepSeek-Chat 响应快（2-3s/次 vs Claude 5-10s/次），整体收敛 ~16s

### 限制

- ❌ DeepSeek 模型能力 < Claude 4，复杂场景 verdict 质量略低
- ❌ 用户付 DeepSeek 费用（虽然便宜，¥0.01-0.03 一次）
- ❌ 同进程不是真 subagent（虽然信息隔离正确，但 marketing 上 Claude 党会挑刺）

---

## 3. 为什么不合并

**两条路径并存就是 feature，不是债务**。原因：

### 3.1 成本结构不同

| 场景 | Skill 适合 | Web/Cron 适合 |
|------|-----------|---------------|
| 用户主动追问"该不该加仓" | ✅（Claude 自家用，0 成本）| ❌（每次都 ¥0.03）|
| cron 每日 03:00 自动跑 | ❌（用户没在 Claude Code 里）| ✅（DeepSeek 月成本可控）|
| 多资产并行扫一遍 | ❌（用户的 Claude 不擅长长批处理）| ✅（ThreadPool 16s 完成）|

### 3.2 验证机制

**同问题两套 verdict**——验证模型偏差。
比如 Skill 出 BUY、Web 出 HOLD，说明这个判断在不同模型间不收敛，**值得人工复核**。
合并成单路径 → 失去这个对比信号。

### 3.3 容灾

DeepSeek 限速 / 涨价 / 跑路？Coordinator 路径不受影响，用户照常用。
Claude API 抖？Direct 路径不受影响，自动化照跑。

---

## 4. 实现层次（哪些代码两条路径共享）

```
┌────────────────────────────────────────────────────────────────┐
│  agents/{macro,quant,risk,cio}.py 的 prompt                    │ ← 共享
├────────────────────────────────────────────────────────────────┤
│  core/regime.py REGIME 分类 + 概率口径                         │ ← 共享
├────────────────────────────────────────────────────────────────┤
│  core/committee/debate.py run_committee 编排逻辑               │ ← 仅 Direct 用
│  skills/invest/scripts/run.sh prepare_committee 提示生成       │ ← 仅 Coordinator 用
│  skills/invest/scripts/run.sh run_committee（包 run_committee）│ ← Direct 在 skill 里的入口
├────────────────────────────────────────────────────────────────┤
│  agents/sdk_agent.py SDKAgent (DeepSeek HTTP)                  │ ← 仅 Direct 用
│  Claude Agent({...}) tool                                      │ ← 仅 Coordinator 用
└────────────────────────────────────────────────────────────────┘
```

**共享率约 70%**（prompt + REGIME + sanity check + 数据准备）。
**分歧 30%**（编排器 + LLM client）。

加一个新角色（如 ESG 分析师）需要两边都注册——这是一致性的代价。

---

## 5. 升级 Claude Agent SDK？

调研过（2026-05）：可以用 [`anthropics/claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python) 让 Direct 路径也走 Claude 真 subagent。

**问题**：
1. 强绑 Claude 模型（不能 DeepSeek），cron 每日跑成本 30-100×
2. SDK 是 Claude Code CLI 的 subprocess wrapper，需要服务器装 `claude` CLI binary
3. 单次 committee ¥0.5-2，每天 cron = 百块/月

**结论**：暂不升级。详见 [adr/002-no-claude-agent-sdk.md](adr/002-no-claude-agent-sdk.md)。

---

## 6. 决策树：你该用哪条

```
你在 Claude Code 里 + 想问"该不该买"？
  → Coordinator 路径（让你的 Claude spawn 4 subagent，不烧 DeepSeek token）

你在 Cursor / Cline / Codex / 其他非 Claude agent 里？
  → Direct 路径（skill `run_committee SYM` 一键拿 verdict，需要 DEEPSEEK_API_KEY）

你在 Web GUI 想点按钮触发 + 看实时进度？
  → Direct 路径（POST /api/committee/run，自带 SSE）

你想每天 03:00 自动跑（无人值守）？
  → Direct 路径 cron（jobs/daily_report.py）

你想对比同问题两个模型的 verdict 看一致性？
  → 两条都跑一遍
```

---

## 下一步

→ [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md) — 为什么这个决定不是 tech debt

→ [adr/002-no-claude-agent-sdk.md](adr/002-no-claude-agent-sdk.md) — 不升级 Claude Agent SDK 的具体原因

→ [02-agents.md](02-agents.md) — 4 个角色 prompt 在两条路径里都长什么样
