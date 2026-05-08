# 双执行路径（Skill vs Web/Cron）

同一套委员会逻辑有两套实现。用户问"每天自动跑"、"GUI 触发委员会"、"DeepSeek 成本"、
"为什么我跑出来和昨天 cron 跑的 verdict 不一样"时读这个。

## 对比

| 维度 | Skill 路径（你正在用）| Web/Cron 路径 |
|------|---------------------|---------------|
| 触发方式 | 用户在 Claude Code 里问 | `POST /api/committee/run` 或 cron `daily_report` |
| Coordinator | 你（这个对话里的 Claude）| `core/committee.py` Python 模块 |
| Worker 实现 | `Agent({subagent_type})` 真 subprocess | 4 个 `SDKAgent` 实例 + ThreadPool |
| Worker 模型 | Claude（用户 Claude 订阅）| DeepSeek-Chat |
| 成本 | 项目零开销，用户 Claude budget 承担 | ~¥0.01-0.03 一次（DeepSeek token）|
| 信息隔离 | 真 subprocess（独立 context window）| 同进程，prompt 字符串隔离 |
| 延迟 | ~2-5 分钟（Claude 单次慢一点）| ~15-60s（DeepSeek 快 + 并行）|
| 持久化 | `memory/.committee/<date>/<sym>.md` 带 `Provider: claude (skill mode)` | 同文件，带 `Provider: deepseek` |
| SSE 直播 | ❌ 无 | ✅ `/api/committee/live/{task_id}` |

## 为什么两套都留

**Skill 路径**让用户的 Claude 干活——项目本身不付一分钱。适合"我现在在
Claude Code 里问问题"场景。

**Web/Cron 路径**无人值守跑。cron `daily_report` 每天 03:00 触发，写 verdict 到
`memory/daily/<date>/<sym>.md`，可选发邮件。GUI 用户点按钮也走这条，能在 SSE
流里看 progress。

**同 prompt，同 REGIME 约束，同收敛检测**。两边 verdict **可能不同**——因为
模型不同（Claude vs DeepSeek）。这种差异是**特性**，不是 bug：cross-model
validation。如果两边都说 TRIM，比单边说 TRIM 证据更强。

## 用户问"我应该用哪条"

| 用户场景 | 推荐 |
|----------|------|
| "我想自动跑——有变化叫我" | Web/Cron + 邮件 |
| "我现在就想问，给个建议" | Skill（你正在用，继续）|
| "我想要个 live dashboard" | Web GUI + Web/Cron 路径 |
| "Cron 那边说 TRIM 你说 HOLD，谁对？" | 两个都是信号。分歧 → 真模糊。降低用户 confidence |
| "我想在这件事上零成本" | Skill（用户 Claude 订阅，不烧 API token）|

## 你（orchestrator）**不要**做的事

- **不要主动调 cron 路径** —— 别跑 `python -m jobs.daily_report`，那花用户的 DeepSeek
  钱。只在用户明说 "跑深度分析" / "run full report" 时跑。
- **不要主动调 Web `POST /api/committee/run`** —— 除非用户在用 GUI 且明确点了按钮。
- **不要说 Skill 路径"更好"**。两条服务不同场景。

## 架构细节

读项目 repo 里的：
- [docs/wiki/04-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/04-execution-paths.md)
- [docs/wiki/adr/001-dual-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/adr/001-dual-execution-paths.md)

ADR 解释了为什么两条都留，不合并。
