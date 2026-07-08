#!/bin/bash
# invest-backup — 备份/恢复 memory/ + db/ + .env + user_profile.json*
#
# 这些数据全部 .gitignore（含真实持仓/交易/委员会记录/凭据），git 里完全没有
# 历史版本。2026-07-08 migrate_profile.py 被直接跑了一次，无任何 safety guard
# 地把 user.md / strategy.md / portfolio.md 覆盖成 demo 默认值，daily_report
# 因 target_assets 变空而每天早退、邮件全断——这个 skill 就是防这类事故的兜底。
#
# 数据目录解析走 openinvest.paths.INVEST_ROOT（同一套优先级：INVEST_HOME env >
# 仓库标记探测 > cwd），不在这里重复一份路径逻辑——重复路径解析正是这次事故
# 的同类根因（各处各自猜路径，一处漂移就出事）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

resolve_root() {
    (cd "$REPO_ROOT" && uv run --no-sync python -c \
        "from openinvest.paths import INVEST_ROOT; print(INVEST_ROOT)")
}

ROOT="$(resolve_root)"
BACKUP_DIR="$ROOT/.backups"
# 备份对象：git 完全不追踪、又不可再生的数据。db/*.sqlite-journal 等 WAL 临时
# 文件不带——那是运行时产物，恢复时会自动重建，带了反而可能是半提交状态。
INCLUDE_PATHS=(memory db .env user_profile.json user_profile.json.bak)
EXCLUDE_GLOBS=("*.pyc" "*.sqlite-journal" "*.db-shm" "*.db-wal" "*.lock")

usage() {
    cat >&2 <<'EOF'
Usage: run.sh backup [output_dir]
       run.sh restore <zip_path> [--force]
       run.sh list

backup   打一个 openinvest-backup-<UTC时间戳>.zip，默认存到 $INVEST_HOME/.backups/
restore  从 zip 解回 $INVEST_HOME。默认拒绝覆盖已含真实数据的 memory/portfolio.md
         （对齐 lifecycle_cmds.py 的 _write_v2_portfolio 同款 safety guard）。
         --force 跳过这个检查。恢复前总会先把当前状态备份一份，不管有没有 --force。
list     列出 $INVEST_HOME/.backups/ 下现有的备份
EOF
}

has_real_portfolio_data() {
    # 复用同一份判定口径：cash 任一币种 > 0，或 holdings 非空 → 算真实数据。
    # 用 python 而不是 grep，避免 yaml/markdown 格式变化时 grep 误判。
    (cd "$REPO_ROOT" && uv run --no-sync python -c "
from openinvest.core.memory_store import MemoryStore
store = MemoryStore()
doc = store.read('portfolio')
if doc is None:
    raise SystemExit(1)
cash = doc.get('cash') or {}
holdings = doc.get('holdings') or []
has_real = any(float(v or 0) > 0 for v in cash.values()) or len(holdings) > 0
raise SystemExit(0 if has_real else 1)
" 2>/dev/null)
}

cmd_backup() {
    local out_dir="${1:-$BACKUP_DIR}"
    mkdir -p "$out_dir"
    local ts
    ts="$(date -u +%Y%m%d-%H%M%S)"
    local name="openinvest-backup-${ts}-UTC.zip"
    local path="$out_dir/$name"

    local exclude_args=()
    for g in "${EXCLUDE_GLOBS[@]}"; do
        exclude_args+=(-x "$g")
    done

    (cd "$ROOT" && zip -r -q "$path" "${INCLUDE_PATHS[@]}" "${exclude_args[@]}" 2>/dev/null || true)
    if [ ! -f "$path" ]; then
        echo '{"status":"error","error":"zip 失败，检查 INCLUDE_PATHS 是否存在"}' >&2
        exit 1
    fi
    local size
    size="$(du -h "$path" | cut -f1)"
    echo "{\"status\":\"ok\",\"path\":\"$path\",\"size\":\"$size\"}"
}

cmd_restore() {
    local zip_path="${1:?restore 需要 zip 路径}"
    local force="${2:-}"
    if [ ! -f "$zip_path" ]; then
        echo "{\"status\":\"error\",\"error\":\"$zip_path 不存在\"}" >&2
        exit 1
    fi

    if [ "$force" != "--force" ] && has_real_portfolio_data; then
        echo '{"status":"refused","reason":"当前 memory/portfolio.md 已含真实持仓/现金，拒绝覆盖。确认要恢复请加 --force（会先自动备份当前状态）。"}' >&2
        exit 1
    fi

    echo "先备份当前状态..." >&2
    cmd_backup "$BACKUP_DIR" >&2

    (cd "$ROOT" && unzip -o -q "$zip_path")
    echo "{\"status\":\"ok\",\"restored_from\":\"$zip_path\"}"
}

cmd_list() {
    if [ ! -d "$BACKUP_DIR" ]; then
        echo '{"status":"ok","backups":[]}'
        return
    fi
    local files
    files="$(ls -1t "$BACKUP_DIR"/openinvest-backup-*.zip 2>/dev/null || true)"
    if [ -z "$files" ]; then
        echo '{"status":"ok","backups":[]}'
        return
    fi
    echo "$files" | while IFS= read -r f; do
        du -h "$f" | awk '{printf "%s\t%s\n", $2, $1}'
    done
}

case "${1:-}" in
    backup)
        shift
        cmd_backup "$@"
        ;;
    restore)
        shift
        cmd_restore "$@"
        ;;
    list)
        cmd_list
        ;;
    *)
        usage
        exit 1
        ;;
esac
