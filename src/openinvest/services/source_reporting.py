"""Acquisition evidence permits a source report, never truth or learning by itself."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime,timezone
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3

from openinvest.core.evidence_eligibility import Eligibility,EvidenceIntegrityError,digest,source_artifact_ids

COLLECTORS={
    "openinvest.services.news_sources.ddgs_news":"fetch_ddgs_news",
    "openinvest.services.news_sources.searxng_news":"fetch_searxng_news",
    "openinvest.services.news_sources.rss_feed":"fetch_rss",
    "openinvest.services.news_sources.yfinance_news":"fetch_yfinance_news",
    "openinvest.services.news_sources.akshare_news":"fetch_cn_wire",
}
FIELDS=("src_name","url","title","snippet","raw_text","published_at","fetched_at")


class ReportingPolicy(Eligibility):
    """One immutable governance view per read, never a cross-request TTL cache."""
    def __init__(self,root):
        super().__init__(root)
        self.denied=super().excluded_ids()
        try:self.snapshot=super()._state()
        except (OSError,ValueError):self.snapshot=None
    def excluded_ids(self):return self.denied
    def _state(self):
        if self.snapshot is None:raise ValueError("no reviewed source registry")
        return self.snapshot


def source_payload(source):
    return {key:(source.get(key) if key=="published_at" else source.get(key) or "") for key in FIELDS}


def source_fingerprint(source):return digest(source_payload(source))


def from_item(item):
    return {"src_name":item.src_name,"url":str(item.url).strip(),"title":item.title,"snippet":item.snippet,
            "raw_text":getattr(item,"text","") or "","published_at":getattr(item,"published_at",None),"fetched_at":item.fetched_at,
            "provenance_json":json.dumps(getattr(item,"raw_meta",{}) or {},ensure_ascii=False)}


def from_input(record):
    return {"src_name":record["source"],"url":record["url"],"title":record["title"],"snippet":record["snippet"],
            "raw_text":record.get("text","") or "","published_at":record.get("published_at"),"fetched_at":record.get("fetched_at","")}


def metadata(source):
    value=source.get("provenance_json") or {}
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError as exc:raise EvidenceIntegrityError("DATA_UNTRUSTED: invalid source metadata") from exc
    if not isinstance(value,dict):raise EvidenceIntegrityError("DATA_UNTRUSTED: invalid source metadata")
    return value


def _moment(value):
    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if parsed.tzinfo is None:raise ValueError("acquisition timezone unavailable")
    return parsed.astimezone(timezone.utc)


def capture_adapter_items(items,collector,request,*,data_root=None):
    """Called by the first-party fetch dispatcher, before normalization or filtering."""
    module_name=getattr(collector,"__module__","")
    function=COLLECTORS.get(module_name)
    if function is None or getattr(collector,"__name__",None)!=function:return items
    module=importlib.import_module(module_name)
    if (getattr(module,function) is not collector or not getattr(collector,"__code__",None)
            or Path(collector.__code__.co_filename).resolve()!=Path(module.__file__).resolve()):return items
    from openinvest.core.memory_store import MemoryStore
    from openinvest.services.news_collection import RawCollection
    owner=Path(data_root) if data_root is not None else MemoryStore().data_root
    code_sha=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    contract={"module":module_name,"function":function,"code_sha256":code_sha,"upstream_request":request,
              "evidence_kind":"controlled_adapter_received_report_not_verified_publisher_article"}
    raw=RawCollection(owner)
    try:
        for item in items:
            payload=source_payload(from_item(item));acquired=datetime.now(timezone.utc).isoformat()
            if _moment(payload["fetched_at"])>_moment(acquired):raise ValueError("future adapter acquisition")
            packet={"source":payload,"collector":contract,"acquired_at":acquired}
            sha=digest(packet);ident=digest({"scope":raw.scope,"sha256":sha})
            with raw.db:raw.db.execute("INSERT OR IGNORE INTO adapter_captures VALUES(?,?,?)",(ident,sha,json.dumps(packet,ensure_ascii=False)))
            item.raw_meta={**item.raw_meta,"collector_capture_id":ident,"collector_code_sha256":code_sha}
    finally:raw.db.close()
    return items


def _adapter(connection,capture_id,source):
    table=connection.execute("SELECT 1 FROM sqlite_master WHERE name='adapter_captures'").fetchone()
    if not table:return None
    row=connection.execute("SELECT sha256,payload FROM adapter_captures WHERE id=?",(capture_id,)).fetchone()
    if not row:return None
    packet=json.loads(row[1])
    if digest(packet)!=row[0]:raise EvidenceIntegrityError("DATA_UNTRUSTED: adapter capture digest mismatch")
    collector=packet["collector"]
    if (COLLECTORS.get(collector.get("module"))!=collector.get("function")
            or packet["source"]!=source_payload(source)):return None
    return packet


def api_acquisition(connection,record):
    """Bind availability to an owned server receipt, not caller-supplied dates."""
    meta=record.get("raw_meta") or {}
    ident=meta.get("response_capture_id")
    if not ident or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='response_captures'").fetchone():return None
    row=connection.execute("SELECT sha256,payload FROM response_captures WHERE id=?",(ident,)).fetchone()
    if not row:return None
    packet=json.loads(row[1])
    if digest(packet)!=row[0]:raise EvidenceIntegrityError("DATA_UNTRUSTED: response capture digest mismatch")
    if (any(meta.get(key)!=packet.get(key) for key in ("response_sha256","source","fetched_from","fetched_at","requested_at","recorded_at"))
            or record["source"]!=packet["source"] or record["fetched_at"]!=packet["fetched_at"]):return None
    return max(_moment(packet["recorded_at"]),_moment(packet["fetched_at"]))


def input_contract(connection,record):
    """Trust upgrades get new observation IDs even if article content is unchanged."""
    from openinvest.services.news_collection import validate_input_snapshot
    meta=record.get("raw_meta") or {}
    if meta.get("response_sha256"):
        try:validate_input_snapshot(connection,record)
        except ValueError as exc:
            if str(record.get("proof_contract_id","")).startswith("api:"):
                raise EvidenceIntegrityError("DATA_UNTRUSTED: previously authenticated API evidence unavailable") from exc
            return "unverified"
        if api_acquisition(connection,record) is None:return "unverified"
        if not api_observation(connection,record):return "unverified"
        return "api:"+str(record["source"])+":"+str(meta.get("decoder_version",1))
    if meta.get("collector_capture_id"):
        packet=_adapter(connection,meta["collector_capture_id"],from_input(record))
        if packet:return "adapter:"+digest(packet["collector"])
    return "unverified"


def api_observation(connection,record):
    """Observation identity, decoder and parents come from the collector's row."""
    meta=record.get("raw_meta") or {};ident=meta.get("source_observation_id")
    if not ident:return False
    row=connection.execute("SELECT payload FROM raw_candidates WHERE id=?",(ident,)).fetchone()
    if not row:return False
    observed=json.loads(row[0]);receipt=observed.get("source_receipt") or {}
    if observed.get("source_observation_id")!=ident or observed.get("source_item_id")!=meta.get("source_item_id"):return False
    if any(meta.get(key)!=value for key,value in receipt.items() if key!="response_storage"):return False
    mapping=connection.execute("SELECT mapping FROM raw_locator_mappings WHERE candidate_id=?",(ident,)).fetchone()
    projection=json.loads(mapping[0]) if mapping else {}
    if meta.get("locator_projection_version",receipt.get("decoder_version",1))!=projection.get("projection_version",receipt.get("decoder_version",1)):return False
    expected={**from_input(observed),"url":projection.get("source_locator") or observed.get("source_locator") or observed.get("url")}
    return source_payload(expected)==source_payload(from_input(record))


def reporting_use(data_root,source,*,as_of=None,policy=None):
    root=Path(data_root);payload=source_payload(source);ident=source_fingerprint(source)
    result={"allowed":False,"source_id":ident,"kind":"acquisition_unverified","reason":"source_acquisition_evidence_missing",
            "claims_verified":False,"learning_granted":False}
    meta=metadata(source);gate=policy or ReportingPolicy(root);excluded=gate.excluded_ids()
    ids=source_artifact_ids(meta)|{"source_report:"+ident}
    if meta.get("upstream_publication") and meta.get("upstream_instance"):
        ids.add("replica_publication:"+meta["upstream_instance"]+":"+meta["upstream_publication"])
    if meta.get("collector_code_sha256"):ids.add("collector_version:"+meta["collector_code_sha256"])
    if ids & excluded:return {**result,"reason":"source_or_parent_revoked"}
    if meta.get("origin") in ("test","synthetic") or meta.get("source_origin") in ("test","synthetic"):
        return {**result,"reason":"synthetic_source_audit_only"}
    if gate.allows("source_report:"+ident,payload,"advice"):
        if as_of is not None:
            record=gate._state()[0]["source_report:"+ident]
            acquired=_moment(payload["fetched_at"])
            if record.get("origin")!="verified_point_in_time":acquired=max(acquired,_moment(record["reviewed_at"]))
            if acquired>as_of:return result
        return {**result,"allowed":True,"kind":"independently_reviewed_source_report","reason":"content_bound_review"}
    if not meta.get("source_snapshot_id") and not meta.get("collector_capture_id"):return result
    path=root/"db/source_snapshots.sqlite"
    if not path.exists():return result
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
        snapshot=meta.get("source_snapshot_id")
        captured=None
        if snapshot and connection.execute("SELECT 1 FROM sqlite_master WHERE name='raw_input_snapshots'").fetchone():
            row=connection.execute("SELECT payload FROM raw_input_snapshots WHERE id=?",(snapshot,)).fetchone()
            if row:
                captured=json.loads(row[0])
                from openinvest.services.agent_queue import news_content_hash
                if news_content_hash(captured)!=captured.get("content_sha256"):
                    raise EvidenceIntegrityError("DATA_UNTRUSTED: source input digest mismatch")
                if source_payload(from_input(captured))!=payload:return {**result,"reason":"source_does_not_match_capture"}
                captured_meta={**(captured.get("raw_meta") or {}),"source_snapshot_id":snapshot}
                if source_artifact_ids(captured_meta)&excluded:return {**result,"reason":"source_or_parent_revoked"}
                contract=input_contract(connection,captured)
                if contract.startswith("api:"):
                    acquired=api_acquisition(connection,captured)
                    if as_of is None or acquired<=as_of:
                        return {**result,"allowed":True,"kind":"captured_api_response","reason":"original_api_item_verified","acquired_at":acquired.isoformat()}
                meta=captured.get("raw_meta") or {}
        if meta.get("collector_capture_id"):
            packet=_adapter(connection,meta["collector_capture_id"],source)
            if packet and "collector_version:"+packet["collector"]["code_sha256"] in excluded:
                return {**result,"reason":"source_or_parent_revoked"}
            if packet and (as_of is None or _moment(packet["acquired_at"])<=as_of):
                return {**result,"allowed":True,"kind":"controlled_adapter_report","reason":"collector_output_verified",
                        "acquired_at":packet["acquired_at"],"collector":packet["collector"]["module"]}
    return result


EVENT_FIELDS=("event_id","one_line_claim","event_type","stance","severity","ts","entities","affected_symbols")


def event_payload(event):
    result={key:event.get(key) for key in EVENT_FIELDS}
    result["severity"]={1:"low",2:"mid",3:"high"}.get(result["severity"],result["severity"])
    for key in ("entities","affected_symbols"):result[key]=sorted(set(result[key] or []))
    return result


def event_review_payload(event,sources):
    return {"event":event_payload(event),"sources":sorted(source_fingerprint(s) for s in sources)}


def event_review_id(event,sources):return "event_report:"+digest(event_review_payload(event,sources))


def record_backend_observation(data_root,event,*,producer,inputs):
    """Internal producers persist an observation before the recoverable outbox write."""
    if producer!="openinvest.jobs.price_sentinel":raise ValueError("unknown backend observation producer")
    module=importlib.import_module(producer)
    from openinvest.core.decision_evidence import _plain
    from openinvest.services.news_collection import RawCollection
    content={"event":event_payload(event),"producer":producer,
             "code_sha256":hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),"inputs":_plain(inputs)}
    raw=RawCollection(data_root)
    try:
        ident=digest({"scope":raw.scope,"content":content})
        packet={**content,"acquired_at":datetime.now(timezone.utc).isoformat()}
        with raw.db:raw.db.execute("INSERT OR IGNORE INTO backend_observations VALUES(?,?,?)",(ident,digest(packet),json.dumps(packet,ensure_ascii=False)))
        Eligibility(data_root).record_audit_lineage("backend_observation:"+ident,content,{"collector_version:"+content["code_sha256"]})
        return ident
    finally:raw.db.close()


def backend_observation_use(data_root,event,ident,*,as_of=None,policy=None):
    path=Path(data_root)/"db/source_snapshots.sqlite"
    if not path.exists():return False
    gate=policy or ReportingPolicy(data_root)
    if "backend_observation:"+str(ident) in gate.excluded_ids():return False
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='backend_observations'").fetchone():return False
        row=connection.execute("SELECT sha256,payload FROM backend_observations WHERE id=?",(ident,)).fetchone()
    if row is None:return False
    packet=json.loads(row[1])
    if row[0]!=digest(packet):raise EvidenceIntegrityError("DATA_UNTRUSTED: backend observation digest mismatch")
    witness=(packet.get('inputs') or {}).get('market_witness')
    if packet.get('event',{}).get('event_type')=='price_alert':
        # Older native-price alerts could be based on a failed refresh or a
        # converted futures/FX reference with no quote timestamp. Ownership of
        # that old packet does not establish a real current/native bank quote.
        if not isinstance(witness,dict) or not _native_quote_witness_allowed(witness,packet['acquired_at']):
            return False
    if witness is not None and not _market_witness_temporal(witness,packet['acquired_at']):return False
    return (packet.get("producer")=="openinvest.jobs.price_sentinel" and packet.get("event")==event_payload(event)
            and _moment(event["ts"])<=_moment(packet["acquired_at"])
            and "collector_version:"+packet["code_sha256"] not in gate.excluded_ids()
            and (as_of is None or _moment(packet["acquired_at"])<=as_of))


def event_reporting_use(data_root,event,sources,support,*,as_of=None,policy=None):
    gate=policy or ReportingPolicy(data_root)
    if "event:"+event["event_id"] in gate.excluded_ids():return False
    if gate.allows(event_review_id(event,sources),event_review_payload(event,sources),"advice"):
        if sources and not any(reporting_use(data_root,source,as_of=as_of,policy=gate)["allowed"] for source in sources):return False
        if as_of is None:return True
        record=gate._state()[0][event_review_id(event,sources)]
        return record.get("origin")=="verified_point_in_time" or _moment(record["reviewed_at"])<=as_of
    if not support or support.get("event_sha256")!=digest(event_payload(event)):return False
    if support.get("kind")=="backend_observation":
        return backend_observation_use(data_root,event,support["observation_id"],as_of=as_of,policy=gate)
    return any(source_fingerprint(source)==support.get("source_id") and
        reporting_use(data_root,source,as_of=as_of,policy=gate)["allowed"] for source in sources)


def event_version_binding(index, event_id, version):
    """Validate the original quoted version and its still-qualified current view.

    Acquisition metadata may advance, but a changed normalized fact, retained
    source content or source permission cannot silently inherit the old input.
    """
    from openinvest.services.news_identity import analysis_identity
    if type(version) is not int or version <= 0:
        return {'allowed': False, 'reason': 'invalid_event_version'}
    row = index.conn.execute('SELECT payload_json FROM event_versions WHERE event_id=? AND id=?',
                             (event_id, version)).fetchone()
    current = index.get_event(event_id)
    if row is None or current is None:
        return {'allowed': False, 'reason': 'source_missing_or_unqualified'}
    original = json.loads(row[0]); original['_version_id'] = version
    sources = original.get('sources', [])
    if (original.get('event_id') != event_id
            or index._source_excluded(event_id, Eligibility(index.data_root).excluded_ids(), sources=sources)
            or not index._reporting_allowed(original, sources=sources, support=original.get('reporting_support'))):
        return {'allowed': False, 'reason': 'original_source_unqualified'}
    if event_payload(original) != event_payload(current):
        return {'allowed': False, 'reason': 'referenced_event_fact_changed'}
    if sources:
        qualified = [s for s in sources if reporting_use(index.data_root, s, policy=index._policy())['allowed']]
        identity = analysis_identity(original, qualified)
        if identity != (current.get('_analysis_cause_id'), current.get('_analysis_version_id')):
            return {'allowed': False, 'reason': 'referenced_source_content_changed'}
    elif current.get('_version_id') != version:
        return {'allowed': False, 'reason': 'backend_observation_version_changed'}
    return {'allowed': True, 'reason': 'original_and_current_input_qualified', 'original': original}


def _market_witness_temporal(witness, acquired_at):
    try:
        if not isinstance(witness,dict) or witness.get('schema')!='openinvest.market_witness.v1':return False
        observed=_moment(witness['observation_asof']);captured=_moment(acquired_at)
        if observed>captured:return False
        source=witness.get('source_observation') or {}
        received=source.get('received_at')
        if received and _moment(received)>captured:return False
        if witness.get('kind')=='completed_session_return':
            reference=witness.get('return_reference') or {}
            daily = reference.get('source_observation') or source
            if daily.get('daily_alert_eligible') is not True or not daily.get('received_at'):
                return False
            received = _moment(daily['received_at'])
            ends = daily.get('completion_ends') or {}
            return (received <= captured and any(
                _moment(end) == observed and observed <= received for end in ends.values()))
        return witness.get('kind') in {'native_quote_alert','intraday_price_observation'}
    except (ValueError,TypeError,KeyError):return False


def _native_quote_witness_allowed(witness, acquired_at):
    if (witness.get('kind')!='native_quote_alert' or witness.get('quote_fresh') is not True
            or witness.get('is_reference') is not False or not _market_witness_temporal(witness,acquired_at)):
        return False
    try:
        age=(_moment(acquired_at)-_moment(witness['quote_asof'])).total_seconds()
        return 0<=age<=25*60 and (witness.get('source_observation') or {}).get('quote_fresh') is True
    except (ValueError,TypeError,KeyError):return False
