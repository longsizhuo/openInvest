"""db/trades_db.py — 一键记账 SQLite WAL 数据库

职责：
- 维护 db/trades.db，独立于 market_store.py 和 memory/ 目录
- WAL + busy_timeout 保证多进程并发安全（scheduler / web_api / CLI 同时写不饿死）
- 不连真实支付渠道，不动 portfolio.md，不动 fcntl.flock

schema：
  trades(id, ts, verdict_id, symbol, direction, units, price, cost_currency, note, status,
         intended_date)
  - ts:            记录意向的 UTC 时间戳（自动生成，ISO 8601）
  - intended_date: 计划成交日期（用户填写，ISO 日期 YYYY-MM-DD，可空）
                   None 表示"记录时就打算立即执行"
  status 值域：planned → executed → cancelled
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.paths import INVEST_ROOT

# trades.db 存放在 db/ 目录下，与 market_data.db 同级
DB_PATH = str(INVEST_ROOT / "db" / "trades.db")


class TradesDB:
    """线程安全 + 多进程并发安全的记账库

    跨线程：check_same_thread=False + RLock
    跨进程：journal_mode=WAL + busy_timeout=5000
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        # 允许测试注入临时路径，默认用 db/trades.db
        path = db_path or DB_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(
            path,
            check_same_thread=False,
            timeout=5.0,
        )
        self.conn.row_factory = sqlite3.Row  # 让 fetchall 返回 dict-like 对象

        cur = self.conn.cursor()
        # WAL 模式：reader/writer 不互相 block；NORMAL 是 WAL 下推荐的 sync 级别
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        self.conn.commit()

        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """建表 + 索引 + schema 迁移（全部幂等）"""
        with self._lock:
            cur = self.conn.cursor()
            # 建表（含最新 schema，IF NOT EXISTS 保证新库直接建全字段）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT    NOT NULL,
                    verdict_id     TEXT,
                    symbol         TEXT    NOT NULL,
                    direction      TEXT    NOT NULL,
                    units          REAL    NOT NULL,
                    price          REAL,
                    cost_currency  TEXT    DEFAULT 'CNY',
                    note           TEXT,
                    status         TEXT    DEFAULT 'planned',
                    intended_date  TEXT
                )
            """)
            # ---- 在线迁移：旧库补 intended_date 列 ----
            # ALTER TABLE ADD COLUMN 对已有列会抛 OperationalError，用 try/except 兜底
            try:
                cur.execute("ALTER TABLE trades ADD COLUMN intended_date TEXT")
            except Exception:
                # 列已存在（OperationalError: duplicate column name）时静默跳过
                pass

            # 按时间倒序查最近 N 笔是最常见操作
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC)"
            )
            # 按标的过滤
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)"
            )
            self.conn.commit()

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        direction: str,
        units: float,
        price: Optional[float] = None,
        cost_currency: str = "CNY",
        verdict_id: Optional[str] = None,
        note: Optional[str] = None,
        ts: Optional[str] = None,
        intended_date: Optional[str] = None,
    ) -> int:
        """插入一笔计划交易，返回新行 id。

        Args:
            symbol:        标的代码（如 NDQ.AX / GC=F）
            direction:     BUY 或 SELL（大写）
            units:         数量（股数 / 克数）
            price:         每单位价格，None 表示市价
            cost_currency: 计价货币，默认 CNY
            verdict_id:    关联决议 decision_id（"<date>/<symbol>"，如 "2026-07-03/GC=F"；
                           历史 transcript 路径写法仍被 decision_ledger 兼容匹配）（可选）
            note:          备注（可选）
            ts:            记录意向时刻（ISO 8601 时间戳，默认 UTC now）
            intended_date: 计划成交日期（ISO 日期 YYYY-MM-DD，可空）
                           None 表示"现在记录、现在打算执行"；
                           填具体日期表示计划在该日成交（金融审计用）
        Returns:
            新插入行的 id（int）
        """
        # 方向标准化：只接受 BUY / SELL
        direction = direction.upper()
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"direction 必须是 BUY 或 SELL，收到 {direction!r}")

        now = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO trades
                       (ts, verdict_id, symbol, direction, units, price,
                        cost_currency, note, status, intended_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)""",
                (now, verdict_id, symbol, direction, units, price,
                 cost_currency, note, intended_date),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def patch_status(self, trade_id: int, status: str) -> bool:
        """改状态（planned → executed / cancelled）。

        Returns:
            True 如果找到并修改了，False 如果 id 不存在
        """
        if status not in ("planned", "executed", "cancelled"):
            raise ValueError(
                f"status 必须是 planned / executed / cancelled，收到 {status!r}"
            )
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE trades SET status = ? WHERE id = ?",
                (status, trade_id),
            )
            self.conn.commit()
            return cur.rowcount > 0  # rowcount==0 → 该 id 不存在

    def claim_status_transition(self, trade_id: int, to_status: str) -> bool:
        """原子状态跃迁 compare-and-set：仅当当前状态 != to_status 时改写。

        Returns:
            True  → 本次调用赢得了真实跃迁（rowcount==1），调用方独占后续副作用
                    （如 portfolio 同步）。
            False → 状态已是 to_status（并发/重放的另一方已抢到）或 id 不存在，
                    调用方应幂等返回，不得重复入账。

        防 #109 并发重复入账：旧的 get_trade()→sync→patch_status() 三步在 check 与
        set 之间无锁，两个并发 PATCH executed 都读到 planned 各同步一次。本方法把
        判定+写下沉为单条 SQL 原子 CAS，只有 rowcount==1 的请求才允许同步。
        """
        if to_status not in ("planned", "executed", "cancelled"):
            raise ValueError(
                f"status 必须是 planned / executed / cancelled，收到 {to_status!r}"
            )
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE trades SET status = ? WHERE id = ? AND status != ?",
                (to_status, trade_id, to_status),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def release_claim(self, trade_id: int, from_status: str, to_status: str) -> bool:
        """释放一个 claim_status_transition 抢到的状态（同步失败后回退用）。

        与 patch_status 的关键区别：本方法只在当前状态**仍是 from_status**（即
        本请求自己刚 claim 到的状态）时才改写；如果状态已经被别的并发请求改动
        （例如 A claim 了 executed 但 sync 失败要回退时，B 已经把它 PATCH 成
        cancelled），本方法不会覆盖 B 的写入，直接返回 False 让调用方感知冲突,
        而不是像无条件 UPDATE 那样静默把 B 的结果踩掉。

        Returns:
            True  → 成功释放（回退到 to_status，行还处于本请求的 from_status）
            False → 状态已被并发改动，未做任何写入；调用方不应假设回退生效
        """
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE trades SET status = ? WHERE id = ? AND status = ?",
                (to_status, trade_id, from_status),
            )
            self.conn.commit()
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def list_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        """按 ts 倒序返回最近 N 笔（转成普通 dict 方便序列化）"""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        # sqlite3.Row 支持 dict() 转换
        return [dict(r) for r in rows]

    def get_trade(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """按 id 查单笔，不存在返回 None"""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
            row = cur.fetchone()
        return dict(row) if row else None
