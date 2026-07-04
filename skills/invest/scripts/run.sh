#!/bin/bash
# Skill wrapper —— 薄转发：数据目录初始化 + uvx 从 PyPI 拉后端跑。
#
# 2026-07 起后端从 PyPI 分发（pypi.org/project/openinvest），本脚本不再
# git clone / uv sync / 自愈更新——那 180 行 bash 全部退役：
#   - 后端代码：uvx 按需拉 openinvest 包（缓存于 uv cache，首跑需网络）
#   - 数据目录：$INVEST_HOME（默认 ~/openInvest），只放 memory/ db/ .env static/
#   - 更新：`run.sh update`（= uvx --refresh，显式更新，不再启动时静默 git pull）
#   - GUI dist 拉取 / 远端模式提示等业务逻辑收进 openinvest-web（Python）
#
# 老用户（~/openInvest 是 git clone）零迁移：clone 目录直接当数据目录用。
# 想 pin 版本：export OPENINVEST_SPEC='openinvest==0.16.0'

set -euo pipefail

export INVEST_HOME="${INVEST_HOME:-$HOME/openInvest}"
SPEC="${OPENINVEST_SPEC:-openinvest}"

if ! command -v uvx >/dev/null 2>&1; then
    echo "❌ uv 未安装。装一下：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    # mcp 模式 stdout 是 JSON-RPC 通道，错误 JSON 只在非 mcp 时输出
    [ "${1:-}" != "mcp" ] && echo '{"status":"error","error":"uv 未安装","hint":"运行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 后重试"}'
    exit 1
fi

mkdir -p "$INVEST_HOME"
cd "$INVEST_HOME"

case "${1:-}" in
  mcp)
    # stdout 是 JSON-RPC 通道；uvx 的安装进度本来就走 stderr，无需借道
    exec uvx --from "$SPEC" openinvest-mcp
    ;;
  gui)
    exec uvx --from "$SPEC" openinvest-web
    ;;
  update)
    echo "⬆️  刷新 openinvest 到 PyPI 最新..." >&2
    exec uvx --refresh --from "$SPEC" openinvest doctor
    ;;
  "")
    cat >&2 <<'EOF'
Usage: run.sh <subcommand> [args]

Onboarding（首次必跑）:
  doctor / init [--from-stdin]

只读:
  status | strategy | history [-n N] | live_prices | what_if | decisions

写:
  buy | sell | deposit | withdraw | record_execution

委员会:
  prepare_committee SYM / save_committee SYM   （Coordinator，Claude Code）
  run_committee SYM [--force]                  （Direct，需 DEEPSEEK_API_KEY）

其他:
  gui      启动 Web GUI（http://127.0.0.1:8765）
  mcp      MCP stdio server（plugin .mcp.json 自动走这条）
  update   更新后端到 PyPI 最新版

完整子命令表：uvx openinvest --help 或 skills/invest/references/tools.md
EOF
    exit 1
    ;;
  *)
    exec uvx --from "$SPEC" openinvest "$@"
    ;;
esac
