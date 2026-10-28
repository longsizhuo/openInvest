"""每日自动备份（评审路线 Sprint 2 D2）——账本与记忆打成 zip，备份前先把 WAL 刷进主库。

备份此前只有手动路径（plugin/skills/invest-backup），没人跑就没有；.backups/ 最近一次
2026-07-23。本 job 每天 03:30 Asia/Shanghai（避开 02:00 verdict_review 与 10:00 日报）
把下面这些打进 `INVEST_ROOT/.backups/auto_<UTC YYYYMMDD_HHMM>.zip`，只留最近 14 份
`auto_*`（手动备份 openinvest-backup-* / manual_* 不动）：

  - memory/ 整个目录（含 .committee / .dreams / .state；排除 *.lock、*.bak.*、
    原子写残留 *.tmp.*）。周中不含 memory/.backtest* 目录，周日带上（见下）
  - db/trades.db（账本）、db/insights.db、db/jobs.sqlite（job_runs 历史）

**为什么 .backtest* 只在周日带**：它占整份备份的约 89%（2026-10 实测：全量 271MB，不含
.backtest* 22MB），每天一份 ×14 不值；但里面混着 2026-07-24 起的前瞻纸面舰队（
verdict_review.FLEET_START），那部分重跑不出来，不能不备。折中：Asia/Shanghai 的周日那次
（03:30 触发时是周日凌晨）带上 .backtest*，文件名加 `_full` 后缀
`auto_<stamp>_full.zip`，与平日 zip 共用同一个 14 份轮转（14 天里自然留 2 份带舰队的）。
文件名里的 stamp 仍是 UTC，所以周日凌晨 03:30 的 full 文件名日期是周六。

**为什么先 checkpoint**：WAL 库的新成交先落 `trades.db-wal`，主库文件要等 checkpoint 才更新。
`TradesDB` 只在打开连接时 checkpoint（db/trades_db.py #104），调度器长驻持有连接期间不会再做——
只拷主库文件的备份会丢 WAL 里的成交。这里对 db/ 下全部 SQLite 库各开一条短连接做 checkpoint(TRUNCATE)
（db/wal.py，跨连接/跨进程有效；进包的只有下面三个，其余库是为了手动备份整包带的 `-wal` 也刷过）。
万一被读事务挡住没刷完（busy=1），残留的 `-wal` 随包带上。

**为什么不备 market_data.db（574MB）/ events.db（140MB）**：行情与新闻缓存，丢了能重拉，
每天 14 份只是占盘；`.env` 也不进自动备份（凭据别每天复制 14 份，手动备份仍会带）。

失败（zip 写不出 / checkpoint 报错 / 文件读不了）一律抛异常——runner 记 job_runs failed；backup.yml 标了
`alert_on_failure: true`，job_watchdog 据此告警（判据 f；默认不标的 job 失败只落库，不报警）。checkpoint 报错时 zip 仍会写完（有备份总比没有强）再抛。
"""
from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
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
DB_FILES = ("db/trades.db", "db/insights.db", "db/jobs.sqlite")
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
    for rel in DB_FILES:
        p = root / rel
        if p.is_file():
            yield p, rel
            wal = p.with_name(p.name + "-wal")
            if wal.is_file() and wal.stat().st_size > 0:  # checkpoint 没刷干净时 WAL 里还有货
                yield wal, rel + "-wal"


def _prune(out_dir: Path, keep: int) -> List[str]:
    autos = sorted(out_dir.glob("auto_*.zip"))  # 文件名含 UTC 时间戳，字典序 = 时间序
    old = autos[: max(len(autos) - keep, 0)]
    for p in old:
        p.unlink(missing_ok=True)
    return [p.name for p in old]


def run(*, root: Optional[Path] = None, keep: int = KEEP_AUTO,
        now: Optional[datetime] = None,
        include_backtest: Optional[bool] = None) -> Dict[str, Any]:
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
    errors = {rel: r["error"] for rel, r in ckpt.items() if rel in DB_FILES and r and "error" in r}
    for rel, r in ckpt.items():
        if r and "error" in r and rel not in errors:
            log.warning("[backup] %s checkpoint 失败（不在备份范围，忽略）: %s", rel, r["error"])
        elif r and r.get("busy"):
            log.warning("[backup] %s checkpoint 被读事务挡住（busy）%s", rel,
                        "，-wal 将随包带上" if rel in DB_FILES else "")

    files = list(_files_to_back_up(root, include_backtest))
    if not files:
        return {"status": "skipped", "reason": "memory/ 与 db/ 下没有可备份的文件"}

    out_dir = root / ".backups"
    out_dir.mkdir(exist_ok=True)
    for stale in out_dir.glob("auto_*.zip.tmp"):  # 上次崩在写一半留下的
        stale.unlink(missing_ok=True)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d_%H%M")
    dest = out_dir / f"auto_{stamp}{'_full' if include_backtest else ''}.zip"
    tmp = dest.with_name(dest.name + ".tmp")  # 写完再 rename：半截 zip 不会被当成一份备份
    n = 0
    # ponytail: checkpoint→读文件之间是毫秒窗口，trades.db 只有几十 KB；写入量大了改用
    #   sqlite3.Connection.backup()（scripts/snapshot.py 同款）出一致快照再打包。
    #   jobs.sqlite 同理：它是回滚日志模式（非 WAL），checkpoint 对它是 no-op，读文件时若恰有别的
    #   job 在 runner._record_run 提交，可能拷到写了一半的页；03:30 此刻没有其他 job 在跑
    #   （event_watch / price_sentinel 窗口在 0-2、8-23 点），风险极低，换 Connection.backup() 时一并解决。
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            for src, arc in files:
                try:
                    zf.write(src, arc)
                    n += 1
                except FileNotFoundError:  # 遍历后被删（别的进程的原子写/清理），不算失败
                    log.info("[backup] 跳过已消失的文件 %s", arc)
        oslock.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)

    pruned = _prune(out_dir, keep)
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
