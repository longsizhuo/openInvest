---
type: adr
title: "ADR-002: 暂不升级 Web/Cron 到 Claude Agent SDK"
tags: [architecture, agent, cost, deployment]
intent: 决策参数
documents:
  endpoints: []
  config_keys: []
  symbols: []
status: accepted
date: 2026-05-06
supersedes: []
superseded_by: []
---

# ADR-002: 暂不升级 Web/Cron 到 Claude Agent SDK

- **状态**: ✅ 已采纳
- **日期**: 2026-05-06
- **决策者**: longsizhuo + Claude

[← ADR 索引](../README.md#架构决策记录-adr) · [双路径背景](../04-execution-paths.md)

---

## Context

[ADR-001](001-dual-execution-paths.md) 决定保留双路径。但 Web/Cron 路径目前是 DeepSeek + ThreadPool 同进程多线程，**不是真 subagent 隔离**。

调研过 [`anthropics/claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python)，理论上能让 Web/Cron 也走 Claude 真 subagent：

- 支持 `AgentDefinition` + `allowed_tools=["Task"]` + `fork_session()` 真 subagent
- API 设计上和 Claude Code 内的 `Agent({subagent_type})` 一致
- 通过 Context7 文档查证可工作

理论上**好处**：
- Web/Cron 也是真 subagent（subprocess 隔离），和 Skill 路径架构一致
- Claude 4 模型能力 > DeepSeek-Chat
- marketing 上 "100% real Agent Teams"

那为什么不上？

---

## Decision

**暂不升级**。Web/Cron 继续用 DeepSeek + ThreadPool。

---

## 关键拒绝原因

### 1. 必须用 Claude 模型，不能 DeepSeek

`claude-agent-sdk-python` 是 Claude Code CLI 的 subprocess wrapper：
```
Python SDK → subprocess.run("claude --print ...") → Claude API
```

Subagent 模型固定走 Claude 订阅 / API。**不能让 subagent 跑 DeepSeek**。

### 2. 成本爆炸 30-100×

实测对比：

| 模型 | 单次 LLM | 单次委员会（5-15 LLM）| 每日 cron 月成本 |
|------|---------|----------------------|-----------------|
| DeepSeek-Chat | ¥0.001 | ¥0.01-0.03 | ¥0.30-1.00 |
| Claude Sonnet 4 | ~¥0.05 | ¥0.5-2 | ¥15-60 |
| Claude Opus 4 | ~¥0.20 | ¥2-6 | ¥60-180 |

cron 每日跑 = 月百块级别。对单人开源工具不可接受。

### 3. 需要服务器装 Claude CLI

`claude-agent-sdk-python` 不是纯 Python 库，是 CLI wrapper：
```bash
# 服务器上必须先
curl -fsSL https://claude.ai/install.sh | bash
# 然后 SDK 才能 spawn subprocess
```

Docker 镜像 + systemd 部署增加复杂度。

### 4. 两条路径已经各司其职

[ADR-001](001-dual-execution-paths.md) 解释了：
- Skill 路径已经是真 subagent + Claude 模型（用户 Claude Code）
- Web/Cron 是低成本 + 自动化

**用户主动追问 → Skill；自动化 / Web GUI 触发 → Web/Cron**。
合并到全 Claude 后这个分工消失。

### 5. 真 subagent 在 Web 路径价值有限

Web/Cron 当前实现的"信息分隔"是**靠规约**而不是 subprocess：
- 每个 agent 拿独立 prompt 字符串
- 每个 agent 在独立 ThreadPool worker（不共享变量）
- 测试覆盖了"信息泄漏"边界

→ 同 GIL 同进程，确实不是物理隔离。但**功能等价**。
→ 升级到真 subagent 对**用户感知**没区别（CIO 输出仍是同样格式）。

---

## Consequences

### 正面（保持现状的）

1. cron 月成本可控（¥1 vs ¥60）
2. 服务器部署简单（不用 Claude CLI）
3. Web 响应快（DeepSeek 2-3s/次 vs Claude 5-10s/次）
4. ADR-001 双路径分工清晰

### 负面

1. Web/Cron 不是"100% real Agent Teams"——marketing 上略弱
2. DeepSeek 模型能力上限低于 Claude，复杂 verdict 质量略差
3. 如果 Skill 路径和 Web/Cron 路径出 conflicting verdict，没有单一权威
4. 未来 Claude API 价格降下来不会自动启用——需要新开 ADR 复评

---

## Alternatives Considered

### A. OpenAI Agents SDK + DeepSeek

`openai-agents-python` 支持 `AsyncOpenAI(base_url="https://api.deepseek.com")` + `OpenAIChatCompletionsModel`。
让 SDKAgent 改用 OpenAI Agents SDK 协议，享受标准化 + 免费 OpenAI Traces dashboard。

**拒绝**：
- 自家 telemetry 已够用（`memory/llm_usage.jsonl` + `/api/llm/usage`）
- 协议标准化只是 marketing 噱头
- 工时 ~3-4h 不如花在功能开发

### B. 自己写 multiprocessing wrapper + DeepSeek

让 Web 路径也用 `multiprocessing.Pool` 真 subprocess + DeepSeek，绕开 Claude 限制。

**拒绝**：
- 加复杂度但收益边际（信息隔离已经靠规约保证）
- subprocess startup overhead（启动一个 Python 进程 ~200ms）会拖慢整体

### C. 等 Claude Agent SDK 支持非 Claude 模型

Anthropic 未来如果开放 SDK 跑别的模型 → 直接采纳。

**拒绝（暂）**：未来不可控，等不如先用 DeepSeek。

---

## Reference

- 双路径完整设计：[04-execution-paths.md](../04-execution-paths.md)
- ADR-001：双路径必要性：[001](001-dual-execution-paths.md)
- Context7 调研记录（memory）：`project_invest_committee_paths.md`

---

## 复盘触发条件

1. Claude API 价格降到 DeepSeek 的 3× 以内
2. 用户付费意愿出现（开源项目能赞助 ¥100/月 来跑 Claude cron）
3. Claude Agent SDK 支持非 Claude 模型
4. 有具体 use case 证明"真 subagent 隔离"对 Web 路径有质量增益（不只是架构纯粹）
