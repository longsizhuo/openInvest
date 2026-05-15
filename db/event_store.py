"""db/event_store.py —— 事件层 SQLite WAL + sqlite-vec 存储

职责：
- 维护 db/events.db，独立于 trades_db / market_store / memory/
- 表 events / sources / events_vec 三张
- 事件归一化后 upsert：同一 event_id（claim hash）多源 merge 到 sources 表
- recall(symbol) 给 committee_runner 召回：SQL hard filter → 向量精排 → 时效排序
- mark_committee_task(event_id, task_id) 给 trigger 路径打链接

schema：
  events:
    id                INTEGER PRIMARY KEY AUTOINCREMENT      （= events_vec.rowid）
    event_id          TEXT UNIQUE NOT NULL                    （claim 文本 sha256[:16]）
    one_line_claim    TEXT NOT NULL
    event_type        TEXT                                   earnings/macro/policy/...
    stance            TEXT NOT NULL                          risk/opportunity/neutral
    severity          INTEGER NOT NULL                        1=low 2=mid 3=high
    source_reliability TEXT                                  low/mid/high
    ts                TEXT NOT NULL                           ISO 8601 事件发生时间
    entities_json     TEXT                                    JSON list 实体 / macro tag
    affected_symbols_json TEXT NOT NULL                       JSON list yfinance symbols
    created_at        TEXT NOT NULL                           入库 ISO timestamp
    committee_task_id TEXT                                    trigger 触发的 committee task

  sources(id, event_id FK, src_name, url, title, snippet, fetched_at)
  events_vec USING vec0(embedding float[N])                   N 维由 caller 传入决定
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "events.db")

_SEVERITY_INT = {"low": 1, "mid": 2, "high": 3}
_SEVERITY_STR = {v: k for k, v in _SEVERITY_INT.items()}
_VALID_STANCE = {"risk", "opportunity", "neutral"}


def claim_to_event_id(claim: str) -> str:
    """对一句话事实声明做归一化 hash，给同一事件多源去重。

    步骤：
    1. lower-case
    2. 剥掉日期片段（2026-05-13 / Q1 2026 等容易因报道时差不一致）
    3. 多空白压成单空格
    4. sha256[:16]
    """
    s = (claim or "").lower()
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", s)
    s = re.sub(r"\bq[1-4]\s*20\d{2}\b", "", s)
    s = re.sub(r"\b20\d{2}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _serialize_vec(embedding: List[float]) -> bytes:
    """sqlite-vec float[N] binary format = packed little-endian float32 array"""
    return struct.pack(f"{len(embedding)}f", *embedding)


class EventStore:
    """事件存储 —— 跨线程 + 跨进程并发安全

    用法：
        store = EventStore(embedding_dim=1024)
        was_new, eid = store.upsert_event(event_dict, embedding=[...])
        store.add_source(eid, src_dict)
        events = store.recall("NDQ.AX", query_embedding=[...])
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        embedding_dim: int = 1024,
    ) -> None:
        path = db_path or DB_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.embedding_dim = embedding_dim

        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row

        # sqlite-vec 是动态扩展，需要先 enable_load_extension 再 sqlite_vec.load
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._vec_loaded = True
        except Exception as e:
            log.warning(f"sqlite-vec 加载失败，向量精排将降级为纯 SQL: {e}")
            self._vec_loaded = False

        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        self.conn.commit()

        self._lock = threading.RLock()
        self._init_db()

    @property
    def vec_loaded(self) -> bool:
        return self._vec_loaded

    def _init_db(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id              TEXT UNIQUE NOT NULL,
                    one_line_claim        TEXT NOT NULL,
                    event_type            TEXT,
                    stance                TEXT NOT NULL,
                    severity              INTEGER NOT NULL,
                    source_reliability    TEXT,
                    ts                    TEXT NOT NULL,
                    entities_json         TEXT,
                    affected_symbols_json TEXT NOT NULL,
                    created_at            TEXT NOT NULL,
                    committee_task_id     TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id    TEXT NOT NULL,
                    src_name    TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    title       TEXT,
                    snippet     TEXT,
                    fetched_at  TEXT NOT NULL,
                    UNIQUE(event_id, url)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sources_event ON sources(event_id)")

            if self._vec_loaded:
                cur.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS events_vec "
                    f"USING vec0(embedding float[{self.embedding_dim}])"
                )
            self.conn.commit()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def upsert_event(
        self,
        event: Dict[str, Any],
        *,
        embedding: Optional[List[float]] = None,
    ) -> Tuple[bool, str]:
        """归一化后落盘。返回 (was_new, event_id)。

        event 必须含字段：
          one_line_claim, stance, severity (low/mid/high or 1/2/3), ts,
          affected_symbols (List[str])
        可选：event_type, source_reliability, entities (List[str])

        event_id 由 one_line_claim 自动算出；同 event_id 已存在则**只更新
        severity / stance / 元数据**，不重写 ts（保留最早出现时刻）。
        """
        claim = event.get("one_line_claim")
        if not claim:
            raise ValueError("event.one_line_claim 不能为空")

        stance = event.get("stance", "neutral")
        if stance not in _VALID_STANCE:
            raise ValueError(f"stance 必须 ∈ {_VALID_STANCE}，收到 {stance!r}")

        sev = event.get("severity", 1)
        sev_int = _SEVERITY_INT.get(sev, sev) if isinstance(sev, str) else int(sev)
        if sev_int not in (1, 2, 3):
            raise ValueError(f"severity 必须 ∈ low/mid/high，收到 {sev!r}")

        eid = event.get("event_id") or claim_to_event_id(claim)
        affected = event.get("affected_symbols") or []
        entities = event.get("entities") or []
        ts = event.get("ts") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id, severity FROM events WHERE event_id = ?", (eid,))
            row = cur.fetchone()
            was_new = row is None

            if was_new:
                cur.execute("""
                    INSERT INTO events
                        (event_id, one_line_claim, event_type, stance, severity,
                         source_reliability, ts, entities_json,
                         affected_symbols_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    eid, claim, event.get("event_type"), stance, sev_int,
                    event.get("source_reliability"), ts,
                    json.dumps(entities, ensure_ascii=False),
                    json.dumps(affected, ensure_ascii=False),
                    created_at,
                ))
                new_id = cur.lastrowid
                if self._vec_loaded and embedding is not None:
                    if len(embedding) != self.embedding_dim:
                        raise ValueError(
                            f"embedding dim {len(embedding)} != "
                            f"store dim {self.embedding_dim}"
                        )
                    cur.execute(
                        "INSERT INTO events_vec(rowid, embedding) VALUES (?, ?)",
                        (new_id, _serialize_vec(embedding)),
                    )
            else:
                # 已存在：只 bump severity 到 max（事件持续升级时）+ 覆盖 stance
                old_sev = row["severity"]
                cur.execute("""
                    UPDATE events
                       SET severity = ?, stance = ?,
                           affected_symbols_json = ?, entities_json = ?
                     WHERE event_id = ?
                """, (
                    max(old_sev, sev_int),
                    stance,
                    json.dumps(affected, ensure_ascii=False),
                    json.dumps(entities, ensure_ascii=False),
                    eid,
                ))
            self.conn.commit()
            return was_new, eid

    def add_source(
        self,
        event_id: str,
        *,
        src_name: str,
        url: str,
        title: Optional[str] = None,
        snippet: Optional[str] = None,
        fetched_at: Optional[str] = None,
    ) -> bool:
        """挂一条源到事件。同 (event_id, url) 已存在返回 False 不报错。"""
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO sources(event_id, src_name, url, title, snippet, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (event_id, src_name, url, title, snippet, fetched_at))
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_committee_task(self, event_id: str, task_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE events SET committee_task_id = ? WHERE event_id = ?",
                (task_id, event_id),
            )
            self.conn.commit()

    def gc_old(self, *, keep_days: int = 30) -> int:
        """删除 keep_days 前的事件。返回删除条数。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id FROM events WHERE ts < ?", (cutoff,))
            ids = [r["id"] for r in cur.fetchall()]
            if not ids:
                return 0
            qmarks = ",".join(["?"] * len(ids))
            cur.execute(f"DELETE FROM sources WHERE event_id IN "
                        f"(SELECT event_id FROM events WHERE id IN ({qmarks}))", ids)
            cur.execute(f"DELETE FROM events WHERE id IN ({qmarks})", ids)
            if self._vec_loaded:
                cur.execute(f"DELETE FROM events_vec WHERE rowid IN ({qmarks})", ids)
            self.conn.commit()
            return len(ids)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def is_seen_url(self, url: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM sources WHERE url = ? LIMIT 1", (url,))
        return cur.fetchone() is not None

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        return _row_to_event(row) if row else None

    def get_sources(self, event_id: str) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM sources WHERE event_id = ? ORDER BY fetched_at", (event_id,))
        return [dict(r) for r in cur.fetchall()]

    def list_recent(
        self,
        *,
        hours: int = 24,
        min_severity: str = "low",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列最近 N 小时入库的事件（severity 倒序 + 时间倒序）。

        给 GUI Events Tab + agent debug 用。跟 recall() 不一样：不按 symbol 过滤，
        不做向量精排——纯时间序列扫描，看"系统现在感知到啥"。

        Args:
            hours: 时间窗（默认 24h）
            min_severity: low / mid / high
            limit: 最多返回多少条
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        min_sev_int = _SEVERITY_INT.get(min_severity.lower(), 1)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM events
             WHERE ts >= ? AND severity >= ?
             ORDER BY severity DESC, ts DESC
             LIMIT ?
            """,
            (cutoff, min_sev_int, limit),
        )
        return [_row_to_event(r) for r in cur.fetchall()]

    def count_recent(self, *, hours: int = 24) -> Dict[str, int]:
        """汇总最近 N 小时事件数（按 severity 分桶）"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        cur = self.conn.cursor()
        cur.execute(
            "SELECT severity, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY severity",
            (cutoff,),
        )
        buckets = {1: 0, 2: 0, 3: 0}
        for row in cur.fetchall():
            buckets[row["severity"]] = row["n"]
        return {
            "low": buckets[1],
            "mid": buckets[2],
            "high": buckets[3],
            "total": sum(buckets.values()),
        }

    def recall(
        self,
        symbol: str,
        *,
        time_window_days: int = 7,
        min_severity: str = "mid",
        top_k: int = 8,
        query_embedding: Optional[List[float]] = None,
        extra_tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按维度召回 + 向量精排 + 时效冲突标注。

        - hard filter: 时间窗 + min_severity + (symbol 在 affected_symbols 或 entity tag 命中)
        - rerank: 如果 query_embedding 非空且 sqlite-vec 加载成功，按 cosine 距离精排
                 否则就按 ts DESC 返回
        - supersedes: 后处理 —— 同实体的两条事件按时间排序，新事件 .supersedes = 旧 event_id

        返回的每条 event 字典含:
          event_id, one_line_claim, event_type, stance, severity (str),
          source_reliability, ts, entities, affected_symbols,
          sources (List[{src_name, url, title}]),
          supersedes (str | None),
          committee_task_id (str | None),
          distance (float | None)
        """
        min_sev_int = _SEVERITY_INT.get(min_severity, 2)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=time_window_days)).isoformat(timespec="seconds")
        tags = [t.lower() for t in (extra_tags or [])]

        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM events WHERE ts >= ? AND severity >= ? ORDER BY ts DESC LIMIT 200",
            (cutoff, min_sev_int),
        )
        rows = cur.fetchall()

        sym_lower = symbol.lower()
        candidates: List[Dict[str, Any]] = []
        for r in rows:
            affected = [s.lower() for s in json.loads(r["affected_symbols_json"] or "[]")]
            ents = [s.lower() for s in json.loads(r["entities_json"] or "[]")]
            if sym_lower in affected or any(t in ents for t in tags):
                candidates.append(_row_to_event(r))
        if not candidates:
            return []

        # 向量精排（可选）
        if self._vec_loaded and query_embedding is not None and candidates:
            id_to_dist: Dict[int, float] = {}
            ids = [c["_id"] for c in candidates]
            qmarks = ",".join(["?"] * len(ids))
            cur.execute(
                f"SELECT rowid, vec_distance_cosine(embedding, ?) AS dist "
                f"FROM events_vec WHERE rowid IN ({qmarks})",
                (_serialize_vec(query_embedding), *ids),
            )
            for rr in cur.fetchall():
                id_to_dist[rr["rowid"]] = float(rr["dist"])
            for c in candidates:
                c["distance"] = id_to_dist.get(c["_id"])
            candidates.sort(
                key=lambda c: (c["distance"] if c["distance"] is not None else 99.0),
            )

        top = candidates[:top_k]

        # 挂 sources
        for c in top:
            c["sources"] = [
                {"src_name": s["src_name"], "url": s["url"], "title": s["title"]}
                for s in self.get_sources(c["event_id"])
            ]
            c.pop("_id", None)

        # 时效冲突：同 entity 的两条事件按时间排序，新覆盖旧
        top_sorted = sorted(top, key=lambda c: c["ts"])
        for i in range(len(top_sorted) - 1, 0, -1):
            cur_e = top_sorted[i]
            for j in range(i - 1, -1, -1):
                prev_e = top_sorted[j]
                if set(cur_e["entities"]) & set(prev_e["entities"]):
                    cur_e["supersedes"] = prev_e["event_id"]
                    break

        # 最终按时间倒序返回（最新在前），保持 caller 期望
        return sorted(top, key=lambda c: c["ts"], reverse=True)


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "_id": row["id"],
        "event_id": row["event_id"],
        "one_line_claim": row["one_line_claim"],
        "event_type": row["event_type"],
        "stance": row["stance"],
        "severity": _SEVERITY_STR.get(row["severity"], "low"),
        "source_reliability": row["source_reliability"],
        "ts": row["ts"],
        "entities": json.loads(row["entities_json"] or "[]"),
        "affected_symbols": json.loads(row["affected_symbols_json"] or "[]"),
        "committee_task_id": row["committee_task_id"],
        "supersedes": None,
        "distance": None,
    }
