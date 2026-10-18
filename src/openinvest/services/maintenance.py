"""运维清理：memory/ 里的 `*.bak.<时间戳>` 保留策略 + 历史遗留的 `*.lock` sidecar（评审路线 D7）

背景（2026-10 生产 memory/ 实测）：247 份 `portfolio.md.bak.*`、158 个 `*.lock`。
- `.bak.*` 的写入点（全部是"覆盖前留一份"，没有任何清理）：
    skill_cmds/lifecycle_cmds.py:_write_v2_portfolio  每次 `init`——**含被"已有真实持仓"守卫拒绝的那次**，
                                                      所以反复跑 init / 测试打到真实目录就一次一份
    scripts/migrate_profile.py                        `<name>.md.bak.<ts>`
    scripts/migrate_portfolio_to_holdings.py          `portfolio.md.bak.<ts>`
  生产这 247 份里 107 份内容逐字相同（同一份真实持仓被反复备份）。自动备份 job（jobs/backup.py）排除 *.bak.*，
  所以它们不在任何轮转里。
- `*.lock` 是 `memory_store._file_lock` 的 sidecar，旧版用后不删；新版用后即删，这里只清历史遗留。

默认 **dry-run**（只数不删）；`--apply` 才真删。保留规则（宁多留）：
  每个 family（`.bak.` 前的部分，如 portfolio.md）最新 `keep` 份，**或** mtime 在 `days` 天内的——两者取并集，
  其余才删。`*.lock` 只删当前没人持有的（非阻塞 flock 探测 + 持锁期间 unlink + inode 核对，
  与 `_file_lock` 同一套协议，不会和正在跑的进程抢锁）。
  "无人持有"的 flock 探测只对 flock 型锁成立，所以只认 `_file_lock` sidecar 的形状：0 字节，且去掉 `.lock`
  后是数据文件名（后缀 .md/.json/.jsonl/.yml/.yaml）。别的工具放进 memory/ 的"文件存在即上锁"型锁
  （如 git 的 `index.lock`、`HEAD.lock`）形状不符，不碰。
"""
from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BAK_KEEP = 20
BAK_DAYS = 7
_BAK_RE = re.compile(r"^(?P<family>.+)\.bak\.\d{8}-\d{6}$")
_SIDECAR_DATA_SUFFIXES = {".md", ".json", ".jsonl", ".yml", ".yaml"}


def _is_sidecar(p: Path) -> bool:
    """`_file_lock` 的 sidecar：`<data>.<ext>.lock`，从不写内容（0 字节）。"""
    try:
        return Path(p.stem).suffix in _SIDECAR_DATA_SUFFIXES and p.stat().st_size == 0
    except OSError:
        return False


def plan_bak(root: Path, keep: int = BAK_KEEP, days: int = BAK_DAYS,
             now: Optional[float] = None) -> Tuple[List[Path], List[Path]]:
    """memory/ 顶层（所有写入点都写 store.root 下）的 `*.bak.<ts>` → (保留, 待删)。纯读。"""
    now = time.time() if now is None else now
    families: Dict[str, List[Path]] = {}
    for p in root.iterdir() if root.is_dir() else []:
        m = _BAK_RE.match(p.name)
        if m and p.is_file() and not p.is_symlink():
            families.setdefault(m["family"], []).append(p)
    kept: List[Path] = []
    doomed: List[Path] = []
    for files in families.values():
        files.sort(key=lambda p: p.name, reverse=True)  # 时间戳定宽 → 字典序 = 时间序，新→旧
        for i, p in enumerate(files):
            fresh = now - p.stat().st_mtime < days * 86400
            (kept if i < keep or fresh else doomed).append(p)
    return kept, doomed


def _walk_locks(root: Path) -> List[Path]:
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # .backtest* 体量大（占整份备份 ~89%）且不含 sidecar，别遍历
        dirnames[:] = [d for d in dirnames if not d.startswith(".backtest")]
        out += [q for q in (Path(dirpath) / f for f in filenames if f.endswith(".lock")) if _is_sidecar(q)]
    return out


def _remove_if_unheld(lock: Path, apply: bool) -> bool:
    """True = 当前无人持有（apply 时已删除）。持有中 / 已不存在 → False。"""
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # 正被持有 = 在用
        if apply:
            try:
                if os.fstat(fd).st_ino == os.stat(lock).st_ino:
                    os.unlink(lock)
            except FileNotFoundError:
                pass
        return True
    finally:
        os.close(fd)  # 关 fd 才释放 flock


def cleanup(root: Path, *, apply: bool = False, keep: int = BAK_KEEP, days: int = BAK_DAYS,
            now: Optional[float] = None) -> Dict[str, Any]:
    """清理 `root`（memory/）。root 不存在 → 全 0，不创建目录。"""
    kept, doomed = plan_bak(root, keep, days, now)
    size = sum(p.stat().st_size for p in doomed)
    if apply:
        for p in doomed:
            p.unlink(missing_ok=True)
    locks = _walk_locks(root) if root.is_dir() else []
    stale = [lk for lk in locks if _remove_if_unheld(lk, apply)]
    verb = "deleted" if apply else "to_delete"
    return {
        "status": "ok",
        "mode": "apply" if apply else "dry-run",
        "memory_root": str(root),
        "bak": {
            "policy": f"每个 family 最新 {keep} 份，或 mtime 在 {days} 天内（取并集保留）",
            "found": len(kept) + len(doomed), "kept": len(kept), verb: len(doomed), "bytes": size,
            "sample": sorted(p.name for p in doomed)[:5],
        },
        "lock": {"found": len(locks), "in_use": len(locks) - len(stale), verb: len(stale)},
        "hint": None if apply else "dry-run 未删除任何文件；确认数量后加 --apply 执行",
    }
