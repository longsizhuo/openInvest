# Dual Execution Paths (Coordinator vs Direct, renamed 2026-05)

The same committee logic has two implementations. Read this when the user asks
"run it automatically every day / 每天自动跑", "DeepSeek cost / DeepSeek 成本",
"why is my verdict different from yesterday's cron run / 为什么我跑出来和昨天 cron 跑的 verdict 不一样",
or "can non-Claude agents use this / 非 Claude 的 agent 能用吗".

> **2026-05 naming change**: formerly called the "Skill path" vs the "Web/Cron path".
> But the skill in fact supports **both** — `prepare_committee` + spawning subagents
> is Coordinator, `run_committee` is Direct (i.e. the same as the old cron path).
> Now uniformly named **Coordinator** vs **Direct**.

## Comparison

| Dimension | Coordinator path | Direct path |
|------|------------------|-------------|
| Who can use it | Agents with isolated-subtask delegation capability (Claude Code `Agent({...})`, Hermes `delegate_task`), **and the user MUST be present (interactive scenario)** — no unattended cron, see warning below | Any agent (Codex / Hermes / OpenClaw / Cursor / Cline / local DeepSeek / plain Python scripts) + **all unattended cron scenarios take this path, no matter which agent** |
| How it's triggered | In the skill: `prepare_committee` → spawn 4 subagents → `save_committee` | In the skill: one `run_committee SYM` command / `POST /api/committee/run` / cron `daily_report` |
| Coordinator | You, in that conversation (Claude / Hermes / ...) | The `core/committee/` Python package |
| Worker implementation | `Agent({subagent_type})` real subprocesses, or `delegate_task(tasks=[...])` isolated subtasks | 4 `SDKAgent` instances + ThreadPool |
| Worker model | Your own subscribed model | The configured `LLM_MODEL` (default DeepSeek-Chat; any OpenAI-compatible endpoint works) |
| Cost | Zero cost to the project; covered by the user's existing subscription | ~¥0.01-0.03 per run (DeepSeek pricing; can be ¥0 with a free-tier provider) |
| Information isolation | Real subprocesses/isolated subtasks (independent contexts) | Same process, prompt-string isolation |
| Latency | ~2-5 minutes (a bit slower per run) | ~15-60s (fast + parallel) |
| Persistence | `memory/.committee/<date>/<sym>.md` with `Provider: <your brand> (skill mode)` | Same file, with `Provider: deepseek` |
| SSE live stream | ❌ None | ✅ `/api/committee/live/{task_id}` (Web-API-triggered version only; the API is deprecated, used for remote hub mode) |
| Credential requirements | No key needed | `LLM_API_KEY` MUST be configured (any OpenAI-compatible endpoint; free-tier providers work) |

**⚠️ Coordinator is for interactive scenarios only**: it relies on you deciding on the
fly which tool to call and how to assemble the prompts. On 2026-07-14 we actually tested
letting Hermes run this protocol unattended: it did not faithfully call `delegate_task`,
went off track, then hit a safety block and stalled. In unattended cron no one can
correct you when you go off track — always switch to Direct; the little key-configuration
cost you would save does not buy reliability in unattended scenarios.

## Why both are kept

**The Coordinator path** puts you yourself (the model the user already subscribes to)
to work — the project itself pays nothing. Suited to "I'm present right now, asking a
question" scenarios. Limitations: only agents that support isolated-subtask delegation
can take it (Claude Code `Agent({...})`, Hermes `delegate_task`), and it is
**interactive-only** — not applicable unattended (cron), see the warning above.

**The Direct path** lets any agent use the same skill. One `run.sh run_committee SYM`
command gets the verdict — Cursor users, Cline users, Codex CLI users, script writers,
and unattended cron all take this path. The Web-API-triggered version
(`POST /api/committee/run`, deprecated, serves remote hub mode only) is the same Direct
implementation, plus SSE progress streaming.

**Same prompts, same REGIME constraints, same convergence detection.** The two sides'
verdicts **may differ** — because the models differ (Claude vs DeepSeek). That
difference is a **feature**, not a bug: cross-model validation. If both sides say TRIM,
that is stronger evidence than one side alone saying TRIM.

## When the user asks "which one should I use / 我应该用哪条"

| User scenario | Recommendation |
|----------|------|
| "I want it to run automatically — ping me when something changes / 我想自动跑——有变化叫我" | **Direct** (unattended cron, no matter which agent) + `daily_report` + email/IM |
| "I'm present, asking right now / 我在场，现在就想问" + has delegation capability (Claude Code / Hermes) | Coordinator (burns no API tokens) |
| "I'm present, using an agent without delegation capability such as Codex / Cursor / Cline / 我在场，用 Codex / Cursor / Cline 等没委派能力的 agent" | Direct (`run.sh run_committee SYM`) |
| "I want a live dashboard / 我要 live dashboard" | Web GUI retired (2026-07) — take Direct; watch progress via the CLI JSON output |
| "Cron said TRIM, you say HOLD — who's right? / Cron 说 TRIM 你说 HOLD，谁对？" | Both are signals. Divergence → genuine ambiguity. Lower the confidence |
| "I want zero cost / 我想零成本" | Use Coordinator for interactive scenarios; unattended cron MUST be Direct, but switching to a free-tier `LLM_API_KEY` provider (Qwen / Zhipu / MiMo, etc.) can be zero cost too — paid is not the only option |

## Things you (the orchestrator) must **NOT** do

- **Do not call the cron path on your own initiative** — don't run
  `python -m jobs.daily_report`; that runs every asset in one shot and burns DeepSeek
  money. Run it only when the user explicitly says "run a deep analysis / 跑深度分析" /
  "run full report".
- **Do not call the Web `POST /api/committee/run` on your own initiative** — that is a
  deprecated endpoint (serves remote hub mode only); locally always use
  `run.sh run_committee`.
- **Do not say the Coordinator path is "better"**. The two serve different scenarios.
  A GPT-x user taking Direct is a perfectly normal choice.
- **Do not simulate spawning on the Coordinator path** — without an isolated-subtask
  delegation tool such as `Agent({...})`/`delegate_task`, take Direct; do not "play"
  the 4 roles yourself one after another.
- **Do not run Coordinator in unattended scenarios (cron)**, even if you have
  delegation capability — see the warning above; no one can correct you if you go
  off track.

## Architecture details

Read these in the project repo:
- [docs/wiki/04-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/04-execution-paths.md)
- [docs/wiki/adr/001-dual-execution-paths.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/adr/001-dual-execution-paths.md)

The ADR explains why both paths are kept rather than merged.
