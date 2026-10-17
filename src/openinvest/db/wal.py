"""SQLite WAL checkpoint 单一实现（备份前把 WAL 里已提交的写刷进主库文件）。

为什么需要：WAL 库的新提交先落 `<db>-wal`，主库文件要等 checkpoint 才更新。只拷主库文件
的备份会丢掉 WAL 里还没 checkpoint 的成交；`TradesDB` 只在打开连接时 checkpoint（#104），
调度器长驻持有连接期间不会再做。TRUNCATE 模式：刷完把 WAL 截成 0 字节。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Optional, Union


def checkpoint(conn: sqlite3.Connection) -> Dict[str, int]:
    """在已有连接上 checkpoint(TRUNCATE)。busy=1 表示被读事务挡住、没刷完（WAL 里还有货）。"""
    busy, wal_pages, moved = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()[0]
    return {"busy": busy, "wal_pages": wal_pages, "checkpointed": moved}


def checkpoint_file(path: Union[str, Path], timeout: float = 10.0) -> Optional[Dict[str, int]]:
    """开一条短连接 checkpoint 指定库；文件不存在返回 None（不凭空建空库）。

    跨连接/跨进程有效：WAL 是库级共享的，持有长连接的调度器/web 进程不影响这里刷。
    非 WAL 库（如 jobs.sqlite 的回滚日志模式）上是 no-op，busy=0。
    """
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        return checkpoint(conn)
    finally:
        conn.close()
