#!/bin/bash
# Skill wrapper —— 首次调用时自动 bootstrap openInvest 项目，之后每次调用 delegate 到 scripts/skill.py
#
# 任何人（不只原作者）都能部署：
#   1. 装好 Claude Code 并 link 本 skill
#   2. 首次调用自动 git clone https://github.com/longsizhuo/openInvest.git → $INVEST_HOME
#   3. 自动 `uv sync` 装 venv
#   4. 自动 `python -m scripts.sync_gui_dist` 拉前端 dist 到 static/（130KB，零成本，让 Web GUI 可用）
#   5. Onboarding：Claude 调 `run.sh doctor` 检测到缺 memory/.env → 问用户 5 个问题 →
#      `run.sh init --from-stdin` 写入
#   6. 之后每次子命令都 delegate 给 scripts/skill.py
#
# 仓库重命名记录（2026-04）：longsizhuo/invest → longsizhuo/openInvest
# Fork 用户改 INVEST_REPO 环境变量即可。

set -euo pipefail

INVEST_HOME="${INVEST_HOME:-$HOME/openInvest}"
INVEST_REPO="${INVEST_REPO:-https://github.com/longsizhuo/openInvest.git}"
INVEST_BRANCH="${INVEST_BRANCH:-main}"

# ============ Bootstrap ============

# 1) clone 仓库（如果不存在）
if [ ! -d "$INVEST_HOME" ]; then
    # 首次 clone 需要 git——缺了给结构化错误，别让 set -e 吐裸 "command not found"
    if ! command -v git >/dev/null 2>&1; then
        echo "❌ git 未安装。首次运行要 git clone 后端仓库。" >&2
        echo '{"status":"error","error":"git 未安装","hint":"用系统包管理器装 git（Debian/Ubuntu: `sudo apt-get install git`；macOS: `xcode-select --install`；Fedora: `sudo dnf install git`），装好后重试本命令。"}'
        exit 1
    fi
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
        # 同时输出 JSON 到 stdout 让 agent (Claude) 能解析为结构化错误并引导用户
        echo '{"status":"error","error":"uv (Python 包管理器) 未安装","hint":"运行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 装好 uv 后重试本命令"}'
        exit 1
    fi
    if ! uv sync --frozen --python 3.13 >&2; then
        echo '{"status":"error","error":"uv sync 失败","hint":"通常是网络拉 Python 3.13 失败 / 依赖锁文件冲突。手动跑 `uv sync --frozen --python 3.13` 看完整输出"}'
        exit 1
    fi
fi

# 3) GUI dist：从 invest-gui dist-latest release 拉静态文件到 static/
#    自动安装但不自动启动——uvicorn 是显式 `run.sh gui` 才起
#    远端模式（INVEST_API_BASE 已设）跳过：GUI 由 hub serve，客户端不需要 static/
if [ -z "${INVEST_API_BASE:-}" ] && [ ! -f "static/index.html" ]; then
    echo "🎨 第一次跑：拉 GUI dist 到 static/..." >&2
    .venv/bin/python -m scripts.sync_gui_dist >&2 || {
        echo "⚠️  GUI dist 拉取失败（可能 GitHub 网络问题）。跳过——CLI/skill 模式仍可用。" >&2
        echo "   想要 GUI 时手动跑：cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist" >&2
    }
fi

# ============ 子命令分发 ============

# 处理本 wrapper 自带的 gui 子命令（不进 Python skill.py）
if [ "${1:-}" = "gui" ]; then
    # 远端模式：GUI 在 hub 上，本机不起 uvicorn，直接给出入口
    if [ -n "${INVEST_API_BASE:-}" ]; then
        echo "{\"status\":\"ok\",\"mode\":\"remote\",\"gui_url\":\"${INVEST_API_BASE}\",\"hint\":\"远端模式：GUI 由 hub serve，浏览器直接打开 gui_url 即可（hub 开了 token/CF Access 的话按其登录方式走）。\"}"
        exit 0
    fi
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

# MCP stdio server（plugin 的 .mcp.json 走这条；bootstrap 已在上面完成）
# stdout 是 JSON-RPC 通道——本分支之前的输出必须全部走 >&2（现状如此，别改坏）
if [ "${1:-}" = "mcp" ]; then
    # 旧 clone 无 MCP adapter（bootstrap 只 clone 不 pull）→ 尝试一次 ff-only 自愈更新
    if [ ! -f connectors/mcp_server.py ]; then
        echo "⬆️  $INVEST_HOME 缺 MCP adapter（旧版后端），尝试 git pull 更新..." >&2
        if git pull --ff-only >&2 && uv sync --frozen --python 3.13 >&2 \
           && [ -f connectors/mcp_server.py ]; then
            echo "✅ 后端已更新" >&2
        else
            echo "❌ 自动更新失败/更新后仍缺模块。手动跑：cd $INVEST_HOME && git pull && uv sync" >&2
            exit 1
        fi
    fi
    exec .venv/bin/python -m connectors.mcp_server
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

MCP（plugin .mcp.json 自动走这条，一般不手动跑）:
  mcp                      起 stdio MCP server（14 工具，JSON-RPC over stdin/stdout）

委员会 — Coordinator 路径（仅 Claude Code，不烧 DeepSeek token）:
  prepare_committee SYM    输出 brief + 4 角色 prompt 给 Claude 做 fan-out
  save_committee SYM       从 stdin 落盘 transcript 到 memory/.committee/<date>/

委员会 — Direct 路径（任意 agent 可用，需要 DEEPSEEK_API_KEY）:
  run_committee SYM [--force]
                           一键跑完 4 角色委员会，输出最终 verdict JSON + CIO memo
EOF
    exit 1
fi

exec .venv/bin/python -m scripts.skill "$@"
