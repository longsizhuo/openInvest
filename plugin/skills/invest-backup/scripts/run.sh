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

# Backups include credentials and holdings. Apply before creating directories,
# archives or invoking either backend; never inherit a permissive caller umask.
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SPEC="${OPENINVEST_SPEC:-openinvest==0.68.4}"  # x-release-please-version

# python 执行器：git clone 形态优先 REPO_ROOT 的 venv（uv run）；plugin 安装形态
# （ClawHub bundle / Claude marketplace 拷贝）REPO_ROOT 是无 pyproject 的 plugin
# cache，uv run 必炸——兜底 uvx 从 PyPI 拉后端跑，与 sibling run.sh 同款（CR 命中：
# 没兜底时数据丢失安全网对全部 PyPI/uvx 自托管用户失效）。
_py() {
    if [ -f "$REPO_ROOT/pyproject.toml" ]; then
        (cd "$REPO_ROOT" && uv run --no-sync python -c "$1")
    else
        uvx --from "$SPEC" python -c "$1"
    fi
}

resolve_root() {
    _py "from openinvest.paths import INVEST_ROOT; print(INVEST_ROOT)"
}

ROOT="$(resolve_root)"
BACKUP_DIR="$ROOT/.backups"
# 备份对象：git 完全不追踪、又不可再生的数据。*.db-shm / *.sqlite-journal 是运行时
# 产物，恢复时自动重建，不带。*.db-wal **要带**：调度器长驻持有 SQLite 连接期间，
# 新成交只躺在 WAL 里、主库文件还没更新——排除它等于丢账（旧版就丢过）。
# 备份前 checkpoint_dbs 先把 WAL 刷进主库（WAL 通常因此为空）；刷不干净时 WAL 随包带上。
INCLUDE_PATHS=(memory db .env user_profile.json user_profile.json.bak)
EXCLUDE_GLOBS=("*.pyc" "*.sqlite-journal" "*.db-shm" "*.lock")

usage() {
    cat >&2 <<'EOF'
Usage: run.sh backup [output_dir]
       run.sh restore <zip_path> [--force]
       run.sh list

backup   打一个 openinvest-backup-<UTC时间戳>.zip，默认存到 $INVEST_HOME/.backups/
         （先 checkpoint WAL，调度器在跑也安全；后端每天 03:30 另有自动备份 auto_*.zip）
restore  从 zip 解回 $INVEST_HOME。默认拒绝覆盖已含真实数据的 memory/portfolio.md
         （对齐 lifecycle_cmds.py 的 _write_v2_portfolio 同款 safety guard）。
         --force 跳过这个检查。恢复前总会先把当前状态备份一份，不管有没有 --force。
list     列出 $INVEST_HOME/.backups/ 下现有的备份（手动 openinvest-backup-* + 自动 auto_*）
EOF
}

has_real_portfolio_data() {
    # 复用同一份判定口径：cash 任一币种 > 0，或 holdings 非空 → 算真实数据。
    # 用 python 而不是 grep，避免 yaml/markdown 格式变化时 grep 误判。
    _py "
from openinvest.core.memory_store import MemoryStore
store = MemoryStore()
doc = store.read('portfolio')
if doc is None:
    raise SystemExit(1)
cash = doc.get('cash') or {}
holdings = doc.get('holdings') or []
has_real = any(float(v or 0) > 0 for v in cash.values()) or len(holdings) > 0
raise SystemExit(0 if has_real else 1)
" 2>/dev/null
}

# 备份前把 db/ 下所有 SQLite 库的 WAL 刷进主库（与后端每日 backup job 同一个函数）——
# 整个 db/ 连同各库的 -wal 都会进包，所以每个库都要刷，不只是账本三件套。
# 失败不拦备份：钉住的旧版 PyPI 后端（SPEC）可能还没有 openinvest.jobs.backup；
# 那时靠上面不再排除 *.db-wal 兜底，WAL 随包带上，数据不丢。输出走 stderr，不污染结果 JSON。
checkpoint_dbs() {
    _py "import json; from openinvest.jobs.backup import checkpoint_dbs; print(json.dumps(checkpoint_dbs()))" >&2 \
        || echo "warning: checkpoint 未执行（后端版本过旧？），*.db-wal 将随备份一并打包" >&2
}

cmd_backup() {
    local out_dir="${1:-$BACKUP_DIR}"
    mkdir -p "$out_dir"
    out_dir="$(cd "$out_dir" && pwd)"
    # Current backends use online SQLite snapshots. Only a genuinely older
    # backend may take the explicitly unverified legacy path below.
    local backup_rc
    if INVEST_BACKUP_OUTPUT_DIR="$out_dir" _py '
import json, os, sys
from pathlib import Path
try:
    from openinvest.jobs.backup import manual_backup
except ImportError:
    raise SystemExit(42)
from openinvest.paths import INVEST_ROOT
result = manual_backup(INVEST_ROOT, Path(os.environ["INVEST_BACKUP_OUTPUT_DIR"]))
print(json.dumps(result.get("checkpoint", {})), file=sys.stderr)
print(json.dumps(result))
'; then
        return 0
    else
        backup_rc=$?
        if [ "$backup_rc" -ne 42 ]; then
            return "$backup_rc"
        fi
    fi
    checkpoint_dbs
    local ts
    ts="$(date -u +%Y%m%d-%H%M%S)"
    local name="openinvest-backup-${ts}-UTC.zip"
    local path="$out_dir/$name"

    local exclude_args=()
    for g in "${EXCLUDE_GLOBS[@]}"; do
        exclude_args+=(-x "$g")
    done

    # Stage privately and publish without replacing an existing backup (including
    # a same-second collision or symlink). Never let zip update an old archive.
    local staging
    staging="$(mktemp -d "$out_dir/.openinvest-backup.XXXXXX")"
    local staged_path="$staging/archive.zip"
    (cd "$ROOT" && zip -r -q "$staged_path" "${INCLUDE_PATHS[@]}" "${exclude_args[@]}" 2>/dev/null || true)
    if [ ! -f "$staged_path" ]; then
        rmdir "$staging"
        echo '{"status":"error","error":"zip 失败，检查 INCLUDE_PATHS 是否存在"}' >&2
        exit 1
    fi
    # Exact exclusive creation rejects files, directories and all symlinks. Do not
    # use ln/mv directory-target semantics or require hardlink-capable filesystems.
    if ! INVEST_BACKUP_STAGED="$staged_path" INVEST_BACKUP_DEST="$path" _py '
import os, shutil
from pathlib import Path
source = Path(os.environ["INVEST_BACKUP_STAGED"])
destination = Path(os.environ["INVEST_BACKUP_DEST"])
with source.open("rb") as incoming:
    with destination.open("xb") as outgoing:
        identity = os.fstat(outgoing.fileno())
        try:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        except BaseException:
            # Remove only the file we created, never a pre-existing/replaced path.
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                destination.unlink()
            raise
'; then
        rm -f "$staged_path"
        rmdir "$staging"
        echo '{"status":"error","error":"backup destination already exists or cannot be created; no existing backup was changed"}' >&2
        return 1
    fi
    rm -f "$staged_path"
    rmdir "$staging"
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

    INVEST_RESTORE_ARCHIVE="$zip_path" _py '
import json, os
from pathlib import Path
from openinvest.paths import INVEST_ROOT
from openinvest.services.restore_guard import restore_archive
print(json.dumps(restore_archive(INVEST_ROOT, Path(os.environ["INVEST_RESTORE_ARCHIVE"]))))
'
}

cmd_list() {
    if [ ! -d "$BACKUP_DIR" ]; then
        echo '{"status":"ok","backups":[]}'
        return
    fi
    local files
    files="$(ls -1t "$BACKUP_DIR"/openinvest-backup-*.zip "$BACKUP_DIR"/auto_*.zip 2>/dev/null || true)"
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
