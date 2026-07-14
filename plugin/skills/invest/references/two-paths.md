# 双执行路径（Coordinator vs Direct，2026-05 重命名）

同一套委员会逻辑有两套实现。用户问"每天自动跑"、"DeepSeek 成本"、
"为什么我跑出来和昨天 cron 跑的 verdict 不一样"、"非 Claude 的 agent 能用吗"
时读这个。

> **2026-05 命名调整**：以前叫 "Skill 路径" vs "Web/Cron 路径"。
> 但实际上 skill **同时**支持两条——`prepare_committee` + spawn subagent
> 走 Coordinator，`run_committee` 走 Direct（即旧 cron 路径同款）。
> 现在统一叫 **Coordinator** vs **Direct**。

## 对比

| 维度 | Coordinator 路径 | Direct 路径 |
|------|------------------|-------------|
| 谁能用 | 有隔离子任务委派能力的 agent（Claude Code `Agent({...})`、Hermes `delegate_task`），**且必须用户在场（交互场景）**——不能无人值守 cron，见下方警告 | 任何 agent（Codex / Hermes / OpenClaw / Cursor / Cline / DeepSeek 本地 / 普通 Python 脚本）+ **cron 无人值守场景一律走这条，不管什么 agent** |
| 触发方式 | skill 里 `prepare_committee` → spawn 4 subagent → `save_committee` | skill 里 `run_committee SYM` 一条命令 / `POST /api/committee/run` / cron `daily_report` |
| Coordinator | 那个对话里的你（Claude / Hermes / ...）| `core/committee/` Python 包 |
| Worker 实现 | `Agent({subagent_type})` 真 subprocess，或 `delegate_task(tasks=[...])` 隔离子任务 | 4 个 `SDKAgent` 实例 + ThreadPool |
| Worker 模型 | 你自己订阅的模型 | 配置好的 `LLM_MODEL`（默认 DeepSeek-Chat，任意 OpenAI 兼容端点均可）|
| 成本 | 项目零开销，用户已有订阅承担 | ~¥0.01-0.03 一次（DeepSeek 计价；换成有免费额度的供应商可以是 ¥0）|
| 信息隔离 | 真 subprocess/隔离子任务（独立 context）| 同进程，prompt 字符串隔离 |
| 延迟 | ~2-5 分钟（单次慢一点）| ~15-60s（快 + 并行）|
| 持久化 | `memory/.committee/<date>/<sym>.md` 带 `Provider: <你的品牌> (skill mode)` | 同文件，带 `Provider: deepseek` |
| SSE 直播 | ❌ 无 | ✅ `/api/committee/live/{task_id}`（仅 Web API 触发版本；API 已 deprecated，remote hub 用）|
| 凭据需求 | 不需要 key | 必须配置 `LLM_API_KEY`（任意 OpenAI 兼容端点，可用免费额度供应商）|

**⚠️ Coordinator 只用于交互场景**：它依赖你临场决定"调哪个工具、prompt 怎么
拼"，2026-07-14 实测过让 Hermes 无人值守跑这套协议，没有老实调用
`delegate_task`，走偏还撞上安全拦截卡住。cron 无人值守没人能纠正你走偏，
一律改走 Direct——省的那点 key 配置成本，换不来无人值守场景的可靠性。

## 为什么两套都留

**Coordinator 路径**让你自己（用户已有订阅的模型）干活——项目本身不付一分钱。
适合"我现在在场问问题"场景。但局限：只有支持隔离子任务委派的 agent 能走
（Claude Code `Agent({...})`、Hermes `delegate_task`），且**只能交互用**——
无人值守（cron）不适用，见上方警告。

**Direct 路径**让任何 agent 都能用同一套 skill。`run.sh run_committee SYM` 一
条命令拿 verdict —— Cursor 用户、Cline 用户、Codex CLI 用户、写脚本的用户、
cron 无人值守，都走这条。Web API 触发的版本（`POST /api/committee/run`，
deprecated，只服务 remote hub 模式）也是 Direct 同实现，多了 SSE 进度推送。

**同 prompt，同 REGIME 约束，同收敛检测**。两边 verdict **可能不同**——因为
模型不同（Claude vs DeepSeek）。这种差异是**特性**，不是 bug：cross-model
validation。如果两边都说 TRIM，比单边说 TRIM 证据更强。

## 用户问"我应该用哪条"

| 用户场景 | 推荐 |
|----------|------|
| "我想自动跑——有变化叫我" | **Direct**（cron 无人值守，不管什么 agent）+ `daily_report` + 邮件/IM |
| "我在场，现在就想问" + 有委派能力（Claude Code / Hermes）| Coordinator（不烧 API token）|
| "我在场，用 Codex / Cursor / Cline 等没委派能力的 agent" | Direct（`run.sh run_committee SYM`）|
| "我要 live dashboard" | Web GUI 已退役（2026-07）——走 Direct，进度看 CLI JSON 输出 |
| "Cron 说 TRIM 你说 HOLD，谁对？" | 两个都是信号。分歧 → 真模糊。降低 confidence |
| "我想零成本" | 交互场景用 Coordinator；cron 无人值守必须 Direct，但换个有免费额度的
  `LLM_API_KEY` 供应商（千问/智谱/MiMo 等）一样能零成本，不是只能付费 |

## 你（orchestrator）**不要**做的事

- **不要主动调 cron 路径** —— 别跑 `python -m jobs.daily_report`，那一次跑所有
  资产烧 DeepSeek 钱。只在用户明说 "跑深度分析" / "run full report" 时跑。
- **不要主动调 Web `POST /api/committee/run`** —— 那是 deprecated 端点（只服务
  remote hub 模式），本地一律走 `run.sh run_committee`。
- **不要说 Coordinator 路径"更好"**。两条服务不同场景。一个 GPT-x 用户走
  Direct 是完全正常的选择。
- **不要在 Coordinator 路径模拟 spawn**——没有 `Agent({...})`/`delegate_task`
  等隔离子任务委派工具就走 Direct，别自己依次"扮演" 4 个角色。
- **不要在无人值守场景（cron）跑 Coordinator**，即使你有委派能力——见上方
  警告，走偏没人能纠正。

## 架构细节

读项目 repo 里的：
- [docs/wiki/04-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/04-execution-paths.md)
- [docs/wiki/adr/001-dual-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/adr/001-dual-execution-paths.md)

ADR 解释了为什么两条都留，不合并。
