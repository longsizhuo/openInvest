<div align="center">
  
<img width="120" height="120" alt="owl-02-lineart-gold" src="https://github.com/user-attachments/assets/e4c1efa0-026a-4777-ae62-48b9b0be435c" />

# openInvest

**A self-hosted investment decision engine built for modern AI agents. Multi-agent information isolation and cross-challenge protocol, providing an auditable decision trail (Audit Trail).**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/Skill-Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/longsizhuo/openInvest?style=social)](https://github.com/longsizhuo/openInvest)

[📚 Full Architecture Wiki](docs/wiki/README.md) · [🇨🇳 中文版](README_zh.md)

</div>

---

## What is OpenInvest?

OpenInvest is a self-hosted investment decision engine built for modern AI agents.

It provides a verifiable investment committee, evidence-based reasoning, long-horizon backtesting, and auditable decision records. Instead of replacing Claude Code, Codex, Hermes, or OpenClaw, OpenInvest is designed to power them.

## Why OpenInvest?

Most AI investment assistants try to become better chatbots. OpenInvest takes a different approach.

It focuses on building a transparent, verifiable, and auditable decision engine that can be integrated into any modern AI agent. Instead of competing with personal AI agents, OpenInvest is designed to amplify them. 

Every improvement in Claude Code, OpenClaw, Codex, or future agents automatically makes OpenInvest more capable.

---

## Live Performance & PnL

<div align="center">
  <img src="https://raw.githubusercontent.com/longsizhuo/openInvest/pnl-data/docs/pnl_chart.svg" alt="PnL chart" width="100%"/>
  <sub>Data feed is automatically updated every 2 hours using `jobs/pnl_snapshot` and pushed to the <a href="https://github.com/longsizhuo/openInvest/tree/pnl-data">pnl-data branch</a></sub>
  <br/>
  <sub>Upper half: 30-day net asset value trend · Lower half: Net asset value comparison against 8 benchmark assets (transparent disclosure, <b>not an alpha claim</b>—the committee's proven value is discipline and transparency, not excess return, see <a href="docs/wiki/adr/023-honest-positioning-not-alpha.md">ADR-023</a>)</sub>
  <br/>
  <sub>📌 <b>Note</b>: The current chart shows the author's live production portfolio. After self-hosting, the system will automatically render your own equity curve based on the holdings defined in your `memory/` directory.</sub>
</div>

<!-- OUTPERFORM_FEED_START -->
<!-- OUTPERFORM_FEED_END -->

*   **Benchmark Portfolio**: The system introduces 8 standard control benchmarks across 4 quadrants (AI advisors / Mutual funds / Wealth management / Broad market index). For details on the comparison methodology and data cleaning logic, see [docs/wiki/README.md](docs/wiki/README.md).
*   **System Self-Disclosure**: This system is an **auditing tool to eliminate human investment cognitive biases and enforce reasoning transparency**, not a return-amplifying black box. Latest automated audit (`docs/verdict_accuracy.md`): Directional verdicts (excluding HOLD) have a true hit rate of **42.2%** (n=56, **below random**); `HOLD` accounts for **56%** of all decisions. The system's value lies in transparency and discipline (mostly staying inactive, low turnover), **not directional prediction**. Detailed log stream can be found in [docs/verdict_accuracy.md](docs/verdict_accuracy.md).

---

## Product Philosophy

OpenInvest is not another chatbot or AI agent.

We believe the future belongs to increasingly capable personal agents such as Claude Code, Codex, Hermes, OpenClaw, and future Agent Operating Systems. These agents stay with their users over time and understand their goals, preferences, and long term context.

OpenInvest focuses on investment decisions.

OpenInvest provides:
*   Verifiable investment committees
*   Evidence-based reasoning
*   Long-horizon backtesting and calibration
*   Auditable decision records
*   Decision protocols for AI agents

Modern AI agents typically provide:
*   Long term memory
*   Natural conversation
*   User understanding
*   Decision reflection
*   Personalized guidance

OpenInvest understands the market. Your agent understands you.

```
                   User
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
   Claude Code             OpenInvest
(User Understanding)   (Market Understanding)
         │                       │
         └───────────┬───────────┘
                     ▼
            Better Investment Decisions
```

> **Your agent knows you. OpenInvest knows investing.**

### Avoiding User Ownership
OpenInvest intentionally avoids "owning" the user. Most AI products try to own everything—memory, persona, chat history, and workspaces. OpenInvest takes a back seat. It exposes clean APIs, CLI commands, and Claude Code skills, letting your primary agent manage the conversation and context while OpenInvest powers the underlying investment intelligence.

---

## Features

*   **Multi-Agent Investment Committee**: Isolated analysis and round-rebuttal debate.
*   **Coordinator-Worker Architecture**: Prevents context contamination and role hallucination.
*   **Information Isolation**: Rigidly blocks quant and risk analysts from out-of-boundary contexts.
*   **Auditable Decision Trail**: Clean logs showing exactly "why" each decision was made.
*   **Markdown-as-a-Database**: Frontmatter (YAML) + Markdown (Body) as the single source of truth.
*   **Long-Horizon Backtesting**: Built-in test harness with lookahead bias protections.
*   **Dreaming-Based Memory Consolidation**: Nightly memory distillation to prevent context drift.
*   **Self-Hosted / Zero-Cost**: Powered directly by your local agent's reasoning resources.
*   **Claude Code Skill**: Lightweight plugin with interactive bootstrap wizard.
*   **Automated Deployment**: GitHub Actions workflow to run the committee and email reports daily.

---

## Quick Start

### 1. Integrate with Claude Code (Recommended)
Add the lightweight skill from the official registry. The host agent will automatically pull the core code and align dependencies on first run:
```bash
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest
```

### 2. Standalone — MCP server or CLI (no clone needed)
The backend ships on [PyPI](https://pypi.org/project/openinvest/); `~/openInvest` holds only your data:
```bash
# MCP (15 tools, any MCP client)
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest-mcp

# or plain CLI
INVEST_HOME=~/openInvest uvx openinvest status
```
Send `set up invest` (or `帮我初始化 invest`) to any skill-enabled AI terminal. The system will trigger an interactive bootstrap wizard to guide you through:
1. Detecting the `memory/` state storage path and `.env` configuration.
2. 5-dimensional profiling (Legal name, Risk capacity, Debt structure, Initial holdings, and optional keys).
3. Running static data migration to immediately generate your first asset exposure memo.

> 💡 **Zero-Cost Execution**: In skill interactive mode, the committee's underlying reasoning relies entirely on the host agent's (e.g. Claude Code) reasoning pipeline. **No third-party API Key is consumed**. You only need to configure an API key when setting up automated crons or calling independent Web APIs.

For self-hosting details, see [docs/QUICK_START.md](docs/QUICK_START.md). (The bundled Web GUI was retired on 2026-07-05 — all capabilities are exposed via CLI/MCP; a standalone frontend may return later.)

### 3. Serverless Self-Hosting (GitHub Actions)
Run the committee automatically via GitHub Actions and receive daily digest emails.
> ⚠️ **Fork must be set to Private**: State files (holdings, verdicts) will be committed back to your fork. Public forks will leak your private financial information.

1. **Fork this repository** and change its visibility to **Private** (Settings -> Visibility).
2. Run `set up invest` locally to generate the initial `memory/` folder, then commit and push it to your private fork:
   ```bash
   git add -f memory/ && git commit -m "chore: init memory state" && git push
   ```
3. In your fork's **Settings -> Secrets and variables -> Actions**, add the following Secrets:
   *   `LLM_API_KEY` (or `DEEPSEEK_API_KEY`): API key to run the committee.
   *   `EMAIL_SENDER` / `EMAIL_PASSWORD`: Gmail address + [App Password](https://support.google.com/accounts/answer/185833).
   *   `DIGEST_EMAIL_TO`: Recipient email address.
4. **Enable Workflows** under the **Actions** tab. The workflow runs automatically at 10:00 AM (Beijing time) daily; you can also manually trigger `daily-report` via **Run workflow**.

---

## Architecture & Multi-Agent Orchestration

openInvest does not run a mock debate in a single LLM session. The system enforces an **Information Isolation Contract** at the `core/committee/` layer, orchestrating 4 independent LLM processes in a directed acyclic graph (DAG):

```
                [ Macro Data Injection ]
                           │
                 ▼ 1. Macro Alignment Context
             ┌──────────────────────────┐
             │    Macro Strategist      │ (VIX / Interest rate spread / Currency momentum)
             └─────────────┬────────────┘
                           │
                 ▼ 2. Async Multi-Dimensional Scrutiny (Async DAG)
             ┌─────────────┴────────────┐
             ▼                          ▼
   ┌──────────────────┐        ┌──────────────────┐
   │  Quant Analyst   │        │   Risk Officer   │
   │ (RSI / Momentum) │        │ (Concentration)  │
   │                  │        │                  │
   │ 🛑 No Holdings   │        │ 🛑 No Indicators │
   └─────────┬────────┘        └─────────┬────────┘
             │                           │
             └─────────────┬─────────────┘
                           │
             ▼ 3. Round 2 Rebuttal & Cross-Challenge
             │ Mutual feedback loop for signal correction
             ▼
   ┌──────────────────────────────────────────────┐
   │         Chief Investment Officer (CIO)       │
   └───────────────────────┬──────────────────────┘
                           │
             ▼ 4. Deterministic State Persistence
          [ BUY / ACCUMULATE / HOLD / TRIM / SELL ]
```

1.  **Macro Strategist**: Assesses the global macro landscape (VIX, yield curve spread, core currency matrix) to establish the portfolio's risk threshold.
2.  **Quant Analyst**: A pure mathematical momentum and technical indicator filter. **Strictly blocked from knowing portfolio holdings** to eliminate human attachment and loss-aversion biases.
3.  **Risk Officer**: Focuses entirely on tail risks (drawdown buffers, concentration limits, solvency multipliers). **Strictly blocked from technical indicators** to make objective asset exposure rulings.
4.  **Round 2 Rebuttal**: Quant and Risk analysts are fed each other's Round 1 reports in Round 2, challenging boundaries until signals converge or safety valves trigger.
5.  **CIO (Chief Investment Officer)**: Synthesizes the audited reports and outputs a structured `Verdict` (BUY / ACCUMULATE / HOLD / TRIM / SELL) with a confidence level. **No auto-order execution occurs**; final action remains strictly up to the human auditor.

---

## Core Design

*   **Coordinator-Worker Pattern**: Workers operate in isolated namespaces. Boundary constraints are hardcoded at the framework layer in Python to prevent attention contamination in large multi-role prompts.
*   **Markdown-as-a-Database**: The system uses Frontmatter (YAML) + Markdown (Body) as the single source of truth. Leveraging `fcntl.flock` process file locks and temporary atomic file replacement, it provides a tamper-proof investment audit trail natively tracked by Git.
*   **Three-Phase Dreaming Consolidation**: Distills daily decisions against actual market outcomes nightly (Light Sleep $\rightarrow$ REM $\rightarrow$ Deep Sleep) to consolidate long-term insights, preventing Large Language Model (LLM) context drift over long execution spans.

---

## Configuration & Environment Settings

### LLM Provider Configuration
The system defaults to DeepSeek endpoints but supports any standard OpenAI-compatible API. Map the variables in `.env` accordingly (ensure `LLM_MODEL` maps to the provider's exact model ID):

```env
# === Option A: DeepSeek (Default) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# === Option B: Aliyun Qwen (OpenAI Compatible) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-max

# === Option C: Zhipu AI (GLM OpenAI Compatible) ===
LLM_API_KEY=xxxxxxxxxxxxxxxx.xxxxxxxxx
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v1
LLM_MODEL=glm-4-flash
```

### Tunable Runtime Overrides
Pursuant to [ADR-017](docs/wiki/adr/017-config-via-api.md), runtime overrides persist in `memory/.state/config_overrides.json` and share the same precedence priority across CLI, REST API, and `.env`:

| Config Key | Data Type (Default) | Architectural Effect |
|---|---|---|
| `verdict.concentration_lens_enabled` | `bool` (`true`) | **Portfolio Concentration Filter**. When enabled, alerts/TRIM are triggered on over-concentrated assets. If disabled, concentration will not trigger TRIM warnings (volatility and valuation wind-downs still apply). See [ADR-019](docs/wiki/adr/019-remove-solvency-concentration-override.md) |
| `verdict.risk_profile` | `str` (`"steady"`) | Risk appetite configuration: `steady` / `aggressive` (allows larger scale buys during downturns). |
| `verdict.gold_defense_dca_enabled` | `bool` (`true`) | Gold defense filter. Splits large allocations into multi-period DCAs during high volatility spikes. |
| `dca.auto_dca_enabled` | `bool` (`false`) | Global toggle for automated dollar-cost averaging decisions. |
| `dca.auto_dca_amount_cny` | `float` (`0.0`) | Base CNY allocation amount for each auto-DCA period. See [ADR-018](docs/wiki/adr/018-dca-dip-reserve.md) |

#### Overriding Parameters at Runtime (e.g. Disabling Concentration Lens)
```bash
# Method 1: Using the CLI
uvx openinvest config --set verdict.concentration_lens_enabled false

# Method 2: REST API (deprecated surface — remote hub mode only)
curl -X PUT http://localhost:8765/api/config -d '{"key":"verdict.concentration_lens_enabled","value":false}' 
```

---

## Disclaimers & Backtest Limitations

1.  **No Financial Advice**: This system is a decision-support tool powered by LLMs. Output memos represent simulated reasoning based on deterministic data and do not constitute asset allocation advice.
2.  **Backtest Time-Lock & Lookahead Guard**: The backtest engine (`scripts/backtest_runner.py`) has a hardcoded safety valve: **it rejects backtests for `decision_date > 2024-06-30` by default** (override with `--allow-lookahead`). Since mainstream foundation models have training cutoff dates around mid-2024, backtesting on later intervals introduces severe **Lookahead Bias** (model pre-training leakage). Parameter tuning, Optuna sweeps, and prompt optimization must run strictly on historical windows prior to June 30, 2024.

---

## Acknowledgments

*   [MiMo](https://mimo.mi.com/) — Special thanks to MiMo Quantitative Lab for sponsoring production-grade high-performance LLM inference (powering `mimo-v2.5-pro` long-horizon sweeps).
*   [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — Theoretical foundation for the three-phase sleep-cycle memory distillation framework.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
