# 双执行路径（Coordinator vs Direct，2026-05 重命名）

同一套委员会逻辑有两套实现。用户问"每天自动跑"、"GUI 触发委员会"、"DeepSeek 成本"、
"为什么我跑出来和昨天 cron 跑的 verdict 不一样"、"非 Claude 的 agent 能用吗"
时读这个。

> **2026-05 命名调整**：以前叫 "Skill 路径" vs "Web/Cron 路径"。
> 但实际上 skill **同时**支持两条——`prepare_committee` + spawn subagent
> 走 Coordinator，`run_committee` 走 Direct（即旧 cron 路径同款）。
> 现在统一叫 **Coordinator** vs **Direct**。

## 对比

| 维度 | Coordinator 路径 | Direct 路径 |
|------|------------------|-------------|
| 谁能用 | **仅 Claude Code**（要 `Agent({...})` 工具）| 任何 agent（Cursor / Cline / Codex / DeepSeek 本地 / 普通 Python 脚本）+ cron 自动跑 |
| 触发方式 | skill 里 `prepare_committee` → spawn 4 subagent → `save_committee` | skill 里 `run_committee SYM` 一条命令 / `POST /api/committee/run` / cron `daily_report` |
| Coordinator | 那个对话里的 Claude（你）| `core/committee.py` Python 模块 |
| Worker 实现 | `Agent({subagent_type})` 真 subprocess | 4 个 `SDKAgent` 实例 + ThreadPool |
| Worker 模型 | Claude（用户 Claude 订阅）| DeepSeek-Chat |
| 成本 | 项目零开销，用户 Claude budget 承担 | ~¥0.01-0.03 一次（DeepSeek token）|
| 信息隔离 | 真 subprocess（独立 context window）| 同进程，prompt 字符串隔离 |
| 延迟 | ~2-5 分钟（Claude 单次慢一点）| ~15-60s（DeepSeek 快 + 并行）|
| 持久化 | `memory/.committee/<date>/<sym>.md` 带 `Provider: claude (skill mode)` | 同文件，带 `Provider: deepseek` |
| SSE 直播 | ❌ 无 | ✅ `/api/committee/live/{task_id}`（仅 Web GUI 触发版本）|
| 凭据需求 | 不需要 DeepSeek key | 必须 `DEEPSEEK_API_KEY` |

## 为什么两套都留

**Coordinator 路径**让用户的 Claude 干活——项目本身不付一分钱。适合"我现在在
Claude Code 里问问题"场景。但局限：只有 Claude Code 能走（其他 agent 没有
spawn subagent 的工具）。

**Direct 路径**让任何 agent 都能用同一套 skill。`run.sh run_committee SYM` 一
条命令拿 verdict —— Cursor 用户、Cline 用户、Codex CLI 用户、写脚本的用户、
cron 无人值守，都走这条。GUI 触发的版本（`POST /api/committee/run`）也是
Direct 同实现，多了 SSE 进度推送。

**同 prompt，同 REGIME 约束，同收敛检测**。两边 verdict **可能不同**——因为
模型不同（Claude vs DeepSeek）。这种差异是**特性**，不是 bug：cross-model
validation。如果两边都说 TRIM，比单边说 TRIM 证据更强。

## 用户问"我应该用哪条"

| 用户场景 | 推荐 |
|----------|------|
| "我想自动跑——有变化叫我" | Direct + cron `daily_report` + 邮件 |
| "我在 Claude Code 里就想问" | Coordinator（不烧 DeepSeek）|
| "我用 Cursor / Cline / Codex / 不用 Claude" | Direct（`run.sh run_committee SYM`）|
| "我要 live dashboard" | Web GUI + Direct（GUI 路径自带 SSE）|
| "Cron 说 TRIM 你说 HOLD，谁对？" | 两个都是信号。分歧 → 真模糊。降低 confidence |
| "我想零成本" | Coordinator（用户 Claude 订阅，不烧 API token）|

## 你（orchestrator）**不要**做的事

- **不要主动调 cron 路径** —— 别跑 `python -m jobs.daily_report`，那一次跑所有
  资产烧 DeepSeek 钱。只在用户明说 "跑深度分析" / "run full report" 时跑。
- **不要主动调 Web `POST /api/committee/run`** —— 除非用户在用 GUI 且明确点了按钮。
- **不要说 Coordinator 路径"更好"**。两条服务不同场景。一个 GPT-x 用户走
  Direct 是完全正常的选择。
- **不要在 Coordinator 路径模拟 spawn**（如果你不是 Claude Code）—— 没有
  `Agent({...})` 工具就走 Direct，别自己依次"扮演" 4 个角色。

## 架构细节

读项目 repo 里的：
- [docs/wiki/04-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/04-execution-paths.md)
- [docs/wiki/adr/001-dual-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/adr/001-dual-execution-paths.md)

ADR 解释了为什么两条都留，不合并。
