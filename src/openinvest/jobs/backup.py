"""Preserve memory, evidence policy and every application SQLite database.

SQLite online backups include committed WAL state and are quick_check verified.
Market/news observations are included because today's download cannot reproduce
what was actually available in the past. The manifest records per-database
snapshot results; it does not claim a globally atomic cross-database snapshot.
Automatic archives omit credentials; explicit manual full backups may include
those. Raw failed copies are forensic-only and never eligible for auto-restore.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
import tempfile
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from openinvest.db.wal import checkpoint_file
from openinvest.paths import INVEST_ROOT
from openinvest.utils import oslock

log = logging.getLogger(__name__)

KEEP_AUTO = 14
DB_FILES = ("db/trades.db", "db/insights.db", "db/jobs.sqlite", "db/market_data.db", "db/events.db")
EXCLUDE_GLOBS = ("*.lock", "*.bak.*", "*.tmp.*")
SQLITE_GLOBS = ("*.db", "*.sqlite", "*.sqlite3")  # checkpoint 范围：db/ 下所有 SQLite 库
BACKTEST_PREFIX = ".backtest"  # memory/.backtest、.backtest_cl_v1、.backtest_archive_* …
FULL_BACKUP_WEEKDAY = 6  # 周一=0 … 周日=6，按 Asia/Shanghai 的日历日
SH = ZoneInfo("Asia/Shanghai")


def checkpoint_dbs(root: Optional[Path] = None) -> Dict[str, Any]:
    """db/ 下所有 SQLite 库（*.db / *.sqlite / *.sqlite3，外加 DB_FILES 三个）各 checkpoint(TRUNCATE) 一次。

    库不存在 → None；报错 → {"error": ...}，不抛。范围取全部而不只是 DB_FILES：手动备份路径
    （invest-backup run.sh）把整个 db/ 连同各库的 -wal 一起打包，这样随包带的 -wal 都是刷过的，
    不会出现"旧主库 + 新 WAL"的错位。
    """
    root = Path(root) if root is not None else INVEST_ROOT
    rels = {p.relative_to(root).as_posix() for g in SQLITE_GLOBS for p in (root / "db").glob(g)}
    out: Dict[str, Any] = {}
    for rel in sorted(rels | set(DB_FILES)):
        try:
            out[rel] = checkpoint_file(root / rel)
        except sqlite3.Error as e:
            out[rel] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _files_to_back_up(root: Path, include_backtest: bool = False) -> Iterator[Tuple[Path, str]]:
    """(磁盘路径, zip 内路径)。zip 内路径相对 root，与 invest-backup `restore`（unzip 回 root）同构。

    include_backtest=False 时跳过 memory/ 顶层的 .backtest* 目录/文件（整棵不下钻）。
    """
    mem = root / "memory"
    for d, dirs, names in os.walk(mem):
        if not include_backtest and Path(d) == mem:
            dirs[:] = [x for x in dirs if not x.startswith(BACKTEST_PREFIX)]
            names = [x for x in names if not x.startswith(BACKTEST_PREFIX)]
        for n in names:
            if not any(fnmatch.fnmatch(n, g) for g in EXCLUDE_GLOBS):
                p = Path(d) / n
                yield p, p.relative_to(root).as_posix()
    database_paths = set(DB_FILES) | {p.relative_to(root).as_posix() for p in (root / "db").rglob("*") if p.is_file() and p.suffix in (".db", ".sqlite", ".sqlite3") and not p.is_symlink()}
    for rel in sorted(database_paths):
        p = root / rel
        if p.is_file():
            yield p, rel
            wal = p.with_name(p.name + "-wal")
            if wal.is_file() and wal.stat().st_size > 0:  # checkpoint 没刷干净时 WAL 里还有货
                yield wal, rel + "-wal"
    integrity = root / "integrity"
    if integrity.exists():
        for p in integrity.rglob("*"):
            if p.is_file() and not p.is_symlink() and not any(fnmatch.fnmatch(p.name, g) for g in EXCLUDE_GLOBS):
                yield p, p.relative_to(root).as_posix()


def _prune(out_dir: Path, keep: int) -> List[str]:
    autos = sorted(out_dir.glob("auto_*.zip"))  # 文件名含 UTC 时间戳，字典序 = 时间序
    old = autos[: max(len(autos) - keep, 0)]
    for p in old:
        p.unlink(missing_ok=True)
    return [p.name for p in old]


def run(*, root: Optional[Path] = None, keep: int = KEEP_AUTO,
        now: Optional[datetime] = None,
        include_backtest: Optional[bool] = None, manual: bool = False,
        output_dir: Optional[Path] = None) -> Dict[str, Any]:
    """checkpoint → 打包 → 轮转。调度器裸调 run()；root/now/include_backtest 仅测试注入。

    include_backtest=None（默认）= 按 Asia/Shanghai 的星期决定：周日带 .backtest*（文件名加
    `_full`），其余天不带。now 需带时区（默认 UTC 当前时刻）。
    """
    root = Path(root) if root is not None else INVEST_ROOT
    now = now or datetime.now(timezone.utc)
    if include_backtest is None:
        include_backtest = now.astimezone(SH).weekday() == FULL_BACKUP_WEEKDAY
    ckpt = checkpoint_dbs(root)
    # 只有进包的库 checkpoint 报错才算本 job 失败；market_data.db 等缓存库报错只记日志
    errors = {rel: r["error"] for rel, r in ckpt.items() if r and "error" in r}
    for rel, r in ckpt.items():
        if r and "error" in r and rel not in errors:
            log.warning("[backup] %s checkpoint 失败（不在备份范围，忽略）: %s", rel, r["error"])
        elif r and r.get("busy"):
            log.warning("[backup] %s checkpoint 被读事务挡住（busy）%s", rel,
                        "，-wal 将随包带上" if rel in DB_FILES else "")

    files = list(_files_to_back_up(root, include_backtest))
    if manual:
        files += [(root / name, name) for name in (".env", "user_profile.json", "user_profile.json.bak")
                  if (root / name).is_file()]
    if not files:
        return {"status": "skipped", "reason": "memory/ 与 db/ 下没有可备份的文件"}

    out_dir = Path(output_dir) if output_dir is not None else root / ".backups"
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in out_dir.glob("auto_*.zip.tmp"):  # 上次崩在写一半留下的
        stale.unlink(missing_ok=True)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d_%H%M")
    dest = out_dir / (f"openinvest-backup-{stamp}-UTC.zip" if manual else f"auto_{stamp}{'_full' if include_backtest else ''}.zip")
    # Unique owner-only staging file: credentials and holdings must never inherit
    # a permissive umask, nor reuse a predictable existing file/symlink. Keep the
    # final atomic replacement behavior for repeated runs in the same minute.
    staged_archive = tempfile.NamedTemporaryFile(
        mode="w+b", prefix=dest.name + ".", suffix=".tmp", dir=out_dir, delete=False)
    tmp = Path(staged_archive.name)
    database_files = {arc for _, arc in files if Path(arc).suffix in (".db", ".sqlite", ".sqlite3")}
    n = 0
    # ponytail: checkpoint→读文件之间是毫秒窗口，trades.db 只有几十 KB；写入量大了改用
    #   sqlite3.Connection.backup()（scripts/snapshot.py 同款）出一致快照再打包。
    #   jobs.sqlite 同理：它是回滚日志模式（非 WAL），checkpoint 对它是 no-op，读文件时若恰有别的
    #   job 在 runner._record_run 提交，可能拷到写了一半的页；03:30 此刻没有其他 job 在跑
    #   （event_watch / price_sentinel 窗口在 0-2、8-23 点），风险极低，换 Connection.backup() 时一并解决。
    try:
        with tempfile.TemporaryDirectory(prefix="sqlite-backup-", dir=out_dir) as directory:
            verified = {}
            with zipfile.ZipFile(staged_archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
                for src, arc in files:
                    if arc.endswith("-wal") and arc[:-4] in database_files:
                        continue  # the online snapshot already includes committed WAL data
                    try:
                        if arc in database_files:
                            snapshot = Path(directory) / (str(n) + ".sqlite3")
                            reader = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
                            writer = sqlite3.connect(snapshot)
                            try:
                                reader.backup(writer)
                                if writer.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                                    raise sqlite3.DatabaseError("snapshot integrity check failed")
                            finally:
                                writer.close(); reader.close()
                            zf.write(snapshot, arc)
                            verified[arc] = "online_snapshot_verified"
                        else:
                            zf.write(src, arc)
                        n += 1
                    except sqlite3.Error as e:
                        errors[arc] = f"snapshot failed: {e}"
                        verified[arc] = "unverified_raw_forensics_only"
                        zf.write(src, "unverified/" + arc)
                        wal_path = src.with_name(src.name + "-wal")
                        if wal_path.exists():
                            zf.write(wal_path, "unverified/" + arc + "-wal")
                    except FileNotFoundError:
                        log.info("[backup] 跳过已消失的文件 %s", arc)
                zf.writestr("backup_integrity.json", json.dumps({
                    "schema": 1, "captured_at": now.isoformat(), "databases": verified,
                    "consistency": "per_database; financial reconciliation required before restoring writes",
                    "restore_rule": "retain newest exclusions; do not revive historical approvals or executions",
                }, ensure_ascii=False))
        staged_archive.close()
        oslock.replace(tmp, dest)
    finally:
        staged_archive.close()
        tmp.unlink(missing_ok=True)

    pruned = [] if manual else _prune(out_dir, keep)
    result = {
        "status": "ok",
        "path": str(dest),
        "include_backtest": include_backtest,
        "size_bytes": dest.stat().st_size,
        "files": n,
        "checkpoint": ckpt,
        "pruned": pruned,
    }
    log.info("[backup] %s %d files %d bytes，轮转删除 %d 份", dest.name, n, result["size_bytes"], len(pruned))
    if errors:
        raise RuntimeError(f"checkpoint 失败 {errors}；备份已写出 {dest.name}（数据库文件按现状打包）")
    return result


def manual_backup(root: Path, output_dir: Optional[Path] = None) -> Dict[str, Any]:
    return run(root=root, output_dir=output_dir, manual=True, include_backtest=True)
