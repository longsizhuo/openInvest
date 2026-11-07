"""Agent 待办队列 —— 后端不调模型时，需要推理的活排队交给宿主 Agent（2026-10-18 插件不持 key，P2）。

两条队列，append-only jsonl（memory/.agent_queue/），只追加不改写：
- **news**：event_watch 抓到、还没归一化的新闻。Agent 用 `pending_news` 取，结构化后经
  `ingest_event` 交回（url 入库即出队）；与持仓无关的用 dismiss 掉；48h 没处理自动过期。
- **committees**：事件 / 价格触发的委员会重跑请求。Agent 用 `pending_committees` 取，
  prepare_committee → submit_committee 交卷后（当天记录的 Run at 晚于请求时间）即出队；24h 过期。

Agent 每次来取都会记 last_drain；看门狗据此判断"有待办但没人来取"（见 job_watchdog）。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openinvest.core.memory_store import MemoryStore, _file_lock, append_jsonl
from openinvest.utils.symbols import safe_symbol

NEWS_TTL = timedelta(hours=48)
COMPACT_ABOVE = 5000   # 行数超过它就在入队时压掉过期行
COMMITTEE_TTL = timedelta(hours=24)
LAST_DRAIN_STATE = "agent_queue_last_drain"


def _dir(memory_root=None) -> Path:
    d = (Path(memory_root) if memory_root is not None else MemoryStore().root) / ".agent_queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name: str, memory_root=None) -> List[Dict[str, Any]]:
    root=Path(memory_root) if memory_root is not None else MemoryStore().root
    path = root / ".agent_queue" / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _now() -> datetime:
    return datetime.now().astimezone()


def _age(rec: Dict[str, Any], now: datetime) -> timedelta:
    return now - datetime.fromisoformat(rec["queued_at"])


def mark_drained() -> None:
    MemoryStore().state_set(LAST_DRAIN_STATE, _now().isoformat(timespec="seconds"))


def last_drained() -> Optional[datetime]:
    raw = MemoryStore().state_get(LAST_DRAIN_STATE)
    return datetime.fromisoformat(raw) if raw else None


# ---------- news ----------

def enqueue_news(items: Iterable[Any], *, memory_root=None) -> int:
    """event_watch 用：RawNewsItem 列表入队。已在队里（含已 dismiss）的 url 跳过——没入库的 url
    每轮都会被 event_watch 当成 unseen 再交一遍，不跳过的话队列文件每 30 分钟整批重复增长。"""
    _compact_news(memory_root=memory_root)
    known = {news_snapshot_id(r) for r in _read("news.jsonl",memory_root)}
    now = _now().isoformat(timespec="microseconds")
    from openinvest.services.news_collection import RawCollection
    owner=MemoryStore(Path(memory_root)) if memory_root is not None else MemoryStore()
    database=RawCollection(owner.data_root)
    n = 0
    try:
        for it in items:
            row={"url":it.url,"title":it.title,"snippet":it.snippet,"text":getattr(it,"text",""),
                 "source":it.src_name,"published_at":it.published_at,"fetched_at":it.fetched_at,
                 "raw_meta":getattr(it,"raw_meta",{}),"queued_at":now}
            row=database.register_input(row)
            snapshot=row["source_snapshot_id"]
            if snapshot in known:continue
            known.add(snapshot)
            append_jsonl(_dir(memory_root) / "news.jsonl",row)
            n+=1
    finally:database.db.close()
    return n


def news_content_hash(record):
    from openinvest.core.evidence_eligibility import digest
    return digest({key:record.get(key) or "" for key in ("url","title","snippet","text","published_at")})


def news_snapshot_id(record):
    return record.get("source_snapshot_id") or news_content_hash(record)


def canonical_news_inputs(*,url=None,snapshot_id=None,processing_status=None,include_excluded=False,memory_root=None):
    import sqlite3
    from contextlib import closing
    owner=MemoryStore(Path(memory_root)) if memory_root is not None else MemoryStore()
    path=owner.data_root/"db/source_snapshots.sqlite"
    if not path.exists():return []
    from openinvest.core.evidence_eligibility import Eligibility,source_artifact_ids
    denied=Eligibility(owner.data_root).excluded_ids()
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='raw_input_snapshots'").fetchone():return []
        records=[];clauses=[];values=[]
        for column,value in (("url",url),("id",snapshot_id),("status",processing_status)):
            if value is not None:clauses.append(column+"=?");values.append(value)
        sql="SELECT payload,status FROM raw_input_snapshots"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY rowid"
        for payload,status in connection.execute(sql,values):
            record=json.loads(payload)
            if news_content_hash(record)!=record["content_sha256"]:raise ValueError("immutable_source_snapshot_digest_mismatch")
            excluded=bool(source_artifact_ids({**record.get("raw_meta",{}),"source_snapshot_id":record["source_snapshot_id"]}) & denied)
            if excluded and not include_excluded:
                if snapshot_id is not None:raise ValueError("source_snapshot_revoked")
                continue
            if snapshot_id is not None or url is not None:
                from openinvest.services.news_collection import validate_input_snapshot
                validate_input_snapshot(connection,record)
            records.append({**record,"processing_status":status,"source_excluded":excluded})
        return records


def set_news_status(snapshot_id,status):
    import sqlite3
    path=MemoryStore().data_root/"db/source_snapshots.sqlite"
    if not path.exists():return
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE raw_input_snapshots SET status=? WHERE id=? AND status='pending'",(status,snapshot_id))


def bind_news_source(item,*,memory_root=None):
    """An agent may interpret a fetched item, but cannot edit its source text."""
    item={**item,"url":str(item.get("url") or "").strip()}
    if not item.get("url"):return None
    requested=item.get("source_snapshot_id")
    candidates=canonical_news_inputs(url=item["url"],snapshot_id=requested,memory_root=memory_root)
    if requested:
        row=next((r for r in candidates if news_snapshot_id(r)==requested),None)
        if row is None:raise ValueError("unknown source_snapshot_id")
        return row
    if candidates:
        match=next((r for r in reversed(candidates) if all((r.get(k) or "")== (item.get(k) or "") for k in ("title","snippet","published_at"))),None)
        if match is None:raise ValueError("fetched_source_content_mismatch; reuse pending_news source_snapshot_id")
        return match
    return None


def _compact_news(*,memory_root=None) -> None:
    """news.jsonl 超过 COMPACT_ABOVE 行时，锁内重写只留未过期的行。"""
    path = _dir(memory_root) / "news.jsonl"
    if not path.exists():
        return
    with _file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= COMPACT_ABOVE:
            return
        now = _now()
        keep = [];removed=[]
        for line in lines:
            try:
                if _age(json.loads(line), now) <= NEWS_TTL:
                    keep.append(line)
                else:removed.append(line)
            except (ValueError, KeyError):
                removed.append(line)
        if removed:
            from openinvest.core.evidence_eligibility import digest
            archive=path.parent/"news_archive.jsonl"
            with _file_lock(archive),archive.open("a",encoding="utf-8",newline="\n") as output:
                for line in removed:output.write(json.dumps({"raw_line":line,"sha256":digest(line),"use":"audit_only"},ensure_ascii=False)+"\n")
                output.flush();os.fsync(output.fileno())
        from openinvest.core.memory_store import _atomic_write_text
        _atomic_write_text(path,"".join(f"{line}\n" for line in keep))


def pending_news_items(now: Optional[datetime] = None, *, include_expired=False) -> List[Dict[str, Any]]:
    """未入库、未 dismiss、未过期的新闻（按 url 去重，先进先出）。纯读，不记 last_drain。"""
    rows=canonical_news_inputs(processing_status="pending")
    if not rows:  # 空队列不碰 EventStore：它会顺手建 db/events.db，调度器写锁时还要等 5s（brief 每个会话都调）
        return []
    now = now or _now()
    seen, out = set(), []
    for rec in rows:
        url = rec.get("url")
        snapshot=news_snapshot_id(rec)
        if not url or snapshot in seen:
            continue
        seen.add(snapshot)
        expired=_age(rec,now)>NEWS_TTL
        if expired and not include_expired:
            continue
        out.append({**rec,"source_snapshot_id":snapshot,"status":"expired_unprocessed" if expired else "pending"})
    return out


def legacy_news_items():
    return [{**{key:value for key,value in row.items() if key!="source_snapshot_id"},
             "status":"legacy_verification_required","allowed_use":"audit_only",
             "recovery":"Reacquire the exact source through collection; old queue text is not a new verified capture."}
            for row in _read("news.jsonl") if not row.get("content_sha256")]


def pending_news(limit: int = 30, dismiss_urls: Iterable[str] = (),dismiss_source_ids: Iterable[str] = (),include_expired=False) -> Dict[str, Any]:
    """Agent 取活：先记下要 dismiss 的 url，再返回待处理新闻。"""
    now = _now()
    records=canonical_news_inputs(processing_status="pending")
    by_url={url:{news_snapshot_id(row) for row in records if row.get("url")==url} for url in set(dismiss_urls or ())}
    ambiguous=[url for url,versions in by_url.items() if len(versions)>1]
    for record in records:
        if news_snapshot_id(record) in set(dismiss_source_ids or ()) or (record.get("url") in by_url and record["url"] not in ambiguous):
            append_jsonl(_dir() / "news_dismissed.jsonl",
                         {"url":record["url"],"source_snapshot_id":news_snapshot_id(record), "at": now.isoformat(timespec="seconds")})
            set_news_status(news_snapshot_id(record),"dismissed")
    mark_drained()
    all_items=pending_news_items(now,include_expired=True)
    items=all_items if include_expired else [row for row in all_items if row["status"]!="expired_unprocessed"]
    result={"pending":len(items),"items":items[:max(1,int(limit))],"dismissal_review_required_urls":ambiguous,
            "expired_unprocessed":sum(row["status"]=="expired_unprocessed" for row in all_items),
            "recovery":"include_expired=true exposes preserved versions for current source verification; ingestion never grants learning eligibility"}
    legacy=legacy_news_items()
    result.update(legacy_verification_required=len(legacy),legacy_items=legacy[:max(1,int(limit))])
    result["revoked_source_inputs"]=sum(row["source_excluded"] for row in canonical_news_inputs(processing_status="pending",include_excluded=True))
    return result


def acknowledge_news_source(snapshot_id,event_id,status):
    if not snapshot_id:return
    path=_dir()/"news_completions.jsonl"
    with _file_lock(path):
        if snapshot_id in {r.get("source_snapshot_id") for r in _read("news_completions.jsonl")}:
            set_news_status(snapshot_id,status)
            return
        with path.open("a",encoding="utf-8",newline="\n") as file:
            file.write(json.dumps({"source_snapshot_id":snapshot_id,"event_id":event_id,"status":status,
                                   "recorded_at":_now().isoformat(),"qualification_granted":False})+"\n")
    set_news_status(snapshot_id,status)


# ---------- committees ----------

def enqueue_committee(symbols: List[str], event_ids: List[str], reason: str = "", *, request_key: str | None = None, memory_root=None) -> str:
    """event_trigger 在 agent 模式下用它代替 POST /api/committee/run。返回 "queued:<id>"。"""
    import hashlib
    qid = hashlib.sha256(request_key.encode()).hexdigest()[:24] if request_key else uuid.uuid4().hex[:12]
    root=Path(memory_root) if memory_root is not None else MemoryStore().root
    from openinvest.db.event_store import EventStore
    db_path=(root.parent if root.name=="memory" else root)/"db/events.db"
    versions={}
    if db_path.exists() and event_ids:
        events=EventStore(str(db_path))
        try:
            versions={eid:(events.get_event(eid) or {}).get("_version_id") for eid in event_ids}
        finally:events.conn.close()
    row = {
        "event_versions":versions, "id": qid, "symbols": list(symbols), "event_ids": list(dict.fromkeys(event_ids)),
        "reason": reason, "queued_at": _now().isoformat(timespec="microseconds"),
    }
    path = _dir(root) / "committees.jsonl"
    with _file_lock(path):
        if request_key and any(r.get("id") == qid for r in _read("committees.jsonl",root)):
            return f"queued:{qid}"
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(row, ensure_ascii=False)+"\n")
    return f"queued:{qid}"


def pending_committee_items(now: Optional[datetime] = None, *, include_expired=False, memory_root=None) -> List[Dict[str, Any]]:
    """Every unacknowledged cause survives; polling or a later report is not completion."""
    now=now or _now()
    from openinvest.core.evidence_eligibility import Eligibility
    root=Path(memory_root) if memory_root is not None else MemoryStore().root
    owner=root.parent if root.name=="memory" else root
    denied=Eligibility(owner).excluded_ids()
    completed={(r.get("request_id"),r.get("symbol")) for r in _read("committee_completions.jsonl",memory_root)}
    waiting={(r.get("request_id"),r.get("symbol")):r for r in _read("committee_reviews_pending.jsonl",memory_root)}
    grouped={}
    for record in _read("committees.jsonl",memory_root):
        original_ids=record.get("event_ids",[])
        live_ids=[eid for eid in original_ids if "event:"+eid not in denied]
        if original_ids and not live_ids:
            continue  # A revoked source is not a new financial analysis completion.
        record={**record,"event_ids":live_ids}
        expired=_age(record,now)>COMMITTEE_TTL
        if expired and not include_expired:continue
        for symbol in record.get("symbols") or []:
            if (record.get("id"),symbol) in completed:continue
            group=grouped.setdefault(symbol,{"symbol":symbol,"reason":"","event_ids":[],"request_ids":[],
                                             "queued_at":record["queued_at"],"requests":[],"status":"expired" if expired else "pending"})
            group["requests"].append(record);group["request_ids"].append(record["id"])
            group["queued_at"]=min(group["queued_at"],record["queued_at"])
            group["reason"]="; ".join(filter(None,[group["reason"],record.get("reason","")]))
            group["event_ids"]=list(dict.fromkeys(group["event_ids"]+record.get("event_ids",[])))
            if not expired:group["status"]="pending"
    for group in grouped.values():
        reviews=[waiting.get((request["id"],group["symbol"])) for request in group["requests"]]
        if reviews and all(reviews):
            group["expired"] = group["status"] == "expired"
            if not group["expired"]:
                group["status"]="awaiting_reasoning_review"
            group["review_requests"]=list({row["request_sha256"]:row for row in reviews}.values())
            group["next_step"]="核验已有草稿；不要为了通过而重复生成同一套委员会。若输入变化，请准备覆盖全部原因的新上下文。"
    return sorted(grouped.values(),key=lambda r:r["queued_at"])


def mark_requests_waiting_review(memory_root,bindings,context_id,request_sha256,status):
    for binding in bindings:
        append_jsonl(_dir(memory_root)/"committee_reviews_pending.jsonl",{
            "request_id":binding["request_id"],"symbol":binding["symbol"],"context_id":context_id,
            "request_sha256":request_sha256,"review_status":status,"recorded_at":_now().isoformat(),
            "completion_granted":False})


def bind_context_requests(memory_root,symbol,event_brief,prepared_at):
    versions=getattr(event_brief,"event_versions",{})
    prepared=datetime.fromisoformat(prepared_at)
    bindings=[]
    for group in pending_committee_items(now=prepared,memory_root=memory_root):
        if group["symbol"]!=symbol:continue
        for request in group["requests"]:
            queued=datetime.fromisoformat(request["queued_at"])
            if queued.tzinfo is None or queued>prepared:continue
            required=request.get("event_versions",{})
            if not all(eid in versions and versions[eid]>=(required.get(eid) or 0) for eid in request.get("event_ids",[])):
                continue
            bindings.append({"request_id":request["id"],"symbol":symbol,"queued_at":request["queued_at"],
                             "event_versions":{eid:versions[eid] for eid in request.get("event_ids",[])}})
    return bindings


def acknowledge_context_decision(memory_root,context_id,decision_id):
    from openinvest.core.decision_evidence import current_decision
    from openinvest.core.evidence_eligibility import digest
    root=Path(memory_root)
    context=json.loads((root/".evidence/contexts"/(context_id+".json")).read_text())
    if context.get("sha256")!=digest(context["payload"]):return 0
    payload=context["payload"]
    if not current_decision(root,{"decision_id":decision_id},payload["symbol"],allow_pending_requests=True):return 0
    bindings=payload["inputs"].get("trigger_requests",[])
    path=_dir(root)/"committee_completions.jsonl"
    count=0
    with _file_lock(path):
        known={(r.get("request_id"),r.get("symbol")) for r in _read("committee_completions.jsonl",root)}
        with path.open("a",encoding="utf-8",newline="\n") as file:
            for item in bindings:
                key=(item["request_id"],item["symbol"])
                if key in known:continue
                file.write(json.dumps({**item,"context_id":context_id,"decision_id":decision_id,"completed_at":_now().isoformat()})+"\n")
                known.add(key);count+=1
    return count


def pending_committees() -> Dict[str, Any]:
    mark_drained()
    items = pending_committee_items()
    expired=[r for r in pending_committee_items(include_expired=True) if r["status"]=="expired"]
    return {"pending": len(items), "items": items, "expired":len(expired), "expired_items":expired[:20]}
