"""每日自动备份（评审路线 Sprint 2 D2）——账本与记忆打成 zip，备份前先把 WAL 刷进主库。

备份此前只有手动路径（plugin/skills/invest-backup），没人跑就没有；.backups/ 最近一次
2026-07-23。本 job 每天 03:30 Asia/Shanghai（避开 02:00 verdict_review 与 10:00 日报）
把下面这些打进 `INVEST_ROOT/.backups/auto_<UTC YYYYMMDD_HHMM>.zip`，只留最近 14 份
`auto_*`（手动备份 openinvest-backup-* / manual_* 不动）：

  - memory/ 整个目录（含 .committee / .dreams / .state；排除 *.lock、*.bak.*、
    原子写残留 *.tmp.*）
  - db/trades.db（账本）、db/insights.db、db/jobs.sqlite（job_runs 历史）

**为什么先 checkpoint**：WAL 库的新成交先落 `trades.db-wal`，主库文件要等 checkpoint 才更新。
`TradesDB` 只在打开连接时 checkpoint（db/trades_db.py #104），调度器长驻持有连接期间不会再做——
只拷主库文件的备份会丢 WAL 里的成交。这里对三个库各开一条短连接做 checkpoint(TRUNCATE)
（db/wal.py，跨连接/跨进程有效）。万一被读事务挡住没刷完（busy=1），残留的 `-wal` 随包带上。

**为什么不备 market_data.db（574MB）/ events.db（140MB）**：行情与新闻缓存，丢了能重拉，
每天 14 份只是占盘；`.env` 也不进自动备份（凭据别每天复制 14 份，手动备份仍会带）。

失败（zip 写不出 / checkpoint 报错 / 文件读不了）一律抛异常——runner 记 job_runs failed，
job_watchdog 据此告警。checkpoint 报错时 zip 仍会写完（有备份总比没有强）再抛。
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

from openinvest.db.wal import checkpoint_file
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

KEEP_AUTO = 14
DB_FILES = ("db/trades.db", "db/insights.db", "db/jobs.sqlite")
EXCLUDE_GLOBS = ("*.lock", "*.bak.*", "*.tmp.*")


def checkpoint_dbs(root: Optional[Path] = None) -> Dict[str, Any]:
    """三个库各 checkpoint(TRUNCATE) 一次。库不存在 → None；报错 → {"error": ...}，不抛。

    手动备份路径（invest-backup run.sh）也调这个。
    """
    root = Path(root) if root is not None else INVEST_ROOT
    out: Dict[str, Any] = {}
    for rel in DB_FILES:
        try:
            out[rel] = checkpoint_file(root / rel)
        except sqlite3.Error as e:
            out[rel] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _files_to_back_up(root: Path) -> Iterator[Tuple[Path, str]]:
    """(磁盘路径, zip 内路径)。zip 内路径相对 root，与 invest-backup `restore`（unzip 回 root）同构。"""
    for d, _dirs, names in os.walk(root / "memory"):
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
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """checkpoint → 打包 → 轮转。root/now 仅测试注入；调度器裸调 run()。"""
    root = Path(root) if root is not None else INVEST_ROOT
    ckpt = checkpoint_dbs(root)
    errors = {rel: r["error"] for rel, r in ckpt.items() if r and "error" in r}
    for rel, r in ckpt.items():
        if r and r.get("busy"):
            log.warning("[backup] %s checkpoint 被读事务挡住（busy），-wal 将随包带上", rel)

    files = list(_files_to_back_up(root))
    if not files:
        return {"status": "skipped", "reason": "memory/ 与 db/ 下没有可备份的文件"}

    out_dir = root / ".backups"
    out_dir.mkdir(exist_ok=True)
    for stale in out_dir.glob("auto_*.zip.tmp"):  # 上次崩在写一半留下的
        stale.unlink(missing_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")
    dest = out_dir / f"auto_{stamp}.zip"
    tmp = dest.with_name(dest.name + ".tmp")  # 写完再 rename：半截 zip 不会被当成一份备份
    n = 0
    # ponytail: checkpoint→读文件之间是毫秒窗口，trades.db 只有几十 KB；写入量大了改用
    #   sqlite3.Connection.backup()（scripts/snapshot.py 同款）出一致快照再打包。
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            for src, arc in files:
                try:
                    zf.write(src, arc)
                    n += 1
                except FileNotFoundError:  # 遍历后被删（别的进程的原子写/清理），不算失败
                    log.info("[backup] 跳过已消失的文件 %s", arc)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)

    pruned = _prune(out_dir, keep)
    result = {
        "status": "ok",
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "files": n,
        "checkpoint": ckpt,
        "pruned": pruned,
    }
    log.info("[backup] %s %d files %d bytes，轮转删除 %d 份", dest.name, n, result["size_bytes"], len(pruned))
    if errors:
        raise RuntimeError(f"checkpoint 失败 {errors}；备份已写出 {dest.name}（数据库文件按现状打包）")
    return result
