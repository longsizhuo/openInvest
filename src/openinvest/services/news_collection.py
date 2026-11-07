"""Collect original source snapshots with explicit per-source coverage.

No model calls, market-direction gates or notification side effects. A budget
limits fetching, not what is allowed to become evidence. Incomplete intervals
remain recorded and resume from the provider cursor; every run checks the head.
"""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import hashlib
import gzip
import json
from pathlib import Path
import time
import sqlite3
import uuid
from urllib.parse import urlencode,urlsplit,quote
from urllib.request import Request,urlopen

from openinvest.core.memory_store import MemoryStore,_atomic_write_text,_file_lock
from openinvest.services.news_sources import RawNewsItem


SOURCES=("wscn:global-channel","wscn:a-stock-channel","wscn:hk-stock-channel","sina:152")
CST=timezone(timedelta(hours=8))


def moment(value):
    result=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if result.tzinfo is None:raise ValueError("source/coverage timestamps require a timezone")
    return result.astimezone(timezone.utc)


def fetch_page(source,cursor):
    if source.startswith("wscn:"):
        params={"channel":source.partition(":")[2],"client":"pc","limit":80}
        if cursor is not None:params["cursor"]=cursor
        url="https://api-one.wallstcn.com/apiv1/content/lives?"+urlencode(params)
    else:
        url="https://zhibo.sina.com.cn/api/zhibo/feed?"+urlencode({"page":cursor or 1,"page_size":50,"zhibo_id":152,"tag_id":0,"dire":"f","dpc":1})
    request=Request(url,headers={"User-Agent":"Mozilla/5.0 openInvest source collector"})
    with urlopen(request,timeout=8) as response:
        if response.status!=200:raise ValueError("source_http_status")
        body=response.read(8*1024*1024+1)
        if len(body)>8*1024*1024:raise ValueError("source_response_exceeds_envelope")
    return url,body


def decode_page(source,body,cursor,*,version=1):
    data=json.loads(body)
    if source.startswith("wscn:"):
        payload=data["data"];raw=payload["items"];next_cursor=payload.get("next_cursor")
    else:
        raw=data["result"]["data"]["feed"]["list"];next_cursor=(cursor or 1)+1
    if not isinstance(raw,list):raise ValueError("source_items_not_a_list")
    items=[]
    for entry in raw:
        if source.startswith("wscn:"):
            published=datetime.fromtimestamp(float(entry["display_time"]),timezone.utc)
            text=entry.get("content_text") or entry.get("title") or ""
            uri=entry.get("uri") or ""
            if uri.startswith("/"):uri="https://wallstreetcn.com"+uri
        else:
            published=datetime.strptime(entry["create_time"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(timezone.utc)
            text=entry.get("rich_text") or ""
            # Use only an actual locator returned by this item. A shared column
            # page must never be invented as a canonical article URL.
            uri=entry.get("url") or ""
        if not text:raise ValueError("source_item_has_no_original_text")
        url=urlsplit(uri)
        verified_url=uri if url.scheme in ("http","https") and url.netloc and not url.username else None
        ident=str(entry.get("id") or hashlib.sha256(json.dumps(entry,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
        item={"source":source,"source_item_id":ident,"title":entry.get("title") or text[:160],
                      "snippet":text,"text":text,"published_at":published.isoformat(),"url":verified_url,
                      "source_locator_status":"provided_by_source" if verified_url else "pending_verification"}
        if version>=2:
            provider_id=entry.get("id") is not None and str(entry["id"]).strip()!=""
            if provider_id:ident=str(entry["id"]);item["source_item_id"]=ident
            locator=verified_url or ("openinvest-source://"+source.replace(":","/")+"/"+quote(ident,safe="") if provider_id else None)
            item.update(item_id_origin="provider" if provider_id else "local_content_hash",
                        publisher_url=verified_url,source_locator=locator,
                        locator_kind="publisher_url" if verified_url else "backend_snapshot" if locator else "unverified")
        items.append(item)
    return items,next_cursor


class RawCollection:
    """Compressed immutable responses and indexed handoffs, outside learned memory."""
    def __init__(self,data_root):
        self.data_root=Path(data_root)
        self.path=Path(data_root)/"db/source_snapshots.sqlite"
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=15)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL").fetchall()
        self.db.executescript("""CREATE TABLE IF NOT EXISTS raw_responses(sha256 TEXT PRIMARY KEY,body_gzip BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_candidates(id TEXT PRIMARY KEY,source TEXT NOT NULL,payload TEXT NOT NULL,
                locator_verified INTEGER NOT NULL,handed_off INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS pending_raw_handoff ON raw_candidates(handed_off,locator_verified);""")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS raw_collection_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_candidate_heads(source_key TEXT PRIMARY KEY,observation_id TEXT NOT NULL,content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_input_snapshots(id TEXT PRIMARY KEY,url TEXT NOT NULL,content_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS raw_input_heads(url TEXT PRIMARY KEY,id TEXT NOT NULL,content_hash TEXT NOT NULL);""")
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_locator_mappings(candidate_id TEXT PRIMARY KEY,mapping TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_observation_aliases(candidate_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS adapter_captures(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backend_observations(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS response_captures(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS raw_input_status ON raw_input_snapshots(status)")
        self.db.execute("CREATE INDEX IF NOT EXISTS raw_input_url ON raw_input_snapshots(url)")
        with self.db:self.db.execute("INSERT OR IGNORE INTO raw_collection_meta VALUES('scope',?)",(uuid.uuid4().hex,))
        self.scope=self.db.execute("SELECT value FROM raw_collection_meta WHERE key='scope'").fetchone()[0]
        self.path.chmod(0o600)

    def snapshot(self,source,url,body,observed,*,requested_at=None):
        ident=hashlib.sha256(body).hexdigest()
        from openinvest.core.evidence_eligibility import digest
        packet={"response_sha256":ident,"source":source,"fetched_from":url,"fetched_at":observed.isoformat(),
                "requested_at":requested_at.isoformat() if requested_at is not None else None,
                "recorded_at":datetime.now(timezone.utc).isoformat()}
        capture=digest({"scope":self.scope,"packet":packet})
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO raw_responses VALUES(?,?)",(ident,gzip.compress(body)))
            self.db.execute("INSERT OR IGNORE INTO response_captures VALUES(?,?,?)",(capture,digest(packet),json.dumps(packet)))
        return {**packet,"response_capture_id":capture,"response_storage":str(self.path)}

    def candidate(self,ident,record):
        from openinvest.services.source_reporting import api_acquisition
        def proved(value):
            return api_acquisition(self.db,{"source":value["source"],"fetched_at":value["fetched_at"],"raw_meta":value["source_receipt"]}) is not None
        key=record["source"]+":"+record["source_item_id"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior=self.db.execute("SELECT * FROM raw_candidate_heads WHERE source_key=?",(key,)).fetchone()
            if prior and prior["content_hash"]==ident:
                previous=json.loads(self.db.execute("SELECT payload FROM raw_candidates WHERE id=?",(prior["observation_id"],)).fetchone()[0])
                if proved(previous)==proved(record):return prior["observation_id"]
            observation=hashlib.sha256((self.scope+":"+(prior["observation_id"] if prior else "")+":"+ident).encode()).hexdigest()
            record={**record,"source_observation_id":observation}
            self.db.execute("INSERT INTO raw_candidates(id,source,payload,locator_verified) VALUES(?,?,?,?)",
                            (observation,record["source"],json.dumps(record,ensure_ascii=False),bool(record.get("source_locator") or record["url"])))
            self.db.execute("INSERT OR REPLACE INTO raw_candidate_heads VALUES(?,?,?)",(key,observation,ident))
            return observation

    def register_input(self,row):
        from openinvest.services.agent_queue import news_content_hash
        from openinvest.services.source_reporting import input_contract
        row={**row,"url":str(row.get("url") or "").strip()}
        content=news_content_hash(row)
        contract=input_contract(self.db,row)
        reserved=urlsplit(row["url"]).scheme.lower()=="openinvest-source"
        status="pending"
        if reserved:
            try:validate_input_snapshot(self.db,row)
            except ValueError:status="reserved_locator_unverified"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior=self.db.execute("SELECT * FROM raw_input_heads WHERE url=?",(row["url"],)).fetchone()
            if prior and prior["content_hash"]==content:
                previous=json.loads(self.db.execute("SELECT payload FROM raw_input_snapshots WHERE id=?",(prior["id"],)).fetchone()[0])
                if input_contract(self.db,previous)==contract:return previous
            repeated=bool(prior and prior["content_hash"]!=content and self.db.execute("SELECT 1 FROM raw_input_snapshots WHERE url=? AND content_hash=?",(row["url"],content)).fetchone())
            ident=hashlib.sha256((self.scope+":"+(prior["id"] if prior else "")+":"+content+":"+contract).encode()).hexdigest()
            value={**row,"source_snapshot_id":ident,"content_sha256":content,"source_regression":repeated,"proof_contract_id":contract}
            self.db.execute("INSERT INTO raw_input_snapshots(id,url,content_hash,payload,status) VALUES(?,?,?,?,?)",(ident,row["url"],content,json.dumps(value,ensure_ascii=False),status))
            self.db.execute("INSERT OR REPLACE INTO raw_input_heads VALUES(?,?,?)",(row["url"],ident,content))
        from openinvest.core.evidence_eligibility import Eligibility,source_artifact_ids
        Eligibility(self.data_root).record_audit_lineage("source_snapshot:"+ident,value,source_artifact_ids(row.get("raw_meta",{})))
        return value

    def pending(self):
        cache={};records={}
        for row in self.db.execute("SELECT id,payload FROM raw_candidates WHERE handed_off=0 AND locator_verified=1").fetchall():
            record=json.loads(row["payload"]);receipt=record["source_receipt"];sha=receipt["response_sha256"]
            key=(sha,record["source"])
            if key not in cache:
                body=gzip.decompress(self.db.execute("SELECT body_gzip FROM raw_responses WHERE sha256=?",(sha,)).fetchone()[0])
                if hashlib.sha256(body).hexdigest()!=sha:raise ValueError("source_snapshot_integrity_failure")
                items,_=decode_page(record["source"],body,None,version=receipt.get("decoder_version",1))
                cache[key]={item["source_item_id"]:item for item in items}
            expected=cache[key].get(record["source_item_id"])
            if expected is None or any(record.get(key)!=value for key,value in expected.items()):
                raise ValueError("candidate_does_not_match_original_source")
            mapping=self.db.execute("SELECT mapping FROM raw_locator_mappings WHERE candidate_id=?",(row["id"],)).fetchone()
            observation=record.get("source_observation_id")
            if not observation:
                observation=hashlib.sha256((self.scope+":legacy_candidate:"+row["id"]).encode()).hexdigest()
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO raw_observation_aliases VALUES(?,?)",(row["id"],observation))
                    observation=self.db.execute("SELECT observation_id FROM raw_observation_aliases WHERE candidate_id=?",(row["id"],)).fetchone()[0]
            records[row["id"]]={**record,"source_observation_id":observation,
                                  "resolved_locator":json.loads(mapping[0]) if mapping else None}
        return records

    def resolve_snapshot_locators(self):
        # Append a derived locator for old captures; never rewrite their original
        # URL/body or pretend that the new locator was returned by the publisher.
        for row in self.db.execute("SELECT id,payload FROM raw_candidates WHERE locator_verified=0 AND handed_off=0").fetchall():
            record=json.loads(row["payload"]);receipt=record["source_receipt"]
            encoded=self.db.execute("SELECT body_gzip FROM raw_responses WHERE sha256=?",(receipt["response_sha256"],)).fetchone()
            if encoded is None:raise ValueError("source_response_missing")
            body=gzip.decompress(encoded[0])
            if hashlib.sha256(body).hexdigest()!=receipt["response_sha256"]:raise ValueError("source_response_corrupt")
            items,_=decode_page(record["source"],body,None,version=2)
            expected=next((item for item in items if item["source_item_id"]==record["source_item_id"]),None)
            if expected and expected.get("source_locator") and expected.get("item_id_origin")=="provider" and all(expected[key]==record[key] for key in ("title","snippet","text","published_at","url")):
                mapping={key:expected[key] for key in ("source_locator","locator_kind","publisher_url","item_id_origin")}
                mapping.update(derived_at=datetime.now(timezone.utc).isoformat(),projection_version=2)
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO raw_locator_mappings VALUES(?,?)",(row["id"],json.dumps(mapping,ensure_ascii=False)))
                    self.db.execute("UPDATE raw_candidates SET locator_verified=1 WHERE id=?",(row["id"],))

    def acknowledged(self,ids):
        with self.db:self.db.executemany("UPDATE raw_candidates SET handed_off=1 WHERE id=?",((ident,) for ident in ids))


def collect(*,store=None,**kwargs):
    store=store or MemoryStore()
    if (store.data_root/"integrity/restore_pending.json").exists():
        return {"status":"blocked","reason":"restore_pending","queued":0}
    raw=RawCollection(store.data_root)
    try:return _collect(store=store,raw=raw,**kwargs)
    finally:raw.db.close()


def inspect_candidates(*,store=None,limit=30):
    store=store or MemoryStore();path=store.data_root/"db/source_snapshots.sqlite"
    if not path.exists():return {"status":"unavailable","items":[]}
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        rows=connection.execute("SELECT id,payload FROM raw_candidates WHERE locator_verified=0 ORDER BY rowid LIMIT ?",(limit,)).fetchall()
    return {"status":"verification_pending","items":[{"source_candidate_id":ident,**json.loads(payload),
             "allowed_use":"audit_and_source_verification_only","recovery":"locate the exact original item; do not substitute a column URL or infer unreported effects"} for ident,payload in rows]}


def source_snapshot(snapshot_id,*,store=None):
    from contextlib import closing
    from openinvest.services.agent_queue import news_content_hash
    store=store or MemoryStore();path=store.data_root/"db/source_snapshots.sqlite"
    if not path.exists():raise ValueError("source_snapshot_unavailable")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
        row=connection.execute("SELECT payload,status FROM raw_input_snapshots WHERE id=?",(snapshot_id,)).fetchone()
        if row is None:raise ValueError("source_snapshot_not_in_this_instance")
        record=json.loads(row[0])
        if news_content_hash(record)!=record["content_sha256"]:raise ValueError("source_snapshot_integrity_failure")
        validation_error=None
        try:validate_input_snapshot(connection,record)
        except ValueError as exc:validation_error=str(exc)
        metadata=record.get("raw_meta",{})
        from openinvest.core.evidence_eligibility import Eligibility,source_artifact_ids
        excluded=bool(source_artifact_ids({**metadata,"source_snapshot_id":snapshot_id}) & Eligibility(store.data_root).excluded_ids())
        return {"source_snapshot_id":snapshot_id,"source_locator":record["url"],
                "locator_kind":metadata.get("locator_kind","publisher_url"),"publisher_url":metadata.get("publisher_url",record["url"]),
                "original_input":record,"processing_status":"revoked" if excluded else row[1],
                "stored_processing_status":row[1],"source_excluded":excluded,
                "allowed_use":"audit_only" if excluded or validation_error else "source_verification_and_normalization",
                "validation_error":validation_error,
                "proof_kind":"captured_api_response" if metadata.get("response_sha256") else "captured_adapter_input",
                "qualification_granted":False,"source_truth_or_model_accuracy_verified":False}


def validate_input_snapshot(connection,record):
    """Recheck a captured response in this instance, never a path from the payload."""
    metadata=record.get("raw_meta") or {}
    sha=metadata.get("response_sha256")
    reserved=urlsplit(str(record.get("url") or "").strip()).scheme.lower()=="openinvest-source"
    if reserved and (not sha or record.get("source") not in SOURCES):
        raise ValueError("backend_source_locator_requires_corresponding_provider_proof")
    if not sha:return  # Immutable received-input snapshot remains the proof boundary for other adapters.
    row=connection.execute("SELECT body_gzip FROM raw_responses WHERE sha256=?",(sha,)).fetchone()
    if row is None:raise ValueError("original_source_response_missing")
    body=gzip.decompress(row[0])
    if hashlib.sha256(body).hexdigest()!=sha:raise ValueError("original_source_response_corrupt")
    items,_=decode_page(record["source"],body,None,version=metadata.get("decoder_version",1))
    keys=("source","title","url","snippet","text","published_at")
    if metadata.get("locator_projection_version")==2 and metadata.get("decoder_version",1)<2:
        items,_=decode_page(record["source"],body,None,version=2)
    if not any(item["source_item_id"]==metadata.get("source_item_id") and all((item.get("source_locator") or item[key] if key=="url" else item[key])==record.get(key) for key in keys) for item in items):
        raise ValueError("input_does_not_match_original_source_response")


def _collect(*,store,raw,since=None,overlap_hours=24,max_pages=20,max_seconds=90,fetcher=None,now=None,clock=None):
    store=store or MemoryStore();now=now or datetime.now(timezone.utc)
    clock=clock or (lambda:datetime.now(timezone.utc))
    if now.tzinfo is None:raise ValueError("collection time must include timezone")
    if overlap_hours<0 or max_pages<2 or max_seconds<=0:raise ValueError("collection requires at least two pages for head and recovery")
    if (store.data_root/"integrity/restore_pending.json").exists():
        return {"status":"blocked","reason":"restore_pending","queued":0}
    root=store.root/".source_collection";root.mkdir(parents=True,exist_ok=True)
    state_file=root/"coverage.json"
    with _file_lock(state_file):
        state=json.loads(state_file.read_text()) if state_file.exists() else {"schema":1,"sources":{}}
        if not state.get("sources") and since is None:
            return {"status":"coverage_unknown","reason":"initial_source_window_required","queued":0}
        initial=moment(since) if since is not None else None
        if initial and initial>now:raise ValueError("coverage starts in the future")
        reports={};candidates={};fetcher=fetcher or fetch_page;start=time.monotonic()
        order=sorted(SOURCES,key=lambda key:state["sources"].get(key,{}).get("attempts",0))
        for source in order:
            previous=state["sources"].get(source,{})
            source_start=time.monotonic();budget=max_seconds/len(SOURCES)
            anchor=moment(previous["coverage_started_at"]) if previous.get("coverage_started_at") else initial
            lower=moment(previous["window_start"]) if previous.get("pending") else (
                moment(previous["complete_through"])-timedelta(hours=overlap_hours) if previous.get("complete_through") else initial)
            if anchor is not None and lower is not None:lower=max(anchor,lower)
            if lower is None:
                reports[source]={"status":"coverage_unknown","reason":"initial_source_window_required"};continue
            end=moment(previous["window_end"]) if previous.get("pending") else now
            cursor=previous.get("next_cursor") if previous.get("pending") else None
            reached=False;pages=0;seen_pages=set();error=None
            # Check new head even while an older uncovered tail is resumed.
            sequence=[None,cursor] if cursor is not None else [None]
            next_cursor=cursor
            try:
                while sequence or next_cursor is not None:
                    if pages>=max_pages or time.monotonic()-source_start>=budget or time.monotonic()-start>=max_seconds:
                        error="fetch_budget_exhausted";break
                    current=sequence.pop(0) if sequence else next_cursor
                    requested_at=clock()
                    url,body=fetcher(source,current)
                    received_at=clock()
                    receipt={**raw.snapshot(source,url,body,received_at,requested_at=requested_at),"decoder_version":2}
                    page_id=receipt["response_sha256"]
                    if page_id in seen_pages:raise ValueError("provider_repeated_page_or_cursor")
                    seen_pages.add(page_id);pages+=1
                    items,following=decode_page(source,body,current,version=2)
                    if not items:raise ValueError("empty_page_does_not_certify_requested_window")
                    for item in items:
                        if not lower<=moment(item["published_at"])<=now:continue
                        ident=hashlib.sha256((source+":"+item["source_item_id"]+":"+json.dumps(item,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
                        record={**item,"fetched_at":received_at.isoformat(),"source_receipt":receipt}
                        candidates[ident]=record
                        raw.candidate(ident,record)
                    oldest=min(moment(item["published_at"]) for item in items)
                    # A resumed tail must still be read; a fresh head is not
                    # evidence that a previously missing interval was covered.
                    is_head_only=cursor is not None and current is None
                    if oldest<=lower and not is_head_only:
                        reached=True;next_cursor=None;break
                    if not is_head_only:
                        if following is None or following==current:raise ValueError("source_history_not_covered")
                        next_cursor=following
            except (OSError,ValueError,KeyError,TypeError) as exc:
                error=type(exc).__name__+":"+str(exc)[:100]
            current={**previous,"attempts":previous.get("attempts",0)+1,"observed_at":now.isoformat(),
                     "coverage_started_at":(anchor or lower).isoformat(),
                     "window_start":lower.isoformat(),"window_end":end.isoformat(),"next_cursor":next_cursor,
                     "pending":not reached,"pages":pages,"error":error,
                     "coverage_kind":"observed_provider_pages_with_bounded_publication_overlap"}
            if reached:
                current["complete_through"]=end.isoformat()
            state["sources"][source]=current
            reports[source]={**current,"status":"complete" if reached and end==now else "catchup_required" if reached else "coverage_incomplete"}
        _atomic_write_text(state_file,json.dumps(state,ensure_ascii=False,sort_keys=True))
    from openinvest.services.agent_queue import enqueue_news
    # Coverage checkpoints attest capture, not handoff. Replay every durable
    # candidate after a queue outage, even if its source window already advanced.
    with _file_lock(root/"handoff"):
        raw.resolve_snapshot_locators()
        saved=raw.pending()
        eligible=list(saved.values())
        items=[RawNewsItem(src_name=r["source"],title=r["title"],url=(r.get("resolved_locator") or {}).get("source_locator") or r.get("source_locator") or r["url"],snippet=r["snippet"],text=r["text"],
                           published_at=r["published_at"],fetched_at=r["fetched_at"],raw_meta={**r["source_receipt"],"source_item_id":r["source_item_id"],"source_origin":"captured_api_response",
                           "locator_kind":(r.get("resolved_locator") or r).get("locator_kind","publisher_url"),"publisher_url":r.get("publisher_url",r["url"]),
                           "source_observation_id":r["source_observation_id"],
                           "locator_projection_version":2 if r.get("resolved_locator") else r["source_receipt"].get("decoder_version",1)}) for r in eligible]
        queued=enqueue_news(items,memory_root=store.root)
        raw.acknowledged(saved)
    pending_verification=raw.db.execute("SELECT COUNT(*) FROM raw_candidates WHERE locator_verified=0").fetchone()[0]
    unfinished=raw.db.execute("SELECT COUNT(*) FROM raw_input_snapshots WHERE status='pending'").fetchone()[0]
    complete=all(row.get("status")=="complete" for row in reports.values()) and len(reports)==len(SOURCES)
    result={"status":"ok" if complete else "coverage_incomplete","queued":queued,"sources":reports,
            "candidate_count":len(candidates),"pending_verification":pending_verification,
            "unfinished_source_inputs":unfinished,
            "raw_snapshot_database":str(raw.path),"model_calls":0,
            "quiet_window_certified":complete and not candidates and pending_verification==0 and unfinished==0,
            "limitation":"Late source insertions outside the configured publication overlap remain unverified; this is not proof of all market news."}
    _atomic_write_text(root/"latest.json",json.dumps(result,ensure_ascii=False))
    return result
