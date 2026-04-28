#!/bin/bash
# Skill wrapper - bootstraps invest project on first use, then delegates to scripts/skill.py
#
# Anyone (not just the original author) can deploy this:
#   1. Install Claude Code with this skill
#   2. First call auto-clones https://github.com/longsizhuo/invest.git → $INVEST_HOME
#   3. Auto-runs `uv sync` to set up venv
#   4. Then delegates every subcommand to scripts/skill.py inside the project

set -euo pipefail

INVEST_HOME="${INVEST_HOME:-$HOME/projects-review/invest}"
INVEST_REPO="${INVEST_REPO:-https://github.com/longsizhuo/invest.git}"
INVEST_BRANCH="${INVEST_BRANCH:-feat/openclaw-overhaul}"

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

# Bootstrap: memory dirs (.env / memory/ are user-private, never in repo)
if [ ! -f "memory/user.md" ]; then
    echo "⚠️  memory/ not initialized. Run:" >&2
    echo "   cd $INVEST_HOME && python scripts/migrate_profile.py" >&2
    echo "   (Need user_profile.json copied from user_profile.example.json first)" >&2
fi

if [ -z "${1:-}" ]; then
    echo "Usage: run.sh <subcommand> [args]" >&2
    echo "Subcommands: status | strategy | history | what_if | live_prices | prepare_debate <SYM> | save_debate <SYM>" >&2
    exit 1
fi

exec .venv/bin/python -m scripts.skill "$@"
