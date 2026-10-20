#!/bin/bash
# Skill wrapper —— 薄转发：数据目录初始化 + uvx 从 PyPI 拉后端跑。
#
# 2026-07 起后端从 PyPI 分发（pypi.org/project/openinvest），本脚本不再
# git clone / uv sync / 自愈更新——那 180 行 bash 全部退役：
#   - 后端代码：uvx 按需拉 openinvest 包（缓存于 uv cache，首跑需网络）
#   - 数据目录：$INVEST_HOME（默认 ~/openInvest），只放 memory/ db/ .env static/
#   - 更新：后端版本固定在下方 SPEC，随插件发版更新；`run.sh update`（= uvx --refresh）只重下这个固定版本，用于修坏掉的缓存
#   - GUI dist 拉取 / 远端模式提示等业务逻辑收进 openinvest-web（Python）
#
# 老用户（~/openInvest 是 git clone）零迁移：clone 目录直接当数据目录用。
# 后端版本默认固定为下方 SPEC（随发版由 release-please 更新）；换版本：export OPENINVEST_SPEC='openinvest==X.Y.Z'

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SCRIPT_DIR = plugin/skills/invest/scripts，仓库根在 4 层之上（plugin/ 重排后
# 比旧的 skills/invest/scripts 多一层，别再蒙对——2026-07 曾因此指向 plugin/ 而非仓库根）
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SRC_ROOT="$REPO_ROOT/src"
export INVEST_HOME="${INVEST_HOME:-$HOME/openInvest}"
SPEC="${OPENINVEST_SPEC:-openinvest==0.67.0}"  # x-release-please-version
DEV_MODE="${OPENINVEST_DEV_MODE:-0}"

if ! command -v uvx >/dev/null 2>&1; then
    echo "❌ uv 未安装。安装方法：https://docs.astral.sh/uv/getting-started/installation/" >&2
    # mcp 模式 stdout 是 JSON-RPC 通道，错误 JSON 只在非 mcp 时输出
    [ "${1:-}" != "mcp" ] && echo '{"status":"error","error":"uv 未安装","hint":"按 https://docs.astral.sh/uv/getting-started/installation/ 安装 uv 后重试"}'
    exit 1
fi

# 跑后端；只有它一个字都没往 stdout 打就失败时才补一条结构化提示。
# CLI 的业务错误（参数缺失、币种不符……）自己会打印 JSON 说清原因，再追加"检查网络 / uv sync"
# 会把 agent 带去重装依赖、查网络（2026-10 #363 实测）。没输出 = 拉包失败或后端起不来。
# tee 不影响 stdin（init --from-stdin 照常读）；pipefail 下 rc 取后端的退出码。
run_with_hint() {
    local hint="$1"; shift
    local out rc=0
    out="$(mktemp)"
    # 被 Ctrl-C / SIGTERM 打断也清掉。路径在这里就展开：EXIT 时 local 变量已出作用域，
    # set -u 下引用它会让 trap 报错、把退出码改成 1
    trap "rm -f '$out'" EXIT
    "$@" | tee "$out" || rc=$?
    if [ "$rc" -ne 0 ] && [ ! -s "$out" ]; then
        echo "{\"status\":\"error\",\"error\":\"openinvest 执行失败 (exit $rc)\",\"hint\":\"$hint\"}"
    fi
    rm -f "$out"
    return "$rc"
}

mkdir -p "$INVEST_HOME"
DATA_DIR="$INVEST_HOME"
cd "$DATA_DIR"

case "${1:-}" in
  mcp)
    if [ "$DEV_MODE" = "1" ]; then
      cd "$REPO_ROOT"
      export PYTHONPATH="$SRC_ROOT${PYTHONPATH:+:$PYTHONPATH}"
      exec uv run python -m openinvest.connectors.mcp_server
    fi
    # stdout 是 JSON-RPC 通道；uvx 的安装进度本来就走 stderr，无需借道
    exec uvx --from "$SPEC" openinvest-mcp
    ;;
  update)
    if [ "$DEV_MODE" = "1" ]; then
      echo "⬆️  OPENINVEST_DEV_MODE=1：本地源码模式不走 PyPI refresh；请改完代码后运行 uv sync。" >&2
      cd "$REPO_ROOT"
      export PYTHONPATH="$SRC_ROOT${PYTHONPATH:+:$PYTHONPATH}"
      exec uv run python -m openinvest.cli doctor
    fi
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
  mcp      MCP stdio server（plugin .mcp.json 自动走这条）
  update   更新后端到 PyPI 最新版

开发:
  export OPENINVEST_DEV_MODE=1
          走本地源码 `uv run python -m openinvest.cli ...`，用于验证未发布的改动

完整子命令表：uvx openinvest --help 或 skills/invest/references/tools.md
EOF
    exit 1
    ;;
  *)
    if [ "$DEV_MODE" = "1" ]; then
      cd "$REPO_ROOT"
      export PYTHONPATH="$SRC_ROOT${PYTHONPATH:+:$PYTHONPATH}"
      run_with_hint "本地源码模式（OPENINVEST_DEV_MODE=1）没输出任何结果就退出了，原因见 stderr 的报错。常见：依赖没装（先运行 uv sync）。" \
          uv run python -m openinvest.cli "$@"
      exit $?
    fi
    run_with_hint "后端没输出任何结果就退出了，原因见 stderr 的报错。常见：首次运行从 PyPI 拉包失败（检查网络后重试）。" \
        uvx --from "$SPEC" openinvest "$@"
    exit $?
    ;;
esac
