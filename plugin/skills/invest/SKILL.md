---
name: invest
version: 0.20.1 # x-release-please-version
description: openInvest multi-asset AI investment committee — **daily use**. Read portfolio / live prices / strategy / decision history / adjust positions / run a 4-role LLM committee for an investment verdict. Supports any yfinance symbol (A-share / HK / US / ETF / crypto / commodities) and any currency. **Two paths** — (1) Coordinator, Claude Code spawns 4 subagents, saves DeepSeek tokens; (2) Direct, any agent (Codex / Hermes / OpenClaw / Cursor / Cline / plain script) runs `run.sh run_committee <SYM>` for a one-shot verdict. **Trigger scenarios** — "show portfolio / 看看我的持仓", "how is my P&L / 我现在涨了多少", "should I buy/sell X / 该不该买卖X", "analyze X / 分析一下X", "run committee on X / 跑委员会", "track AAPL / 跟踪苹果", "add/trim a position, log a trade / 加仓减仓记一笔". **First-time install uses a separate skill `invest-setup`** (switch to it when `doctor` returns `needs_setup`). Backend — longsizhuo/openInvest.
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, portfolio, committee, stocks, gold, etf, crypto, 投资, 持仓, 委员会, 行情]
---

# Invest Skill

**First-time fork users**: run the `invest-setup` skill first to initialize. The main flow lives in this skill (the AI agent uses the CLI/MCP to view the portfolio / run the committee / replay decision history).
The Web GUI has been retired (2026-07) — every capability is exposed via CLI subcommands / MCP tools.
The backend is distributed from PyPI (pulled on demand via uvx); to update, just run `run.sh update`.

openInvest multi-asset AI investment committee. **This skill is not Claude-exclusive** — any agent that can run shell commands can use it; see "Choosing a path" below.

Reply in the language the user is currently speaking unless they explicitly ask to switch languages.

## Choosing a path

**The first question is not "which brand of agent are you" — it is "is a human present for this invocation"**:

```
Is a user asking in chat right now ("should I buy X / 该不该买 X") where you can
react in real time (wrong tool picked, blocked by a safety gate — the user sees it
on the spot and corrects it on the spot)?
  → Check whether you have an isolated sub-task delegation capability (Agent({...}) / delegate_task)
    Yes → Coordinator (table below)
    No  → Direct

Is this a cron / scheduled run with nobody watching?
  → Always go Direct, no matter which agent you are or whether you can delegate
```

**Why unattended runs always go Direct, even with delegation capability**: the
Coordinator protocol depends on you improvising "which tool to call and how to
assemble the prompt". That is fine normally, but on 2026-07-14 a real Hermes cron
ran Coordinator unattended — it did not faithfully call the delegation tool per
the protocol, picked its own route instead, then hit the "unattended cron cannot
approve dangerous commands" safety gate and stalled. Direct is pure deterministic
Python code and never improvises. Saving the small hassle of configuring a key is
not worth a probabilistic hang / derailment in unattended scenarios — a bad trade.

| Scenario | Who you are | Path | What to run | Credentials |
|------|--------|----------|--------|------|
| Interactive (user present) | Claude Code (has the `Agent({...})` tool) | **Coordinator** | `prepare_committee` → spawn 4 subagents → `save_committee` | No key needed |
| Interactive (user present) | Hermes (has the `delegate_task` tool) | **Coordinator** ([Hermes variant](references/committee-protocol-hermes.md)) | Same as above; spawn syntax becomes `delegate_task(tasks=[...])` | No key needed |
| Interactive, no delegation capability | Codex / OpenClaw / Cursor / Cline / plain script | **Direct** | `run_committee <SYMBOL>` one-shot | Needs `LLM_API_KEY` |
| **cron / unattended**, any agent | Anyone, with or without delegation capability | **Direct** | `run_committee` / `daily_report` | Needs `LLM_API_KEY` |

All three paths share the **same foundation** — the same prompts, the same data
preparation (regime classification + probability framing + deterministic fact
block), and the same on-disk format
(`memory/.committee/<date>/<asset>.md`; both Coordinator variants must call `save_committee`
with `--provider <your-brand>` so the transcript is not mislabeled as claude). The only
difference is "who plays the 4 LLM roles": on Coordinator you play them yourself
(the model the user already subscribes to); on Direct the configured LLM (billed
per token) plays them. Verdicts may differ (different models — useful for
cross-validation).

**`LLM_API_KEY` is not DeepSeek-only, and the cost is near zero**: `utils/llm.py` works with any
OpenAI-compatible endpoint (three envs: `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`; `DEEPSEEK_*`
remains supported for compatibility). Want zero cost → providers with free tiers right now
(Qwen / Zhipu / MiMo etc.) all work (free-tier terms change — verify before use); don't want
the hassle → just use DeepSeek: at daily-report volume (a few assets/day) roughly
¥0.01-0.03 per run, under ¥2 a month. Do not bet the reliability of unattended
runs on Coordinator just to save that much.

**No sub-task delegation capability and no key configured**: do not force your way through
Coordinator, do not fabricate a verdict, and do not write code to brute-force around it —
just call `run_committee` (you will get a clear error) and honestly tell the user
"LLM_API_KEY needs to be configured".

## Decision tree (identical up front, whichever path you take)

```
1. Run `run.sh doctor`                               ← mandatory first step
   ├─ status: "ready"        → go to step 2 (keep using this skill)
   └─ status: "needs_setup"  → **switch to the `invest-setup` skill** (not handled here)

2. Pick the subcommand by user intent:

   "show portfolio / how much do I have / 看持仓"     → run.sh status
   "assess the situation / risk / concentration
    / 分析战况"                                       → run.sh status + **`curl /api/user`** for
                                                        wealth_context (**must-read**, avoids
                                                        misjudgment by legacy PWM logic)
   "what is my strategy / 我的策略是什么"             → run.sh strategy
   "recent trades / transaction log / 最近交易"       → run.sh history
   "where is the market / VIX now / 现在大盘"         → run.sh live_prices
   "if X drops 5% how much do I lose / 如果X跌5%"     → run.sh what_if --symbol X --pct -5
                                                        (X is a yfinance symbol in the user's
                                                        portfolio; --gold-pct / --ndq-pct kept
                                                        for legacy usage)
   "should I buy/sell X / analyze X / 该不该买卖X"    → committee protocol ↓
   "track AAPL / I want to watch TSLA / 跟踪苹果"     → see references/adding-assets.md

3. Run the committee per your path:
   - Coordinator → read references/committee-protocol.md (spawn 4 subagents)
   - Direct      → just `run.sh run_committee <SYMBOL>` for a JSON verdict

4. After getting the verdict / cio_memo:
   - **`cio_memo` is a Markdown string** (with `# title ## verdict` structure etc.).
     Render it **as Markdown for the user** — do not print raw JSON and make the
     user parse it themselves
   - Execution step: check the next_step field and guide the user as it says.
     **Never** write memory/ directly (see Constraints)
```

## Coordinator path details (Claude Code only)

Read `references/committee-protocol.md` and follow it strictly. Full 6 stages:

- Stage 0: same-day check (if `memory/.committee/<today>/<asset>.md` exists, reuse it directly)
- Stage 1: `prepare_committee` to get the brief
- Stage 2: Round 1 — 3 `Agent({...})` in parallel (Macro + Quant + Risk)
- Stage 3: Round 2 — Cross-challenge (2 Agents)
- Stage 4 (optional): run Round 3+ if not converged
- Stage 5: CIO synthesis (**you** write it yourself, do not delegate)
- Stage 6: `save_committee` to persist

**Critical warning**: the `regime_brief` / `sentiment_brief` /
`valuation_brief` / `reentry_reference` emitted by `prepare_committee` **must** be pasted
verbatim into the corresponding workers' prompts per the instructions: regime + valuation +
sentiment go into Quant; all three blocks + the path reference go into the CIO. Consequences
of omission: Quant loses the probability framing and the defensive-sentinel background; the
CIO's EXPECTED_PATH is made up out of thin air; INDEP_DEFENSE_FLAG
never reaches the transcript → `save_committee`'s deterministic defense downgrade
(crash sentinel) breaks entirely.

## Direct path details (any agent)

```bash
# One command does it all
~/.claude/skills/invest/scripts/run.sh run_committee NDQ.AX
```

JSON output:
```json
{
  "status": "ok",
  "asset": {...},
  "verdict": {"verdict": "ACCUMULATE", "confidence": 0.72, ...},
  "cio_memo": "<full CIO memo markdown>",
  "transcript_path": "memory/.committee/2026-05-09/NDQ.AX.md",
  "next_step": "..."
}
```

Options:
- `--force`: rerun even if already run today (default reads the cache to save tokens)
- `--max-rounds N`: cap on cross-challenge rounds (default 1)

### Daily report (cron on the host-agent side)

```bash
~/.claude/skills/invest/scripts/run.sh daily_report   # = uvx openinvest daily_report
```

Runs the full daily-report pipeline (multi-asset committee + Gemini second opinion +
translator + discipline ledger); stdout emits markdown identical to the email body,
**does not send email** — delivery belongs to the host agent (a Hermes cron can forward
`--no-agent --script` output as-is; report formatting is guaranteed uniformly by the
backend). On a circuit-breaker trip / unconfigured target_assets, stdout is a structured
JSON error. Rerunning reruns the entire committee (burns tokens).

**Prerequisite**: `.env` has `DEEPSEEK_API_KEY`. If the calling agent runs on the user's
machine but has no key, tell the user to run `run.sh init` to configure it first.
(**Remote-mode exception**: the key lives on the hub, not needed locally — see next section.)

## Remote mode (hub-and-spoke, multiple devices sharing one dataset)

> **Recommended new path (2026-07, BETA — not yet field-tested by the author in a real multi-device setup)**: when the hub runs `openinvest-mcp --http` (remote MCP),
> spokes register the HTTP MCP directly — all 18 tools fully available, no CLI forwarding:
> `claude mcp add --transport http openinvest https://<hub>/mcp --header "Authorization: Bearer $INVEST_API_TOKEN"`
> The CLI→REST forwarding below is still supported (maintenance mode); the Coordinator protocol (prepare/save) and
> doctor/event_check still go through it. For deployment see backend wiki 08 §9.

With `INVEST_API_BASE` set in `.env`, this machine is a **client**: every subcommand is
auto-forwarded to the remote hub (another machine running the invest web_api), and
**this machine has no `memory/` and should not have one**.
All data (portfolio/strategy/verdicts/prompts) lives on the hub; change it once and every device sees it.

Minimal client `.env` (no DeepSeek key / Gmail / memory needed):

```bash
INVEST_API_BASE=https://your-hub.example.com   # or http://10.0.0.x:8765
INVEST_API_TOKEN=<the hub's token of the same name>   # only needed if the hub has auth enabled
# If going through Cloudflare Tunnel + Access, use this pair instead:
# CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=...
```

Behavior differences (all other commands are forwarded transparently, **output has the
same shape as local**, decision tree applies as usual):

| Command | Remote-mode behavior |
|------|--------------|
| `doctor` | Returns **hub-perspective** checks + an extra `remote` section (api_base / auth mode / connectivity) |
| `init` | **Disabled** (data lives on the hub; connecting to the hub only needs the two .env lines above). Error includes a hint |
| `live_prices` / `correlate` | Still run **locally** (pure yfinance, touches no data) |
| `run_committee` | Runs on the **hub** (DeepSeek key is on the hub); the CLI polls automatically until done; same-day cache uses the hub's date semantics |
| `prepare/save_committee` | Via hub RPC — the Coordinator protocol (spawn 4 subagents) is **completely unchanged** |
| `buy/sell/deposit/...` write ops | Land in the hub ledger (history `source: skill_remote`) |
| `event_check` | Forwards the hub's manual scan; `--live` / `--recall` disabled (hub cron already covers them) |

**Discipline**: in remote mode this machine has no `memory/`, so "reading/writing memory
files directly" does not even exist — everything goes through `run.sh` or the hub API.
In the Web API table below, replace `:8765` with `$INVEST_API_BASE` in remote mode, and
add `Authorization: Bearer $INVEST_API_TOKEN` when curling.

## Tool lookup (MCP first; long tail in references/tools.md)

**MCP users** (auto-registered once the plugin is installed; same for Claude Code / Codex): status / strategy /
history / live_prices / what_if / discipline / decisions / explain_decision /
record_execution / ingest_event / buy / sell / deposit / withdraw / set_allocations / track_asset / untrack_asset / run_committee — 18 tools total,
schemas auto-discovered; call them directly, no table lookup needed.

**CLI/REST agents**, or long-tail operations MCP does not cover (trades intent flow /
config whitelist / events / holdings import / gold-specific endpoints) → read
`references/tools.md` (full subcommand table + endpoint table). Full OpenAPI:
`http://127.0.0.1:8765/openapi.json`.

**Subcommand names are a closed set** — commands outside the table do not exist. When
tempted to call names like `get_committee_context` / `analyze_asset`, stop and check the
table — you are most likely hallucinating; you probably want `prepare_committee`
or `run_committee`. All output is JSON; **always quote numbers from the JSON**, never from memory/*.md.

### Importing holdings from a broker-app **screenshot** (you do the OCR; backend has zero dependencies)

When the user sends a broker holdings **screenshot**: **read the image yourself** (you
have vision), convert each row into text as
`symbol/quantity/cost/currency/channel`, then use `import --text "..."` (or POST
/api/holdings/import) — the backend LLM only parses text, it never receives images.
Preview first without `--commit`; after the user verifies, run with `--commit` for a
non-destructive write.

## Feeding news (your search beats any crawler)

You have far stronger search capability than the backend crawler (including
Chinese-language sources). **When you come across finance news relevant to the user's
holdings while browsing/searching, proactively call `ingest_event` to feed it into the
event ledger** (MCP tool, or CLI `ingest_event
--title --url [--snippet --source]`) — the backend handles normalization / severity
grading / dedup / RAG recall, and resending the same item never double-books.
A-share / regional-market news especially: that is the crawler's blind spot and you are
the only source. If the host has a quotes/news skill installed (e.g. Longbridge), its
news is worth feeding too — the ledger cares about the information, not where it came from.

## Decision-loop workflow (Decision Review + Reflection)

openInvest does the bookkeeping; **you do the collection** — that is the host agent's
core duty (issue #133 Decision 2).

**User asks "why HOLD today / why did it tell me to sell"** (Decision Review):
1. `explain_decision <decision_id>` for the full 4-role debate transcript + CIO memo + path snapshot
2. Combine with `status` (current portfolio) + `GET /api/user` (wealth_context) if needed
3. Answer using evidence from the transcript — do not invent reasons yourself

**User reacts to a recommendation with "I didn't buy / I bought / I disagree"** (Reflection):
1. First ask one question about the reason (valuation too high? insufficient funds?
   disagrees with the committee? forgot?) — do not skip this; the reason is the one
   piece of information the system cannot obtain on its own
2. `record_execution <decision_id> [--rejected] --reason "..."` to write it back (idempotent; the user may change their answer anytime)
3. If the user actually traded → guide them to log the trade (a same-symbol,
   same-direction fill within 7 days is also auto-matched as a fallback)

**User asks "how often did I follow the advice / is the committee reliable"**:
`decisions --days 90` → adoption rate + the full verdict↔intervention↔execution↔outcome chain per decision;
pair with `discipline` for the counterfactual P&L of rule-blocked actions. When the user
rejects the same class of recommendation several times in a row, proactively point out
"the divergence pattern between you and the committee" — that is not a bad thing; it is
a signal worth recording.

## Constraints (guard these, do not break them)

- **Before analyzing portfolio / concentration / risk, always read `GET /api/user` for wealth_context** —
  forget this and you repeat **the 2026-05-12 mistake**: the user had entered a ¥4M family
  backup, the agent ran `status` without reading user, and per legacy PWM logic shouted
  "60% concentration overweight → recommend TRIM". Wrong. The correct approach:
  - No wealth_context filled in → judge liquidity by portfolio cash + use the 25-35% concentration alert band
  - Filled in → use the WealthContextOfficer perspective:
    * Concentration % is computed as `portfolio_value / (portfolio + emergency_buffer_cny)`, not portfolio alone
    * `family_backup_available=true` → low portfolio cash is **not** a liquidity risk
    * `account_purpose="零花钱账户"` (pocket-money account) → tolerate larger drawdowns; `"退休金"` (retirement fund) → lean toward trimming
  - The cap on any add-on buy is **always** portfolio cash (**never touch the backup**) — this never changes
- **Do not run `daily_report` proactively in conversation** — unless the user explicitly says "run the deep analysis" / "run full report"
  or you are setting up the daily cron. That path burns DeepSeek tokens. In conversation, single-asset `run_committee` is enough.
- **Never fabricate live prices.** Always go through `run.sh status` or `live_prices`. yfinance may return
  stale data; watch the `is_stale` flag.
- **Never write `memory/` directly.** All state changes go through CLI subcommands / MCP tools (atomic write +
  fcntl lock + audit trail). Direct editing causes schema drift + concurrent-write corruption.
- **Do not rerun the committee for the same asset on the same day** — `run_committee` reads the cache by default;
  on the Coordinator path, check with `ls memory/.committee/<today>/<SYM>.md` first.
- **Do not fabricate CIO confidence.** When workers disagree sharply, honestly write `confidence: 0.4-0.5`.
- **Do not leak the user's email or other personally identifying information** — never hard-code it in output.

## Where to look when something breaks

Read the `doctor` JSON output carefully. Every check has a `hint` field telling you how to fix it.
If doctor is all green but a subcommand still fails, read `references/troubleshooting.md`.

Common Direct-path errors:
- `error: DEEPSEEK_API_KEY 未设` (backend's verbatim "key not set" message) → no key in `.env`; run `run.sh init` to configure it
- `error: asset X not in strategy.target_assets` → add X to the strategy first,
  see `references/adding-assets.md`

## References index

| File | When to read |
|------|--------|
| `references/tools.md` | Full subcommand table + Web API endpoint table (long-tail operations MCP does not cover) |
| `references/committee-protocol.md` | Running the committee on the Coordinator path (Claude Code only) |
| `references/two-paths.md` | Understanding Coordinator vs Direct / DeepSeek cron triggering |
| `references/adding-assets.md` | User wants to track a new symbol |
| `references/troubleshooting.md` | doctor all green but still failing |
| `references/onboarding.md` | **First-time install goes to the `invest-setup` skill**; this file is kept as a detailed reference |

For deeper architectural context see the project wiki:
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
