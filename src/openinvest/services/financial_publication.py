"""Current ledger qualification for displaying or publishing actual returns."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Optional
import uuid

from openinvest.core.decision_evidence import _plain, _write_once, code_version
from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore
from openinvest.core.memory_store import _atomic_write_text, _file_lock
from openinvest.core.portfolio_manager import MemoryNotInitialized, PortfolioManager

_ACTUAL_DOMAINS = {"client_reported", "broker_live", "real", "user_ledger", "live"}
_PRODUCER = "openinvest.pnl_snapshot.v1"
SOURCE_STATEMENT = "客户账本计算，未经券商独立核验；不代表预测能力"
VERIFICATION_PENDING_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 140" role="img" aria-label="收益资料待核">'
    '<title>收益资料待核</title><rect width="800" height="140" fill="#0d1117"/>'
    '<text x="28" y="42" fill="#c9d1d9" font-size="20">收益资料待核</text>'
    '<text x="28" y="78" fill="#8b949e" font-size="14">原曲线已停止展示，来源及计算凭据核对完成后再恢复。</text>'
    '<text x="28" y="110" fill="#8b949e" font-size="14">状态说明，不是收益、学习或预测证据。</text></svg>'
).encode("utf-8")


@dataclass(frozen=True)
class FrozenSVG:
    content: bytes
    manifest_id: str
    manifest_sha256: str
    artifact_path: str


class UnverifiedPointParents(ValueError):
    """The original calculation is retained, but its source facts are audit-only."""


def _actual_source(evidence):
    return (evidence.get("historical_execution_eligible") is True
            and evidence.get("ledger_domain") in _ACTUAL_DOMAINS
            and evidence.get("status") != "unavailable")


def historical_performance_publication_block(*, store: Optional[MemoryStore] = None,
                                             pm: Optional[PortfolioManager] = None) -> Optional[Dict[str, Any]]:
    """Old files cannot regain actual-performance eligibility by being reread."""
    try:
        pm = pm if pm is not None else PortfolioManager(store if store is not None else MemoryStore())
    except MemoryNotInitialized:
        return {"status": "unverified", "reason": "no_portfolio", "source_status": "unavailable",
                "historical_performance_eligible": False, "publication_eligible": False, "pushed": False}
    from openinvest.services.ledger_evidence import account_source_status
    evidence = account_source_status(pm)
    domain = evidence.get("ledger_domain", "unknown")
    paper = domain in {"paper_simulation", "broker_paper"}
    if _actual_source(evidence):
        return None
    simulation = paper and evidence.get("paper_performance_eligible") is True
    return {
        "status": "simulation_only" if simulation else "unverified",
        "reason": "paper_account_not_actual_performance" if simulation else "unverified_execution_history",
        "source_status": evidence.get("status", "unavailable"), "execution_domain": domain,
        "historical_performance_eligible": False, "paper_performance_eligible": simulation,
        "publication_eligible": False, "pushed": False,
    }


def derived_publication_block(reason="unverified_derived_provenance", *, points=None):
    return {"status": "insufficient_history" if points == 1 else "unverified", "reason": reason,
            "historical_performance_eligible": False, "publication_eligible": False, "pushed": False,
            **({"history_points": points} if points is not None else {})}


def _owner(store):
    return str(Path(store.root).resolve())


def _owned(store, path):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(store.data_root).resolve()):
        raise ValueError("financial artifact escaped its owning instance")
    return path


def _receipt_path(store, kind, ident):
    if not re.fullmatch(r"[a-f0-9]{32}", str(ident)):
        raise ValueError("invalid financial receipt reference")
    return _owned(store, Path(store.root) / ".evidence/pnl_publication" / kind / (ident + ".json"))


def _seal(store, kind, payload):
    ident = uuid.uuid4().hex
    value = {**payload, "schema": 1, "kind": kind, "producer": _PRODUCER, "owner": _owner(store),
             "recorded_at": datetime.now(timezone.utc).isoformat(), "code_version": code_version(),
             "allowed_uses": ["display_client_recorded_returns"], "learning_eligible": False,
             "prediction_eligible": False, "market_input_vintage_complete": False,
             "market_input_limit": "Calculation output and ledger fingerprint; not a full point-in-time market replay.",
             "source_statement": SOURCE_STATEMENT}
    envelope = _write_once(_receipt_path(store, kind, ident), value)
    return {"id": ident, "sha256": envelope["sha256"]}


def _load(store, kind, reference):
    if (not isinstance(reference, dict) or set(reference) != {"id", "sha256"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(reference.get("sha256")))):
        raise ValueError("financial receipt unavailable")
    envelope = json.loads(_receipt_path(store, kind, reference["id"]).read_text())
    value = envelope["payload"]
    if (envelope.get("schema") != 1 or envelope.get("sha256") != digest(value)
            or envelope["sha256"] != reference["sha256"] or value.get("kind") != kind
            or value.get("producer") != _PRODUCER or value.get("owner") != _owner(store)
            or value.get("allowed_uses") != ["display_client_recorded_returns"]
            or value.get("learning_eligible") is not False or value.get("prediction_eligible") is not False
            or value.get("market_input_vintage_complete") is not False
            or value.get("source_statement") != SOURCE_STATEMENT
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("code_version")))):
        raise ValueError("financial receipt binding failed")
    return value


def _point(row):
    ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    index = float(row["equity_idx"])
    if ts.tzinfo is None or not math.isfinite(index):
        raise ValueError("invalid financial point")
    point = {"ts": row["ts"], "equity_idx": index, "total_pnl_pct": round(index - 100, 4)}
    if row.get("total_pnl_pct", point["total_pnl_pct"]) != point["total_pnl_pct"]:
        raise ValueError("financial point result changed")
    return point


def seal_calculated_point(store, point, performance, ledger_before):
    """Controlled producer only: retain the calculation and its original inputs, never backfill old rows."""
    from openinvest.services.ledger_evidence import account_source_status
    after = account_source_status(PortfolioManager(store))
    if (not _actual_source(ledger_before) or not _actual_source(after)
            or not ledger_before.get("content_sha256")
            or ledger_before["content_sha256"] != after.get("content_sha256")
            or performance.get("status") != "ok" or performance.get("financial_performance_eligible") is not True):
        raise ValueError("financial calculation inputs changed or are unqualified")
    point = _point(point)
    if point["equity_idx"] != round(100 * (1 + float(performance["twr_all"]) / 100), 4):
        raise ValueError("financial calculation result mismatch")
    return _seal(store, "point", {"point": point, "performance_result": _plain(performance),
        "ledger_input_fingerprint": ledger_before["content_sha256"], "ledger_input": ledger_before,
        "execution_domain": ledger_before["ledger_domain"]})


def qualified_point(store, row):
    value = _load(store, "point", row.get("calculation_receipt"))
    evidence, performance = value["ledger_input"], value["performance_result"]
    point = _point(row)
    if (value["point"] != point or not _actual_source(evidence)
            or value["ledger_input_fingerprint"] != evidence.get("content_sha256")
            or value["execution_domain"] != evidence.get("ledger_domain")
            or performance.get("status") != "ok" or performance.get("financial_performance_eligible") is not True
            or point["equity_idx"] != round(100 * (1 + float(performance["twr_all"]) / 100), 4)):
        raise ValueError("financial point provenance changed")
    from openinvest.services.balance_observations import validate_parent_facts
    if not validate_parent_facts(store, evidence)["allowed"]:
        raise UnverifiedPointParents("financial point source parent unverified")
    return {**point, "calculation_receipt": dict(row["calculation_receipt"])}


def qualified_history(store, path, *, window_days=None):
    """Legacy unreceipted rows remain on disk as audit data, never as qualified returns."""
    path = _owned(store, path)
    if not path.exists():
        return []
    return _qualified_history_bytes(store, path.read_bytes(), window_days=window_days)


def _qualified_history_bytes(store, content, *, window_days=None):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days) if window_days is not None else None
    points, receipts, times = [], set(), set()
    for line in content.decode("utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not row.get("calculation_receipt"):
            continue
        try:
            point = qualified_point(store, row)
        except UnverifiedPointParents:
            continue
        instant = datetime.fromisoformat(point["ts"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        reference = digest(point["calculation_receipt"])
        if reference in receipts or instant in times:
            continue
        receipts.add(reference)
        times.add(instant)
        if cutoff is None or datetime.fromisoformat(point["ts"].replace("Z", "+00:00")) >= cutoff:
            points.append(point)
    return points


def _refs(points):
    if (len({digest(row["calculation_receipt"]) for row in points}) != len(points)
            or len({datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    for row in points}) != len(points)):
        raise ValueError("duplicated financial calculation references or times")
    return [{"point": _point(row), "calculation_receipt": row["calculation_receipt"]} for row in points]


def validate_selected_points(store, points):
    qualified = [qualified_point(store, row) for row in points]
    _refs(qualified)
    if len(qualified) < 2:
        raise ValueError("insufficient distinct qualified financial points")
    return qualified


def _heads_path(store):
    return _owned(store, Path(store.root) / ".state/pnl_artifact_heads.json")


def seal_render_manifest(store, svg_path, history_path, points, *, window_days):
    svg_path, history_path = _owned(store, svg_path), _owned(store, history_path)
    if historical_performance_publication_block(store=store) or len(points) < 2:
        raise ValueError("actual financial rendering requires qualified inputs")
    validate_selected_points(store, points)
    selected = qualified_history(store, history_path, window_days=window_days)
    if _refs(selected) != _refs(points):
        raise ValueError("financial history changed during rendering")
    svg_bytes = svg_path.read_bytes()
    if SOURCE_STATEMENT.encode("utf-8") not in svg_bytes:
        raise ValueError("financial SVG source statement missing")
    reference = _seal(store, "render", {"artifact_path": str(svg_path),
        "artifact_sha256": hashlib.sha256(svg_bytes).hexdigest(),
        "artifact_utf8": svg_bytes.decode("utf-8"),
        "history_path": str(history_path), "history_sha256": hashlib.sha256(history_path.read_bytes()).hexdigest(),
        "window_days": window_days, "selected_points": _refs(selected), "execution_domain": "actual_client_recorded"})
    heads = _heads_path(store)
    with _file_lock(heads):
        state = json.loads(heads.read_text()) if heads.exists() else {}
        state[str(svg_path)] = reference
        _atomic_write_text(heads, json.dumps(state, sort_keys=True))
    return reference


def freeze_render_artifact(store, svg_path, history_path):
    """Freeze exactly the bytes checked against the immutable manifest reference.

    Consumers must serve/stage this buffer, never reopen the mutable SVG path.
    """
    svg_path, history_path = _owned(store, svg_path), _owned(store, history_path)
    head = json.loads(_heads_path(store).read_text())[str(svg_path)]
    manifest = _load(store, "render", head)
    svg_bytes, history_bytes = svg_path.read_bytes(), history_path.read_bytes()
    selected = _qualified_history_bytes(store, history_bytes, window_days=manifest["window_days"])
    if (manifest["artifact_path"] != str(svg_path) or manifest["history_path"] != str(history_path)
            or manifest["artifact_sha256"] != hashlib.sha256(svg_bytes).hexdigest()
            or manifest["history_sha256"] != hashlib.sha256(history_bytes).hexdigest()
            or len(selected) < 2 or manifest["selected_points"] != _refs(selected)
            or manifest["execution_domain"] != "actual_client_recorded"):
        raise ValueError("financial render manifest binding failed")
    return FrozenSVG(svg_bytes, head["id"], head["sha256"], str(svg_path))


def frozen_render_bytes(store, svg_path, history_path):
    return freeze_render_artifact(store, svg_path, history_path).content


def frozen_artifact_block(store, artifact):
    """Revalidate the captured proof and parents without reopening mutable SVG."""
    if blocked := historical_performance_publication_block(store=store):
        return blocked
    try:
        manifest = _load(store, "render", {"id": artifact.manifest_id, "sha256": artifact.manifest_sha256})
        if (manifest["artifact_path"] != artifact.artifact_path
                or manifest["artifact_sha256"] != hashlib.sha256(artifact.content).hexdigest()):
            raise ValueError("frozen financial artifact binding failed")
        points = [{**point["point"], "calculation_receipt": point["calculation_receipt"]}
                  for point in manifest["selected_points"]]
        validate_selected_points(store, points)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return derived_publication_block("unverified_frozen_artifact")
    return None


def render_manifest_block(store, svg_path, history_path):
    """Compatibility status check; consumers use frozen_render_bytes instead."""
    try:
        frozen_render_bytes(store, svg_path, history_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return derived_publication_block("unverified_render_manifest")
    return None


def seal_outperform_event(store, event, history_path, points):
    if historical_performance_publication_block(store=store) or len(points) < 2:
        raise ValueError("actual financial events require qualified inputs")
    all_points = qualified_history(store, history_path)
    if any(row not in all_points for row in points):
        raise ValueError("financial event history changed")
    reference = _seal(store, "event", {"event": event, "selected_points": _refs(points),
                                      "execution_domain": "actual_client_recorded"})
    return {**event, "publication_manifest": reference}


def qualified_outperform_events(store, path, history_path):
    path = _owned(store, path)
    all_points = _refs(qualified_history(store, history_path))
    events = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not row.get("publication_manifest"):
            continue
        value = _load(store, "event", row["publication_manifest"])
        event = {key: val for key, val in row.items() if key != "publication_manifest"}
        selected = [{**point["point"], "calculation_receipt": point["calculation_receipt"]}
                    for point in value["selected_points"]]
        try:
            validate_selected_points(store, selected)
        except UnverifiedPointParents:
            continue
        if (value["event"] != event or len(value["selected_points"]) < 2
                or any(point not in all_points for point in value["selected_points"])
                or value["execution_domain"] != "actual_client_recorded"):
            raise ValueError("financial event manifest binding failed")
        events.append(event)
    return events
