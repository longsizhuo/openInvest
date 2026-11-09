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
    ingested_by       TEXT                                    喂料 agent 身份（溯源，ADR-026；
                                                              区别于 sources.src_name=新闻来源）

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
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

DB_PATH = str(INVEST_ROOT / "db" / "events.db")

_SEVERITY_INT = {"low": 1, "mid": 2, "high": 3}
_SEVERITY_STR = {v: k for k, v in _SEVERITY_INT.items()}
_VALID_STANCE = {"risk", "opportunity", "neutral"}
_CURRENT_SUPPORT=object()


def _locked_read(method):
    @wraps(method)
    def read(self,*args,**kwargs):
        with self._lock:
            outer_read=not getattr(self,"_reporting_read_depth",0)
            self._reporting_read_depth=getattr(self,"_reporting_read_depth",0)+1
            if outer_read:self._reporting_policy=None
            own_snapshot=not self.conn.in_transaction
            try:
                if own_snapshot:self.conn.execute("BEGIN")
                result=method(self,*args,**kwargs)
                if own_snapshot:self.conn.commit()
                return result
            except Exception:
                if own_snapshot:self.conn.rollback()
                raise
            finally:
                self._reporting_read_depth-=1
                if outer_read:self._reporting_policy=None
    return read


def claim_to_event_id(claim: str) -> str:
    """对一句话事实声明做归一化 hash，给同一事件多源去重。

    步骤：
    1. lower-case
    2. 剥掉**精确日期戳**（2026-05-13 多家媒体报道同一事件 publish 日不同）
    3. 多空白压成单空格
    4. sha256[:16]

    **不剥**：季度 (Q1/Q2/Q3/Q4) 和年份 (2025/2026)。
    Why（2026-05-15 PR #5 Copilot CR 修复）：
    旧版剥光 `\\bq[1-4]\\s*20\\d{2}\\b` 和 `\\b20\\d{2}\\b` →
      "Nvidia Q1 2026 guidance miss" 和 "Nvidia Q2 2025 guidance miss" 算同一 event_id。
    财报季度 / 年度 outlook 是事件本身的语义，必须保留。
    """
    s = (claim or "").lower()
    # 剥精确 ISO 日期（同事件多家媒体 publish 日不同导致噪音）
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", s)
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
        *, readonly: bool = False,
    ) -> None:
        path = db_path or DB_PATH
        if readonly:
            location=Path(path).expanduser().resolve()
            parent=location.parent
            self.data_root=parent.parent if parent.name=="db" else parent
            self.embedding_dim=embedding_dim
            self.conn=sqlite3.connect(location.as_uri()+"?mode=ro",uri=True,check_same_thread=False,timeout=5)
            self.conn.row_factory=sqlite3.Row
            self._vec_loaded=False
            self._lock=threading.RLock()
            return
        parent = Path(path).parent
        self.data_root = parent.parent if parent.name == "db" else parent
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
        cur.execute("PRAGMA journal_mode=WAL").fetchall()
        cur.execute("PRAGMA busy_timeout=5000").fetchall()
        cur.execute("PRAGMA synchronous=NORMAL").fetchall()
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except Exception:
            pass
        self.conn.commit()
        cur.close()

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
                    committee_task_id     TEXT,
                    ingested_by           TEXT
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
            # 在线迁移：既有库（CREATE IF NOT EXISTS 不会改老表）补 ingested_by 列
            # ——照 market_store._migrate_ohlcv_columns 的 PRAGMA 模式，幂等
            existing_cols = {r[1] for r in cur.execute("PRAGMA table_info(events)").fetchall()}
            if "published_at" not in {r[1] for r in cur.execute("PRAGMA table_info(sources)").fetchall()}:
                cur.execute("ALTER TABLE sources ADD COLUMN published_at TEXT")
            for column in ("raw_text","provenance_json"):
                if column not in {r[1] for r in cur.execute("PRAGMA table_info(sources)").fetchall()}:
                    cur.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
            if "ingested_by" not in existing_cols:
                cur.execute("ALTER TABLE events ADD COLUMN ingested_by TEXT")
            if "updated_at" not in existing_cols:
                cur.execute("ALTER TABLE events ADD COLUMN updated_at TEXT")
                cur.execute("UPDATE events SET updated_at=created_at WHERE updated_at IS NULL")
            if "last_revision_at" not in existing_cols:
                cur.execute("ALTER TABLE events ADD COLUMN last_revision_at TEXT")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sources_event ON sources(event_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS event_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload_json TEXT NOT NULL)""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_event_versions_time ON event_versions(event_id,observed_at,id)")
            cur.execute("CREATE TABLE IF NOT EXISTS event_reporting_support(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            cur.execute("""CREATE TABLE IF NOT EXISTS news_observation_conflicts (
                fingerprint TEXT PRIMARY KEY,event_id TEXT NOT NULL,observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'review_required')""")
            cur.execute("""CREATE TABLE IF NOT EXISTS event_vector_meta (
                row_id INTEGER PRIMARY KEY, space_id TEXT NOT NULL, content_sha256 TEXT NOT NULL)""")
            self.conn.commit()
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
        revision: bool = False,
        _commit: bool = True,
        _version: bool = True,
    ) -> Tuple[bool, str]:
        """归一化后落盘。返回 (was_new, event_id)。

        event 必须含字段：
          one_line_claim, stance, severity (low/mid/high or 1/2/3), ts,
          affected_symbols (List[str])
        可选：event_type, source_reliability, entities (List[str]), ingested_by（喂料 agent 身份）

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
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        ts = event.get("ts") or created_at
        if event.get("_backend_observation_id"):
            from openinvest.services.source_reporting import backend_observation_use
            candidate={**event,"event_id":eid,"ts":ts}
            if not backend_observation_use(self.data_root,candidate,event["_backend_observation_id"]):
                raise ValueError("backend_observation_does_not_match_immutable_capture")
            revision=True

        with self._lock:
            cur = self.conn.cursor()
            # 2026-10-08：ts 晚于入库时刻 = 错标（LLM 把预告日期当发生时刻），按入库时刻存；
            # 与 recall 读侧 eff_ts 同一 julianday 口径（存量行由读侧兜底，不做数据迁移）
            if cur.execute("SELECT julianday(?) > julianday(?)", (ts, created_at)).fetchone()[0]:
                ts = created_at
            cur.execute(
                "SELECT * "
                "FROM events WHERE event_id = ?", (eid,),
            )
            row = cur.fetchone()
            was_new = row is None
            if revision and row is not None and _version:
                self._record_version(eid)

            if was_new:
                cur.execute("""
                    INSERT INTO events
                        (event_id, one_line_claim, event_type, stance, severity,
                         source_reliability, ts, entities_json,
                         affected_symbols_json, created_at, ingested_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    eid, claim, event.get("event_type"), stance, sev_int,
                    event.get("source_reliability"), ts,
                    json.dumps(entities, ensure_ascii=False),
                    json.dumps(affected, ensure_ascii=False),
                    created_at,
                    event.get("ingested_by"),
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
                    cur.execute("INSERT OR REPLACE INTO event_vector_meta VALUES(?,?,?)",
                                (new_id, getattr(embedding,"space_id","unqualified"),
                                 getattr(embedding,"content_sha256","unqualified")))
            else:
                # 已存在：merge 字段（不无脑覆盖）
                # - severity bump 到 max（事件持续升级时）
                # - affected_symbols / entities 做 union 合并（PR #5 Copilot CR 修复）
                #   Why: 第一源列了 [NVDA, AAPL, MSFT]，第二源同事件只提 NVDA →
                #   旧版覆盖后丢 AAPL/MSFT → 后续 recall(symbol="AAPL") 召不到
                #   该事件。改为 union 保留所有提及过的关联实体。
                # - stance 覆盖最新（语义上"事件态度"会随报道演化）
                old_sev = row["severity"]
                old_affected = json.loads(row["affected_symbols_json"] or "[]")
                old_entities = json.loads(row["entities_json"] or "[]")

                def _union_preserve_order(old_list, new_list):
                    """union 但保留旧 list 的顺序（appended 新增项排后面）"""
                    seen = set()
                    out = []
                    for item in old_list + new_list:
                        if item not in seen:
                            seen.add(item)
                            out.append(item)
                    return out

                merged_affected = _union_preserve_order(old_affected, affected or [])
                merged_entities = _union_preserve_order(old_entities, entities or [])
                if revision:
                    merged_affected, merged_entities = affected, entities
                    cur.execute("""UPDATE events SET one_line_claim=?,event_type=?,source_reliability=?,ts=?
                        WHERE event_id=?""", (claim,event.get("event_type"),event.get("source_reliability"),ts,eid))
                    if self._vec_loaded and claim != row["one_line_claim"]:
                        cur.execute("DELETE FROM events_vec WHERE rowid=?",(row["id"],))
                        cur.execute("DELETE FROM event_vector_meta WHERE row_id=?",(row["id"],))
                cur.execute("""
                    UPDATE events
                       SET severity = ?, stance = ?,
                           affected_symbols_json = ?, entities_json = ?
                     WHERE event_id = ?
                """, (
                    sev_int if revision else max(old_sev, sev_int),
                    stance,
                    json.dumps(merged_affected, ensure_ascii=False),
                    json.dumps(merged_entities, ensure_ascii=False),
                    eid,
                ))
            cur.execute("UPDATE events SET updated_at=? WHERE event_id=?",(created_at,eid))
            if not was_new:
                cur.execute("UPDATE events SET last_revision_at=? WHERE event_id=?",(created_at,eid))
            if event.get("_backend_observation_id"):
                from openinvest.services.source_reporting import backend_observation_use,event_payload
                from openinvest.core.evidence_eligibility import Eligibility,digest
                current=_row_to_event(cur.execute("SELECT * FROM events WHERE event_id=?",(eid,)).fetchone())
                if not backend_observation_use(self.data_root,current,event["_backend_observation_id"]):
                    raise ValueError("backend_observation_does_not_match_immutable_capture")
                self._bind_reporting_support(eid,{"kind":"backend_observation","observation_id":event["_backend_observation_id"],
                    "event_sha256":digest(event_payload(current))})
                Eligibility(self.data_root).record_audit_lineage("event:"+eid,event_payload(current),{"backend_observation:"+event["_backend_observation_id"]})
            if _version:
                self._record_version(eid, event, was_new=was_new)
            if _commit:
                self.conn.commit()
            return was_new, eid

    def _record_version(self, event_id: str, reported: Optional[Dict[str, Any]] = None, *, was_new: bool = False) -> None:
        """Append the version actually available now, never backdate a vintage."""
        reported = reported or {}
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return
        previous = self.conn.execute("SELECT payload_json FROM event_versions WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,)).fetchone()
        prior = json.loads(previous[0]).get("vintage", {}) if previous else {}
        observed = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        payload = _row_to_event(row)
        payload["sources"] = self.get_sources(event_id,purpose="audit")
        payload["reporting_support"]=self._reporting_support(event_id)
        payload["vintage"] = {
            "event_time": reported.get("ts", prior.get("event_time")),
            "published_at": prior.get("published_at") or reported.get("published_at"),
            "observation_published_at": reported.get("published_at"),
            "first_acquired_at": prior.get("first_acquired_at", observed),
            "revision_published_at": reported.get("revision_published_at"),
            "revision_acquired_at": observed,
            "legacy_history_unknown": prior.get("legacy_history_unknown", not was_new),
        }
        self.conn.execute("INSERT INTO event_versions(event_id,observed_at,payload_json) VALUES(?,?,?)",
                          (event_id, observed, json.dumps(payload, ensure_ascii=False)))

    def add_source(
        self,
        event_id: str,
        *,
        src_name: str,
        url: str,
        title: Optional[str] = None,
        snippet: Optional[str] = None,
        fetched_at: Optional[str] = None,
        published_at: Optional[str] = None,
        raw_text: str = "",
        provenance: Optional[Dict[str, Any]] = None,
        _commit: bool = True,
        _version: bool = True,
    ) -> bool:
        """挂一条源到事件。同 (event_id, url) 已存在返回 False 不报错。"""
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.cursor()
            previous=cur.execute("SELECT * FROM sources WHERE event_id=? AND url=?",(event_id,url)).fetchone()
            if previous:
                if ((previous["title"] or "",previous["snippet"] or "",previous["published_at"],previous["raw_text"] or "") == (title or "",snippet or "",published_at,raw_text)
                        and json.loads(previous["provenance_json"] or "{}")== (provenance or {})):
                    return False
                if _version:
                    self._record_version(event_id)
                cur.execute("UPDATE sources SET src_name=?,title=?,snippet=?,fetched_at=?,published_at=?,raw_text=?,provenance_json=? WHERE id=?",
                            (src_name,title,snippet,fetched_at,published_at,raw_text,json.dumps(provenance or {},ensure_ascii=False),previous["id"]))
            else:
                cur.execute("""
                    INSERT INTO sources(event_id, src_name, url, title, snippet, fetched_at,published_at,raw_text,provenance_json)
                    VALUES (?, ?, ?, ?, ?, ?,?,?,?)
                """, (event_id, src_name, url, title, snippet, fetched_at,published_at,raw_text,json.dumps(provenance or {},ensure_ascii=False)))
            if _version:
                self._record_version(event_id)
            if _commit:
                self.conn.commit()
            return True

    def source_unchanged(self, url: str, title: str, snippet: str = "", *, published_at=None,raw_text="",require_qualified=True) -> bool:
        row=self.conn.execute("SELECT * FROM sources WHERE url=? ORDER BY fetched_at DESC,id DESC LIMIT 1",(url,)).fetchone()
        same=row is not None and (row["title"] or "",row["snippet"] or "",row["published_at"],row["raw_text"] or "") == (title or "",snippet or "",published_at,raw_text)
        if same and require_qualified:
            from openinvest.services.source_reporting import reporting_use
            return reporting_use(self.data_root,dict(row))["allowed"] and self.get_event(row["event_id"]) is not None
        return same

    def normalization_input(self, item):
        """Reuse only an already qualified interpretation of identical observed text.

        A new acquisition remains separately recorded. An unverified legacy
        interpretation is never promoted by copying its generated claim.
        """
        from openinvest.services.news_identity import reported_text,item_text
        from openinvest.services.source_reporting import reporting_use,from_item
        incoming=reporting_use(self.data_root,from_item(item))
        if not incoming['allowed']:
            return {'status':'source_verification_required','event':None}
        row=self.conn.execute('SELECT * FROM sources WHERE url=? ORDER BY fetched_at DESC,id DESC LIMIT 1',
            (item.url,)).fetchone()
        if row is not None and reported_text(dict(row))==item_text(item):
            previous=self.get_event(row['event_id'])
            if previous is not None and reporting_use(self.data_root,dict(row))['allowed']:
                return {'status':'reuse','event':previous}
        return {'status':'normalize','event':None}

    def write_news_observation(self, event: dict, raw_item=None, *, embedding=None, reuse_extraction=False) -> dict:
        """Keep source revisions and distinguish material changes from retries."""
        if raw_item:
            from urllib.parse import urlsplit
            if urlsplit(str(raw_item.url).strip()).scheme.lower()=="openinvest-source":
                from openinvest.services.agent_queue import bind_news_source
                bound=bind_news_source({"url":str(raw_item.url).strip(),"source_snapshot_id":
                    (getattr(raw_item,"raw_meta",{}) or {}).get("source_snapshot_id")},memory_root=self.data_root/"memory")
                if bound is None:raise ValueError("backend_source_locator_requires_a_bound_snapshot")
            from openinvest.core.evidence_eligibility import Eligibility,source_artifact_ids
            if source_artifact_ids(getattr(raw_item,"raw_meta",{}) or {}) & Eligibility(self.data_root).excluded_ids():
                raise ValueError("source_snapshot_revoked")
        with self._lock,self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            value=dict(event)
            def qualify(result):
                record=result.get("event")
                active=self.get_event(record["event_id"]) if record else None
                if active is None and record is not None:
                    return {**result,"event":None,"audit_changed":result["changed"],"changed":False,
                            "audit_only":True,"blocked_event_id":record["event_id"]}
                return {**result,"event":active,"audit_only":False}
            previous_source=self.conn.execute("SELECT * FROM sources WHERE url=? ORDER BY fetched_at DESC,id DESC LIMIT 1",
                                              (raw_item.url,)).fetchone() if raw_item else None
            previous=self.get_event(previous_source["event_id"],purpose="audit") if previous_source else None
            from openinvest.services.news_identity import reported_text,item_text
            same_text=bool(previous_source and raw_item and reported_text(dict(previous_source))==item_text(raw_item))
            if reuse_extraction:
                planned=self.normalization_input(raw_item) if raw_item else {}
                if not same_text or planned.get('status')!='reuse':
                    raise ValueError('normalization_reuse_requires_qualified_identical_input')
                value=dict(planned['event'])
            if previous_source and raw_item:
                def acquired(ts):
                    parsed=datetime.fromisoformat(str(ts).replace("Z","+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("source acquisition timezone unavailable")
                    return parsed
                if acquired(raw_item.fetched_at) < acquired(previous_source["fetched_at"]):
                    return qualify({"event":previous,"inserted":False,"changed":False,"previous":previous,"ignored":"older_source_observation"})
            if raw_item:
                value["published_at"]=getattr(raw_item,"published_at",None)
            # A changed article stays attached to its story when its subjects overlap.
            old_subjects=set(previous.get("affected_symbols",[])+previous.get("entities",[])) if previous else set()
            new_subjects=set(value.get("affected_symbols",[])+value.get("entities",[]))
            related=previous and (same_text or not old_subjects or not new_subjects or old_subjects & new_subjects)
            if related:
                value["event_id"]=previous["event_id"]
            eid=value.get("event_id") or claim_to_event_id(value["one_line_claim"])
            old=self.get_event(eid,purpose="audit")
            if old and not previous_source and not value.get("event_id"):
                def day(ts):
                    try:
                        moment=datetime.fromisoformat(str(ts).replace("Z","+00:00"))
                        return moment.astimezone(timezone.utc).date().isoformat() if moment.tzinfo else str(ts)[:10]
                    except ValueError:return str(ts)[:10]
                if day(old.get("ts")) != day(value.get("ts")):
                    eid=hashlib.sha256((eid+"|"+day(value.get("ts"))).encode()).hexdigest()[:16]
                    old=self.get_event(eid,purpose="audit")
            value["event_id"]=eid
            from openinvest.services.source_reporting import reporting_use,from_item,event_payload,source_fingerprint
            from openinvest.core.evidence_eligibility import digest
            incoming_allowed=bool(raw_item and reporting_use(self.data_root,from_item(raw_item))["allowed"])
            current_allowed=bool(old and self.get_event(eid))
            if old and current_allowed and not incoming_allowed:
                record={"event":value,"source":vars(raw_item) if raw_item else None,
                        "reason":"unverified_source_cannot_replace_verified_current_report"}
                encoded=json.dumps(record,ensure_ascii=False,sort_keys=True,default=str)
                fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO news_observation_conflicts(fingerprint,event_id,observed_at,payload_json) VALUES(?,?,?,?)",
                                  (fingerprint,eid,datetime.now(timezone.utc).isoformat(),encoded))
                return {"event":None,"inserted":False,"changed":False,"audit_only":True,"source_verification_required":True,
                        "blocked_event_id":eid,"previous":old}
            if old and raw_item and not same_text and (getattr(raw_item,"raw_meta",{}) or {}).get("source_regression"):
                record={"event":value,"source":vars(raw_item),"reason":"source_returned_to_previously_seen_content_requires_verification"}
                encoded=json.dumps(record,sort_keys=True,ensure_ascii=False,default=str)
                fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO news_observation_conflicts(fingerprint,event_id,observed_at,payload_json) VALUES(?,?,?,?)",
                                  (fingerprint,eid,datetime.now(timezone.utc).isoformat(),encoded))
                return {"event":None,"inserted":False,"changed":False,"audit_only":True,
                        "blocked_event_id":eid,"source_conflict":True,"previous":old}
            def signature(v):
                return (v.get("one_line_claim"),v.get("stance"),_SEVERITY_INT.get(v.get("severity"),v.get("severity")),v.get("event_type"),
                        sorted(set(v.get("affected_symbols") or [])),sorted(set(v.get("entities") or [])))
            older_different_observation=False
            if old and raw_item and not related and signature(old)!=signature(value):
                def observed_time(value):
                    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
                    if parsed.tzinfo is None:raise ValueError("source acquisition timezone unavailable")
                    return parsed
                known=[observed_time(row["fetched_at"]) for row in self.get_sources(eid,purpose="audit")]
                older_different_observation=bool(known) and observed_time(raw_item.fetched_at)<max(known)
            if old and not related and (older_different_observation or claim_to_event_id(old["one_line_claim"])!=claim_to_event_id(value["one_line_claim"])):
                # A late copy of the ORIGINAL article may hash to a story whose
                # same-source correction retained that ID. It cannot merge its
                # old risk labels into the corrected, different current claim.
                conflict={"event":value,"source":vars(raw_item) if raw_item else None,
                          "reason":"claim_hash_matches_a_different_corrected_story"}
                encoded=json.dumps(conflict,ensure_ascii=False,sort_keys=True,default=str)
                fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO news_observation_conflicts(fingerprint,event_id,observed_at,payload_json) VALUES(?,?,?,?)",
                                  (fingerprint,eid,datetime.now(timezone.utc).isoformat(),encoded))
                return {"event":None,"inserted":False,"changed":False,"audit_changed":True,
                        "audit_only":True,"blocked_event_id":eid,"source_conflict":True,"previous":old}
            changed=old is None or signature(old)!=signature(value)
            source_changed=raw_item is not None and not self.source_unchanged(raw_item.url,raw_item.title,raw_item.snippet,published_at=getattr(raw_item,"published_at",None),raw_text=getattr(raw_item,"text",""),require_qualified=False)
            if previous_source and raw_item and json.loads(previous_source["provenance_json"] or "{}")!=(getattr(raw_item,"raw_meta",{}) or {}):source_changed=True
            promotion=incoming_allowed and not current_allowed
            if not changed and not source_changed and not promotion:
                return qualify({"event":old,"inserted":False,"changed":False,"previous":old})
            try:
                if old is not None:
                    self._record_version(eid)
                if changed or promotion:
                    inserted,_=self.upsert_event(value,embedding=embedding,revision=bool(related or promotion),_commit=False,_version=False)
                else:
                    inserted=False
                if raw_item:
                    self.add_source(eid,src_name=raw_item.src_name,url=raw_item.url,title=raw_item.title,
                                    snippet=raw_item.snippet,fetched_at=raw_item.fetched_at,published_at=getattr(raw_item,"published_at",None),
                                    raw_text=getattr(raw_item,"text",""),provenance=getattr(raw_item,"raw_meta",{}),_commit=False,_version=False)
                    from openinvest.core.evidence_eligibility import Eligibility,source_artifact_ids
                    parents=source_artifact_ids(getattr(raw_item,"raw_meta",{}) or {})
                    if parents:
                        stored_event=_row_to_event(self.conn.execute("SELECT * FROM events WHERE event_id=?",(eid,)).fetchone())
                        Eligibility(self.data_root).record_audit_lineage("event:"+eid,event_payload(stored_event),parents)
                    if incoming_allowed:
                        current_row=_row_to_event(self.conn.execute("SELECT * FROM events WHERE event_id=?",(eid,)).fetchone())
                        stored_source=dict(self.conn.execute("SELECT * FROM sources WHERE event_id=? AND url=?",(eid,raw_item.url)).fetchone())
                        self._bind_reporting_support(eid,{"kind":"source_report","source_id":source_fingerprint(stored_source),
                            "event_sha256":digest(event_payload(current_row))})
                self._record_version(eid,value,was_new=inserted)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            current=self.get_event(eid,purpose="audit")
            material=inserted or signature(current)!=signature(old or {}) or bool(previous_source and source_changed and not same_text)
            return qualify({"event":current,"inserted":inserted,"changed":material,"source_added":bool(not previous_source and source_changed),"previous":old})

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
            cur.execute("SELECT id FROM events WHERE COALESCE(last_revision_at,ts,created_at) < ?", (cutoff,))
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

    def _reporting_support(self,event_id):
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='event_reporting_support'").fetchone():return None
        row=self.conn.execute("SELECT payload FROM event_reporting_support WHERE event_id=?",(event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _bind_reporting_support(self,event_id,payload):
        self.conn.execute("INSERT OR REPLACE INTO event_reporting_support VALUES(?,?)",(event_id,json.dumps(payload,ensure_ascii=False)))

    def _policy(self):
        from openinvest.services.source_reporting import ReportingPolicy
        if getattr(self,"_reporting_read_depth",0):
            if self._reporting_policy is None:self._reporting_policy=ReportingPolicy(self.data_root)
            return self._reporting_policy
        return ReportingPolicy(self.data_root)

    def _reporting_allowed(self,event,*,sources=None,support=_CURRENT_SUPPORT,as_of=None):
        from openinvest.services.source_reporting import event_reporting_use
        return event_reporting_use(self.data_root,event,
            self.get_sources(event["event_id"],purpose="audit") if sources is None else sources,
            self._reporting_support(event["event_id"]) if support is _CURRENT_SUPPORT else support,as_of=as_of,policy=self._policy())


    def is_seen_url(self, url: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM sources WHERE url = ? LIMIT 1", (url,))
        return cur.fetchone() is not None

    @_locked_read
    def get_event(self, event_id: str, *, purpose: str = "advice") -> Optional[Dict[str, Any]]:
        if purpose != "audit":
            if self._source_excluded(event_id,self._policy().excluded_ids()):
                return None
        cur = self.conn.cursor()
        cur.execute("SELECT *, (SELECT MAX(v.id) FROM event_versions v WHERE v.event_id=events.event_id) AS source_version FROM events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        if row is None:
            return None
        event = _row_to_event(row)
        event["_version_id"] = row["source_version"] or 0
        if purpose!="audit" and not self._reporting_allowed(event):return None
        from openinvest.services.news_identity import analysis_identity
        sources=self.get_sources(event_id,purpose='audit' if purpose=='audit' else 'advice')
        event['_analysis_cause_id'],event['_analysis_version_id']=analysis_identity(event,sources)
        return event

    def _source_excluded(self,event_id,excluded,*,sources=None):
        from openinvest.core.evidence_eligibility import source_artifact_ids
        from openinvest.services.source_reporting import metadata
        if "event:"+event_id in excluded:return True
        for source in self.get_sources(event_id,purpose="audit") if sources is None else sources:
            if source_artifact_ids(metadata(source)) & excluded:return True
        return False

    @_locked_read
    def get_sources(self, event_id: str, *, purpose="advice") -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM sources WHERE event_id = ? ORDER BY fetched_at", (event_id,))
        rows=[dict(r) for r in cur.fetchall()]
        if purpose=="audit":return rows
        if "event:"+event_id in self._policy().excluded_ids():return []
        from openinvest.services.source_reporting import reporting_use
        allowed=[]
        for source in rows:
            state=reporting_use(self.data_root,source,policy=self._policy())
            if state["allowed"]:allowed.append({**source,"reporting_evidence":state})
        return allowed

    @_locked_read
    def get_event_with_sources(self,event_id,*,policy=None):
        if policy is not None:
            from openinvest.services.source_reporting import ReportingPolicy
            if not isinstance(policy,ReportingPolicy) or policy.root.resolve()!=self.data_root.resolve():
                raise ValueError('reporting policy belongs to another data root')
            self._reporting_policy=policy
        event=self.get_event(event_id)
        return {**event,"sources":self.get_sources(event_id)} if event is not None else None

    @_locked_read
    def list_recent(
        self,
        *,
        hours: int = 24,
        min_severity: str = "low",
        limit: int = 50,
        purpose: str = "advice",
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
             WHERE COALESCE(updated_at,created_at) >= ? AND severity >= ?
             ORDER BY severity DESC, ts DESC
            """,
            (cutoff, min_sev_int),
        )
        from openinvest.core.evidence_eligibility import Eligibility
        rows=cur.fetchall()
        if purpose=="audit":return [_row_to_event(r) for r in rows][:limit]
        excluded = self._policy().excluded_ids()
        return [_row_to_event(r) for r in rows if not self._source_excluded(r["event_id"],excluded)
                and self._reporting_allowed(_row_to_event(r))][:limit]

    def count_recent(self, *, hours: int = 24) -> Dict[str, int]:
        """汇总最近 N 小时事件数（按 severity 分桶）"""
        buckets = {1: 0, 2: 0, 3: 0}
        for event in self.list_recent(hours=hours, limit=2**31-1):
            severity = event.get("severity", 1)
            severity = _SEVERITY_INT.get(severity, severity)
            buckets[severity] += 1
        return {
            "low": buckets[1],
            "mid": buckets[2],
            "high": buckets[3],
            "total": sum(buckets.values()),
        }

    @_locked_read
    def source_reporting_summary(self,*,hours=168):
        from openinvest.services.source_reporting import reporting_use,event_payload,source_fingerprint,metadata
        from openinvest.core.evidence_eligibility import digest,source_artifact_ids
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        rows=self.conn.execute("""SELECT e.* FROM events e WHERE julianday(COALESCE(last_revision_at,updated_at,created_at))>=julianday(?)""",(cutoff,)).fetchall()
        current_unqualified=0;source_reasons={};unqualified_sources=0;verified_sources=0
        incidents=[]
        for row in rows:
            event=_row_to_event(row)
            sources=self.get_sources(event["event_id"],purpose="audit")
            if not self._reporting_allowed(event):
                current_unqualified+=1
                incidents.append({"event":event_payload(event),"support":self._reporting_support(event["event_id"]),
                    "sources":sorted((source_fingerprint(item),sorted(source_artifact_ids(metadata(item)))) for item in sources)})
            for source in sources:
                state=reporting_use(self.data_root,source,policy=self._policy())
                if state["allowed"]:verified_sources+=1
                else:
                    unqualified_sources+=1
                    source_reasons[state["reason"]]=source_reasons.get(state["reason"],0)+1
        return {"schema":1,"window_hours":hours,"current_unqualified_events":current_unqualified,
                "incident_sha256":digest(sorted(incidents,key=lambda item:item["event"]["event_id"])),
                "unqualified_source_versions":unqualified_sources,"verified_source_versions":verified_sources,
                "unqualified_reasons":source_reasons,"unknown_is_not_no_news":True,
                "truth_or_learning_granted":False}

    @_locked_read
    def list_current_with_sources(self, *, hours=24, limit=100):
        """Qualified current claims and their original sources in one snapshot."""
        records=[]
        for entry in self.list_recent(hours=hours,limit=limit):
            current=self.get_event(entry["event_id"])
            if current is not None:
                records.append({**current,"sources":self.get_sources(entry["event_id"]),
                                "claim_kind":"normalized_summary_not_verbatim_source",
                                "labels_kind":"assessed_relevance_not_a_trade_instruction"})
        return records

    @_locked_read
    def recall(
        self,
        symbol: str,
        *,
        time_window_days: int = 7,
        min_severity: str = "mid",
        top_k: int = 8,
        query_embedding: Optional[List[float]] = None,
        extra_tags: Optional[List[str]] = None,
        aliases: Optional[List[str]] = None,
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """按维度召回 + 向量精排 + 时效冲突标注。

        - hard filter: 时间窗 + min_severity + (symbol/aliases 在 affected_symbols 或 entity tag 命中)，
          全在 SQL 里先于 LIMIT 200 做（只在本标的合格集里取最新 200 条进精排）
        - 未来 ts（ts 晚于 created_at）或解析不了的 ts 按 created_at 算：窗口、排序、返回的 ts 字段都用它
        - aliases: 代理匹配集合（issue #26，services/symbol_map.proxy_symbols_for——
          持 NDQ.AX 也命中标 ^NDX 的指数事件）；None → 仅 symbol 本身
        - as_of（issue #196，回测 as-of-D 零前视）：只召回 created_at <= as_of 的事件
          + 只挂 fetched_at <= as_of 的源，时间窗改以 as_of 为锚。口径定 created_at
          （系统自写的 UTC 入库时刻 = 硬边界），不用 ts（LLM 标注的发生时刻，可错标 /
          迟入库）。截断在 SQL 硬过滤里做 → 向量精排只在合格集内排。
          必须带时区（naive 口径不明直接 ValueError）。None = 现行为（锚 now、不截断）。
          ⚠️ 只截"事件存不存在"：同 event_id 后续 upsert 原地改写的 severity / stance /
          affected_symbols 没有历史版本，as_of 还原不了。
        - rerank: 如果 query_embedding 非空且 sqlite-vec 加载成功，按 cosine 距离精排
                 否则就按 eff_ts 的真实时刻（julianday，非字符串）倒序取 top_k（生产默认 hash provider 下调用方传 None，
                 见 services/embeddings.embed_query）
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
        from openinvest.core.evidence_eligibility import Eligibility
        exclusions = Eligibility(self.data_root).excluded_ids()
        excluded_events = [key.removeprefix("event:") for key in exclusions if key.startswith("event:")]
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of 必须带时区（naive datetime 口径不明，可能前视）")
        anchor = as_of.astimezone(timezone.utc) if as_of is not None else datetime.now(timezone.utc)
        if as_of is not None:
            # Latest-row timestamps cannot reconstruct past stance/severity/source
            # revisions. Only immutable observed versions qualify for as-of reads.
            return self._recall_versions(symbol, anchor, time_window_days, min_sev_int,
                                         top_k, aliases or [], extra_tags or [], exclusions)
        cutoff = (anchor - timedelta(days=time_window_days)).isoformat(timespec="seconds")
        # created_at 全库统一 isoformat(timespec="seconds") 的 +00:00 串 → 同格式字典序比较精确
        as_of_iso = anchor.isoformat(timespec="seconds") if as_of is not None else None
        tags = sorted({t.lower() for t in (extra_tags or [])})
        match_syms = sorted({symbol.lower()} | {str(a).lower() for a in (aliases or [])})

        # 2026-10-08：symbol/alias/tag 匹配下推进 SQL、先于 LIMIT。旧版先取全库最新 200 条
        # 再在 Python 里按 symbol 过滤——7 天窗 ~1000 条时 200 条只覆盖 ~2 天，持仓标的自己的
        # 事件被别的标的挤掉（生产快照：某持仓标的合格 8 条召回 0）。匹配口径同旧版：大小写不敏感的精确相等。
        # ponytail: SQLite lower() 只折 ASCII；ticker / macro tag 全 ASCII，有非 ASCII tag 再注册 py lower
        match = ("EXISTS (SELECT 1 FROM json_each(affected_symbols_json) WHERE lower(value) IN "
                 f"({','.join('?' * len(match_syms))}))")
        if tags:
            match += (" OR EXISTS (SELECT 1 FROM json_each(entities_json) WHERE lower(value) IN "
                      f"({','.join('?' * len(tags))}))")
        # 未来 ts（LLM 把预告日期当发生时刻，存量最远到 12 月）按入库时刻算：
        # created_at >= cutoff 让它入库满窗口即出窗；eff_ts 让它按入库时刻排序/展示，不钉在榜首。
        # julianday 比的是真实时刻（ts 混着 +08:00 / Z / naive，字符串比不准）——取 top_k、
        # supersedes、返回顺序全按 eff_jd 排（2026-10-08：曾按 eff_ts 串排，+08:00 行虚高 8h 挤掉更新的 UTC 行）
        # INDEXED BY：ORDER BY 不再是裸 ts 后，无 stat 的规划器会改走 severity 索引扫大半张表（实测 4x 慢）
        # ts 解析不了（julianday 为 NULL）同样落到 created_at → eff_jd 永不为 NULL，下面 Python 排序不会撞 None
        eff = "COALESCE(last_revision_at,CASE WHEN julianday(ts)<=julianday(created_at) THEN ts ELSE created_at END)"
        shown = "CASE WHEN julianday(ts)<=julianday(COALESCE(last_revision_at,created_at)) THEN ts ELSE COALESCE(last_revision_at,created_at) END"
        sql = (f"SELECT *, {eff} AS eff_ts, {shown} AS shown_ts, julianday({eff}) AS eff_jd, "
               "(SELECT MAX(v.id) FROM event_versions v WHERE v.event_id=events.event_id) AS source_version "
               "FROM events INDEXED BY idx_events_ts "
               # price_alert = 用户自设价格线命中（jobs/price_sentinel），是提醒不是新闻：不管 rag_min_severity 调成多低，
               # 都不进召回（委员会论据 / 日报 / 持仓摘要）。IS NOT 对 event_type 为 NULL 的行也成立
               f"WHERE julianday({eff}) >= julianday(?) AND severity >= ? AND event_type IS NOT 'price_alert' "
               f"AND ({match})")
        params = [cutoff, min_sev_int, *match_syms, *tags]
        sql += " AND event_id NOT IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(excluded_events))
        if as_of_iso is not None:
            sql += " AND created_at <= ?"
            params.append(as_of_iso)
        cur = self.conn.cursor()
        cur.execute(sql + " ORDER BY eff_jd DESC", params)
        candidates=[]
        for row in cur:
            event=_row_to_event(row)
            if self._source_excluded(row["event_id"],exclusions) or not self._reporting_allowed(event):continue
            candidates.append({**event,"ts":row["shown_ts"],"_jd":row["eff_jd"],"_version_id":row["source_version"] or 0})
            if len(candidates)>=200:break
        if not candidates:
            return []

        # 向量精排（可选）
        space = getattr(query_embedding, "space_id", "")
        compatible = False
        if self._vec_loaded and query_embedding is not None and space.startswith("openai:") and len(query_embedding) == self.embedding_dim:
            metadata = {r["row_id"]: r for r in cur.execute("SELECT * FROM event_vector_meta WHERE row_id IN (SELECT value FROM json_each(?))",
                                                        (json.dumps([c["_id"] for c in candidates]),)).fetchall()}
            compatible = all(c["_id"] in metadata and metadata[c["_id"]]["space_id"] == space
                             and metadata[c["_id"]]["content_sha256"] == hashlib.sha256(c["one_line_claim"].strip().encode()).hexdigest()
                             for c in candidates)
        if compatible:
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
                s
                for s in self.get_sources(c["event_id"])
                # as_of：D 之后才挂上的源（brief 会渲染源名）同样是前视
                if as_of_iso is None or s["fetched_at"] <= as_of_iso
            ]
            c.pop("_id", None)

        # 时效冲突：同 entity 的两条事件按时间排序，新覆盖旧
        top_sorted = sorted(top, key=lambda c: c["_jd"])
        for c in top_sorted:
            del c["_jd"]
        for i in range(len(top_sorted) - 1, 0, -1):
            cur_e = top_sorted[i]
            for j in range(i - 1, -1, -1):
                prev_e = top_sorted[j]
                if set(cur_e["entities"]) & set(prev_e["entities"]):
                    cur_e["supersedes"] = prev_e["event_id"]
                    break

        # 最终按时间倒序返回（最新在前），保持 caller 期望
        return top_sorted[::-1]

    def _recall_versions(self, symbol, anchor, days, min_severity, top_k, aliases, tags, exclusions):
        cutoff = anchor - timedelta(days=days)
        symbols = {str(s).lower() for s in [symbol, *aliases]}
        wanted_tags = {str(t).lower() for t in tags}
        rows = self.conn.execute("""SELECT payload_json FROM (
            SELECT payload_json, ROW_NUMBER() OVER
              (PARTITION BY event_id ORDER BY observed_at DESC,id DESC) AS rn
            FROM event_versions WHERE observed_at<=?
        ) WHERE rn=1""", (anchor.astimezone(timezone.utc).isoformat(timespec="microseconds"),)).fetchall()
        out = []
        for row in rows:
            event = json.loads(row[0])
            if self._source_excluded(event["event_id"],exclusions,sources=event.get("sources",[])):
                continue
            if not self._reporting_allowed(event,sources=event.get("sources",[]),support=event.get("reporting_support"),as_of=anchor):continue
            from openinvest.services.source_reporting import reporting_use
            qualified=[]
            for source in event.get("sources",[]):
                state=reporting_use(self.data_root,source,as_of=anchor,policy=self._policy())
                if state["allowed"]:qualified.append({**source,"reporting_evidence":state})
            event["sources"]=qualified
            seen = datetime.fromisoformat(event["vintage"]["revision_acquired_at"])
            relevance=event.get("material_revision_at") or event.get("ts")
            try:
                relevant_at=datetime.fromisoformat(str(relevance).replace("Z","+00:00"))
                if relevant_at.tzinfo is None:relevant_at=relevant_at.replace(tzinfo=timezone.utc)
                relevant_at=min(relevant_at,seen)
            except (TypeError,ValueError):relevant_at=seen
            if seen < cutoff or relevant_at < cutoff or event.get("event_type") == "price_alert":
                continue
            if _SEVERITY_INT.get(event.get("severity"), 0) < min_severity:
                continue
            if not (symbols & {s.lower() for s in event.get("affected_symbols", [])}
                    or wanted_tags & {s.lower() for s in event.get("entities", [])}):
                continue
            event.pop("_id", None)
            out.append(event)
        # Current embeddings cannot rank a historical version of the text.
        out.sort(key=lambda e: e["vintage"]["revision_acquired_at"], reverse=True)
        return out[:top_k]


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
        "revision_observed_at": row["updated_at"] if "updated_at" in row.keys() else row["created_at"],
        "material_revision_at": row["last_revision_at"] if "last_revision_at" in row.keys() else None,
        "entities": json.loads(row["entities_json"] or "[]"),
        "affected_symbols": json.loads(row["affected_symbols_json"] or "[]"),
        "committee_task_id": row["committee_task_id"],
        "ingested_by": row["ingested_by"] if "ingested_by" in row.keys() else None,
        "supersedes": None,
        "distance": None,
    }
