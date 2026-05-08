#!/bin/bash
# Skill wrapper —— 首次调用时自动 bootstrap openInvest 项目，之后每次调用 delegate 到 scripts/skill.py
#
# 任何人（不只原作者）都能部署：
#   1. 装好 Claude Code 并 link 本 skill
#   2. 首次调用自动 git clone https://github.com/longsizhuo/openInvest.git → $INVEST_HOME
#   3. 自动 `uv sync` 装 venv
#   4. 自动 `python -m scripts.sync_gui_dist` 拉前端 dist 到 static/（130KB，零成本，让 Web GUI 可用）
#   5. Onboarding：Claude 调 `run.sh doctor` 检测到缺 memory/.env → 问用户 6 个问题 →
#      `run.sh init --from-stdin` 写入
#   6. 之后每次子命令都 delegate 给 scripts/skill.py
#
# 仓库重命名记录（2026-04）：longsizhuo/invest → longsizhuo/openInvest
# Fork 用户改 INVEST_REPO 环境变量即可。

set -euo pipefail

INVEST_HOME="${INVEST_HOME:-$HOME/projects-review/invest}"
INVEST_REPO="${INVEST_REPO:-https://github.com/longsizhuo/openInvest.git}"
INVEST_BRANCH="${INVEST_BRANCH:-main}"

# ============ Bootstrap ============

# 1) clone 仓库（如果不存在）
if [ ! -d "$INVEST_HOME" ]; then
    echo "🌱 第一次跑：克隆 openInvest 到 $INVEST_HOME..." >&2
    mkdir -p "$(dirname "$INVEST_HOME")"
    git clone --branch "$INVEST_BRANCH" "$INVEST_REPO" "$INVEST_HOME" >&2
fi

cd "$INVEST_HOME"

# 2) uv sync 装依赖
if [ ! -d ".venv" ]; then
    echo "📦 第一次跑：用 uv 装依赖..." >&2
    if ! command -v uv >/dev/null 2>&1; then
        echo "❌ uv 未安装。装一下：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        exit 1
    fi
    uv sync --frozen --python 3.13 >&2
fi

# 3) GUI dist：从 invest-gui dist-latest release 拉静态文件到 static/
#    自动安装但不自动启动——uvicorn 是显式 `run.sh gui` 才起
if [ ! -f "static/index.html" ]; then
    echo "🎨 第一次跑：拉 GUI dist 到 static/..." >&2
    .venv/bin/python -m scripts.sync_gui_dist >&2 || {
        echo "⚠️  GUI dist 拉取失败（可能 GitHub 网络问题）。跳过——CLI/skill 模式仍可用。" >&2
        echo "   想要 GUI 时手动跑：cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist" >&2
    }
fi

# ============ 子命令分发 ============

# 处理本 wrapper 自带的 gui 子命令（不进 Python skill.py）
if [ "${1:-}" = "gui" ]; then
    PORT="${INVEST_WEB_PORT:-8765}"
    HOST="${INVEST_WEB_HOST:-127.0.0.1}"

    # 检查 GUI 是否已经在跑（端口被占）
    if command -v lsof >/dev/null && lsof -ti tcp:$PORT >/dev/null 2>&1; then
        EXISTING_PID=$(lsof -ti tcp:$PORT)
        echo "ℹ️  端口 $PORT 已被进程 $EXISTING_PID 占用，可能 GUI 已在跑。" >&2
        echo "   浏览器开 http://$HOST:$PORT 看一下；如要重启先 kill $EXISTING_PID" >&2
        exit 0
    fi

    if [ ! -f "static/index.html" ]; then
        echo "⚠️  static/index.html 不存在，先拉 GUI dist..." >&2
        .venv/bin/python -m scripts.sync_gui_dist >&2
    fi

    echo "🚀 启动 Web GUI 在 http://$HOST:$PORT （Ctrl+C 退出）" >&2
    echo "   API: http://$HOST:$PORT/api/...  Swagger: http://$HOST:$PORT/docs" >&2
    exec .venv/bin/python -m uvicorn connectors.web_api:app --host "$HOST" --port "$PORT"
fi

if [ -z "${1:-}" ]; then
    cat >&2 <<'EOF'
Usage: run.sh <subcommand> [args]

Onboarding（首次必跑）:
  doctor                   健康检查 memory + .env + GUI 状态（JSON 输出给 Claude 读）
  init [--from-stdin]      从 stdin JSON 完成 onboarding（Claude 走这条）
  init                     交互式 CLI onboarding（手动用）

只读 / 快速（无 LLM 调用）:
  status                   持仓 + 实时价 + 浮盈
  strategy                 target_assets + Dreaming 长期 insight
  history [-n N]           最近 N 笔交易 + 委员会决议
  live_prices              VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F
  what_if [--gold-pct ±N]  P&L 情景模拟

Web GUI:
  gui                      启动 uvicorn 前端（http://127.0.0.1:8765，Ctrl+C 退出）

委员会（Coordinator-Worker）:
  prepare_committee SYM    输出 brief + 4 角色 prompt 给 Claude 做 fan-out
  save_committee SYM       从 stdin 落盘 transcript 到 memory/.committee/<date>/
EOF
    exit 1
fi

exec .venv/bin/python -m scripts.skill "$@"
