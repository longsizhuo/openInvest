---
type: adr
title: "ADR-001: 委员会保留 Skill + Web/Cron 双执行路径"
tags: [architecture, committee, execution-paths, skill, deepseek]
intent: 双执行路径架构决策
documents:
  endpoints: []
  config_keys: []
  symbols: [run_committee]
status: accepted
date: 2026-05-06
supersedes: []
superseded_by: []
---

# ADR-001: 委员会保留 Skill + Web/Cron 双执行路径

- **状态**: ✅ 已采纳
- **日期**: 2026-05-06
- **决策者**: longsizhuo + Claude

[← 回 ADR 索引](../README.md#架构决策记录-adr) · [完整双路径文档](../04-execution-paths.md)

---

## Context

openInvest 的"4 角色委员会"（Macro / Quant / Risk / CIO）有两条执行路径：

| 路径 | 协调者 | Worker 实现 | 模型 |
|------|--------|-------------|------|
| **Skill** | 用户的 Claude 当 coordinator | `Agent({subagent_type})` 真 spawn 4 个 subprocess subagent | Claude 4 |
| **Web/Cron** | `core/committee.py`（现 `core/committee/` 包）| 4 个 `SDKAgent` + `ThreadPoolExecutor` 同进程多线程 | DeepSeek-Chat |

两套同 prompt 不同实现，**结果可能不同**（不同模型 + 不同隔离粒度）。

考虑过的合并方向：
- A. 全切 Skill：项目零 API 成本，但 cron 不可能跑（用户得在 Claude Code 里）
- B. 全切 Web/Cron：单路径整洁，但失去"零成本 + Claude 模型能力上限"
- C. 全切 Claude Agent SDK：让 Web 也走真 subagent + Claude 模型——见 [ADR-002](002-no-claude-agent-sdk.md)

---

## Decision

**保留双路径并存**。两条路径各自服务不同场景，不合并。

---

## Consequences

### 正面

1. **零成本主路径**：Skill 路径让用户的 Claude 跑，项目本身不付一分 API 费
2. **可控成本自动化**：Web/Cron 走 DeepSeek（¥0.01-0.03/次），cron 月成本可控
3. **真并行 Web GUI**：DeepSeek 响应快（2-3s/次），多资产 ThreadPool 实测 ~16s 收敛
4. **验证机制**：同问题两套 verdict 不收敛时，是值得人工复核的信号
5. **容灾**：DeepSeek 跑路 / 涨价 → Skill 路径不受影响；反之亦然

### 负面

1. **维护成本 1.3×**：加新角色（如 ESG）要两边都注册，prompt 改动同步
2. **代码共享率约 70%**：prompt + REGIME + sanity check 共享，编排器 + LLM client 各写一份
3. **模型差异讲不清**：marketing 时讲"我们有 Skill 路径"会被 Claude 党挑刺"那 Web 路径不也只是 DeepSeek 吗"

---

## Alternatives Considered

### A. 单 Web/Cron + 切 Claude

让所有路径走 Claude API（OpenAI Agents SDK 或 Claude Agent SDK）。

**拒绝**：cron 每日 ~¥1，月 ~¥30。不可接受。详见 [ADR-002](002-no-claude-agent-sdk.md)。

### B. 单 Skill 删 Web/Cron

只保留 Claude Code Skill 路径。

**拒绝**：
- 失去 Web GUI 的「触发委员会」按钮——用户必须打开 Claude Code
- 失去 cron 03:00 自动 daily_report
- 失去 SSE 直播

### C. 双路径但合并实现

让 Web/Cron 也包装成 Skill 调用。

**拒绝**：
- 需要服务器装 `claude` CLI binary（Claude Agent SDK 是 subprocess wrapper）
- cron 调起 Claude CLI 跑一次 = 一次完整 Claude 订阅消耗
- 没有比当前架构更省

---

## Reference

- 完整双路径解释：[04-execution-paths.md](../04-execution-paths.md)
- 不升级 Claude Agent SDK 的细节：[ADR-002](002-no-claude-agent-sdk.md)
- Skill 实现：`skills/invest/scripts/run.sh prepare_committee` + `skills/invest/SKILL.md`
- Web/Cron 实现：`core/committee.py:run_committee`（现 `core/committee/debate.py:run_committee`）+ `connectors/web_api.py:committee_run`

---

## 复盘触发条件

如果未来出现下面任一情况，本 ADR 应被复评：

1. DeepSeek 涨价 ≥ 5×（成本结构变了）
2. Claude Agent SDK 支持非 Claude 模型（比如能跑 DeepSeek as subagent）
3. 用户量起来后 cron 跑频率需求增加（每小时 vs 每天）
4. OpenAI Agents SDK 流行度 / 协议标准化压力
