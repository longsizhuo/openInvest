#!/bin/bash
# Skill wrapper - bootstraps invest project on first use, then delegates to scripts/skill.py
#
# Anyone (not just the original author) can deploy this:
#   1. Install Claude Code with this skill
#   2. First call auto-clones https://github.com/longsizhuo/invest.git → $INVEST_HOME
#   3. Auto-runs `uv sync` to set up venv
#   4. First-time onboarding: Claude detects missing memory/.env via `run.sh doctor`,
#      asks user 5 questions, then calls `run.sh init --from-stdin` with JSON.
#   5. After onboarding, delegates every subcommand to scripts/skill.py inside the project

set -euo pipefail

INVEST_HOME="${INVEST_HOME:-$HOME/projects-review/invest}"
INVEST_REPO="${INVEST_REPO:-https://github.com/longsizhuo/invest.git}"
INVEST_BRANCH="${INVEST_BRANCH:-main}"

# Bootstrap: clone if missing
if [ ! -d "$INVEST_HOME" ]; then
    echo "🌱 Bootstrapping invest project at $INVEST_HOME..." >&2
    mkdir -p "$(dirname "$INVEST_HOME")"
    git clone --branch "$INVEST_BRANCH" "$INVEST_REPO" "$INVEST_HOME" >&2
fi

cd "$INVEST_HOME"

# Bootstrap: venv via uv
if [ ! -d ".venv" ]; then
    echo "📦 Setting up venv via uv (one-time)..." >&2
    if ! command -v uv >/dev/null 2>&1; then
        echo "Error: uv not installed. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        exit 1
    fi
    uv sync --frozen --python 3.13 >&2
fi

if [ -z "${1:-}" ]; then
    cat >&2 <<'EOF'
Usage: run.sh <subcommand> [args]

Onboarding (first-time):
  doctor                   检查 memory + .env + 凭据状态（输出 JSON 给 Claude 读）
  init [--from-stdin]      从 stdin JSON 完成 onboarding（Claude 走这条）
  init                     交互式 CLI onboarding（手动用）

Read-only / fast:
  status                   持仓 + 实时价 + 浮盈
  strategy                 target_assets + Dreaming 长期 insight
  history [-n N]           最近 N 笔交易 + 委员会决议
  live_prices              VIX / TNX / USDCNY / NDQ / GC=F
  what_if [--gold-pct ±N]  P&L 情景模拟（无 LLM）

Investment Committee (Coordinator-Worker):
  prepare_committee SYM    输出 brief + prompts 给 Claude 做 4 角色 fan-out
  save_committee SYM       从 stdin 落盘 transcript 到 memory/.committee/<date>/
EOF
    exit 1
fi

exec .venv/bin/python -m scripts.skill "$@"
