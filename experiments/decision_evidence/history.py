"""Read-only coverage audit of historical production decision evidence.

This importer extracts persisted fields. It never imports the application, applies
today's parser/rules to old proposals, or interprets a user execution declaration
as a broker fill. ``summary`` is the only public output; the other keys are private.
Historical proposal confidence has no established event semantics and is never
eligible for probability scoring merely because a number can be extracted.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_VERDICTS = {"BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL", "UNCLEAR"}
_BLIND = re.compile(r"(?:^|[._-])(?:sealed|debate|macrosoft|macro_soft|sonnet_arm|t2_confidence)(?:$|[._-])", re.I)
_SECTION = re.compile(r"(?m)^#{2,3} [^\n]+$")


class HistoryAuditError(ValueError):
    """The input cannot be audited safely or did not remain stable."""


def _blind(path: Path) -> bool:
    return any(_BLIND.search(part) for part in path.parts)


def _check_source_path(path: Path) -> None:
    # Inspect the lexical path before resolve() discards symlink ancestors.
    lexical = path.absolute()
    if any(part.is_symlink() for part in (lexical, *lexical.parents)):
        raise HistoryAuditError("symlink source or ancestor refused")
    if _blind(lexical) or _blind(lexical.resolve()):
        raise HistoryAuditError("blind-experiment source refused")


def _inventory(root: Path, last_day: date) -> set[Path]:
    """Allowlisted direct files only; never descend into runs or experiments."""
    _check_source_path(root)
    selected: set[Path] = set()
    for day in root.iterdir():
        if not _DAY.fullmatch(day.name):
            continue
        try:
            observed = date.fromisoformat(day.name)
        except ValueError:
            continue
        if observed > last_day:
            continue
        _check_source_path(day)
        if not day.is_dir():
            continue
        for path in day.iterdir():
            if path.suffix == ".md" or path.name.endswith(("_committee.json", "_path.json")):
                _check_source_path(path)
                if path.is_file():
                    selected.add(path)
    ledger = root.parent / ".dreams" / "executions.jsonl"
    if ledger.exists() or ledger.is_symlink():
        _check_source_path(ledger)
        selected.add(ledger)
    return selected


def _checked_date(value: str) -> date:
    if not _DAY.fullmatch(value):
        raise HistoryAuditError("cutoff must be an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HistoryAuditError("cutoff must be an ISO calendar date") from exc


def _read_stable(path: Path, sources: dict[Path, str]) -> bytes:
    _check_source_path(path)
    try:
        first = path.read_bytes()
        second = path.read_bytes()
    except OSError as exc:
        raise HistoryAuditError("selected source disappeared or became unreadable") from exc
    digest = hashlib.sha256(first).hexdigest()
    if hashlib.sha256(second).hexdigest() != digest:
        raise HistoryAuditError("source changed during read")
    sources[path] = digest
    return first


def _unique(values: list[Any]) -> tuple[Any, str]:
    if not values:
        return None, "missing"
    if len(values) != 1:
        return None, "ambiguous"
    return values[0], "parsed"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) and 0 <= out <= 1 else None


def _header(text: str) -> dict[str, Any]:
    # Body text that merely quotes a valid-looking metadata header is not metadata.
    first_section = _SECTION.search(text)
    prefix = text[:first_section.start()] if first_section else text
    lines = re.findall(r"(?m)^\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s+([^\s)]+)\)\s*$", prefix)
    value, status = _unique(lines)
    out: dict[str, Any] = {"status": status, "verdict": None, "confidence": None}
    if status == "parsed":
        verdict, confidence = value
        if verdict.upper() not in _VERDICTS or _number(confidence) is None:
            out["status"] = "invalid"
        else:
            out.update(verdict=verdict.upper(), confidence=_number(confidence))
    for name, label in [("symbol", "Symbol"), ("run_at", "Run at"), ("run_id", "Run ID"), ("provider", "Provider")]:
        values = re.findall(r"(?m)^\*\*" + re.escape(label) + r"\*\*:\s*([^\n]+)$", prefix)
        value, status = _unique(values)
        out[name] = value.strip() if status == "parsed" else None
        if name == "run_at":
            out["run_at_status"] = status
            if status == "parsed":
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", out[name]):
                        raise ValueError("not a full timestamp")
                    datetime.fromisoformat(out[name].replace("Z", "+00:00"))
                except ValueError:
                    out["run_at_status"] = "invalid"
    return out


def _section(text: str, title_pattern: str) -> tuple[str | None, str]:
    headings = list(_SECTION.finditer(text))
    found = []
    for index, heading in enumerate(headings):
        if re.fullmatch(title_pattern, heading.group()):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            found.append(text[heading.end():end].strip())
    return _unique(found)


def _allocation_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("−", "-").replace("**", "")
    match = re.fullmatch(
        r"([+-]?)\s*(?:(?:[A-Z]{3}|A\$|US\$|[¥￥$€£])\s*)?([+-]?)\s*"
        r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?)", text
    )
    if not match or (match[1] and match[2]):
        return None
    number = float((match[1] or match[2]) + match[3].replace(",", ""))
    return number if math.isfinite(number) else None


def _raw_allocation(memo: str | None) -> dict[str, Any]:
    """Extract a single saved amount; never infer it from the final header."""
    missing = {"alloc_cny": None, "alloc_currency": None}
    if memo is None:
        return missing
    candidates: list[tuple[Any, str | None]] = []
    seen = set()
    for body in [memo] + re.findall(r"```json\s*\n(.*?)\n```", memo, re.S | re.I):
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        signature = json.dumps(obj, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        for key, value in obj.items():
            field = re.fullmatch(r"suggested_alloc_([A-Z]{3})", key, re.I)
            if field:
                candidates.append((value, field[1].upper()))
            elif key == "alloc_cny":
                # Legacy field names are not proof of the base currency.
                currency = obj.get("alloc_currency")
                candidates.append((value, currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency, re.I) else None))
    for currency, value in re.findall(r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?SUGGESTED_ALLOC_([A-Z]{3})\**:[ \t]*([^\n]*)$", memo):
        candidates.append((value.strip(), currency.upper()))
    if len(candidates) != 1:
        return missing
    value, currency = candidates[0]
    number = _allocation_number(value)
    return {"alloc_cny": number, "alloc_currency": currency if number is not None else None}


def _raw_proposal(memo: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "missing", "verdict": None, "confidence": None, "source": None, **_raw_allocation(memo)}
    if memo is None:
        return out
    candidates: list[tuple[str, Any, Any]] = []
    # Only whole JSON documents and explicit JSON code fences count as structured
    # proposals; arbitrary braces in explanatory prose are not interpreted as one.
    json_texts = [memo] + re.findall(r"```json\s*\n(.*?)\n```", memo, re.S | re.I)
    seen = set()
    for body in json_texts:
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or "verdict" not in obj:
            continue
        signature = json.dumps(obj, sort_keys=True)
        if signature not in seen:
            candidates.append(("json", obj.get("verdict"), obj.get("confidence")))
            seen.add(signature)
    verdicts = re.findall(r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?VERDICT\**:[ \t*]*(BUY|ACCUMULATE|HOLD|TRIM|SELL|UNCLEAR)\b", memo)
    confidences = re.findall(r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?CONFIDENCE\**:[ \t*]*([^\s*]+)", memo)
    if len(verdicts) > 1 or len(confidences) > 1:
        return {**out, "status": "ambiguous"}
    if verdicts or confidences:
        if len(verdicts) != 1 or len(confidences) != 1:
            return {**out, "status": "incomplete"}
        candidates.append(("field_lines", verdicts[0], confidences[0]))
    if not candidates:
        return {**out, "status": "unparseable"}
    normalized = [(source, str(verdict).upper(), _number(confidence)) for source, verdict, confidence in candidates]
    if any(verdict not in _VERDICTS or confidence is None for _, verdict, confidence in normalized):
        return {**out, "status": "invalid"}
    if len({(verdict, confidence) for _, verdict, confidence in normalized}) != 1:
        return {**out, "status": "conflicting_sources"}
    source, verdict, confidence = normalized[0]
    return {**out, "status": "parsed", "source": source, "verdict": verdict, "confidence": confidence}


def _health(text: str) -> tuple[dict[str, Any], str]:
    body, status = _section(text, r"## Input Health[^\n]*")
    if status != "parsed":
        return {}, status
    values = re.findall(r"```json\s*\n(.*?)\n```", body or "", re.S)
    if len(values) != 1:
        return {}, "invalid"
    try:
        value = json.loads(values[0])
    except ValueError:
        return {}, "invalid"
    return (value, "parsed") if isinstance(value, dict) else ({}, "invalid")


def audit_history(committee_root: Path, *, cutoff: str | None = None) -> dict[str, Any]:
    """Audit latest production files up to cutoff; never scan ``runs/`` or arms.

    ``private_records`` contains identifiers and extracted values, including saved
    proposed amounts but not raw prose. They must stay private. Sidecars associate by date/filename only and
    cannot establish an identical run. This importer cannot reconstruct past rules
    or verify fills, so historical replay eligibility is conservatively false.
    """
    root = Path(committee_root)
    _check_source_path(root)
    today = datetime.now(timezone.utc).date()
    last_day = _checked_date(cutoff) if cutoff is not None else today
    if last_day > today:
        raise HistoryAuditError("future cutoff refused")
    if not root.is_dir():
        raise HistoryAuditError("committee root is not a directory")
    counts: Counter[str] = Counter()
    sources: dict[Path, str] = {}
    selected = _inventory(root, last_day)
    sidecar_objects: dict[Path, dict[str, Any] | None] = {}
    # Inventory and hash orphan or malformed sidecars as well as paired ones.
    for side in sorted(selected):
        kind = "committee" if side.name.endswith("_committee.json") else "path" if side.name.endswith("_path.json") else None
        if kind is None:
            continue
        counts[kind + "_sidecar_present"] += 1
        suffix = "_" + kind + ".json"
        if side.with_name(side.name[:-len(suffix)] + ".md") not in selected:
            counts[kind + "_sidecar_orphan"] += 1
        data = _read_stable(side, sources)
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeError):
            obj = None
        if not isinstance(obj, dict):
            counts[kind + "_sidecar_invalid"] += 1
            obj = None
        sidecar_objects[side] = obj
    records = []
    joins: Counter[tuple[str, str]] = Counter()
    for day in sorted(root.iterdir()):
        if not _DAY.fullmatch(day.name):
            continue
        try:
            observed_day = date.fromisoformat(day.name)
        except ValueError:
            counts["invalid_date_directories"] += 1
            continue
        if observed_day > last_day:
            counts["after_cutoff_directories_skipped"] += 1
            continue
        if day.is_symlink():
            raise HistoryAuditError("symlink date directory refused")
        if not day.is_dir():
            continue
        for path in sorted(day.glob("*.md")):
            data = _read_stable(path, sources)
            try:
                text = data.decode("utf-8", errors="strict")
                encoding_status = "valid"
            except UnicodeError:
                text = ""
                encoding_status = "invalid"
                counts["encoding_invalid"] += 1
            header = _header(text)
            memo, memo_status = _section(text, r"(?:## CIO Memo[^\n]*|### CIO)")
            raw = _raw_proposal(memo if memo_status == "parsed" else None)
            health, health_status = _health(text)
            model = health.get("model")
            model = model.strip() if isinstance(model, str) else None
            model_known = bool(model) and model.lower() not in {"unknown", "none", "null"}
            symbol = header["symbol"]
            key = (day.name, symbol or path.stem)
            joins[key] += 1
            counts["documents"] += 1
            counts["header_" + header["status"]] += 1
            counts["raw_proposal_" + raw["status"]] += 1
            counts["raw_allocation_present"] += raw["alloc_cny"] is not None
            counts["input_health_" + health_status] += 1
            counts["model_known" if model_known else "model_unknown"] += 1
            counts["run_timestamp_present"] += header["run_at_status"] == "parsed"
            counts["run_timestamp_" + header["run_at_status"]] += 1
            worker_marker = bool(re.search(r"(?m)^\s*\[WORKER_UNAVAILABLE\] reason=", text))
            worker_value = health.get("worker_unavailable")
            worker_valid = isinstance(worker_value, list) and all(
                isinstance(role, str) and role in {"macro", "quant", "risk", "cio"}
                for role in worker_value
            )
            worker_reported = worker_valid and bool(worker_value)
            counts["body_marker_present"] += worker_marker
            counts["worker_failure_field_invalid"] += "worker_unavailable" in health and not worker_valid
            counts["worker_failure_marker"] += worker_marker
            counts["worker_failure_reported"] += worker_reported
            final_hold = header["status"] == "parsed" and header["verdict"] == "HOLD"
            forced = health.get("forced_hold") is True
            rewritten = health.get("rewritten")
            rewrite_reported = isinstance(rewritten, list) and bool(rewritten)
            counts["forced_hold_reported"] += forced
            counts["rewrite_reported"] += rewrite_reported
            fault_hold = final_hold and forced and worker_reported
            rule_hold = final_hold and forced and rewrite_reported and worker_valid and not worker_reported
            counts["fault_hold_evidence"] += fault_hold
            counts["structured_worker_failure_on_hold"] += final_hold and worker_reported
            counts["rule_hold_evidence"] += rule_hold
            counts["hold_cause_unknown"] += final_hold and not (fault_hold or rule_hold)
            paired = header["status"] == "parsed" and raw["status"] == "parsed" and header["run_at_status"] not in {"invalid", "ambiguous"}
            counts["proposal_final_pairs"] += paired
            if paired:
                counts["verdict_differs"] += raw["verdict"] != header["verdict"]
                # Header formatting rounds to two decimal places; do not turn
                # serialization precision into a confidence-rule intervention.
                counts["confidence_differs_beyond_header_rounding"] += abs(raw["confidence"] - header["confidence"]) > 0.005000001
            sidecars: dict[str, Any] = {}
            source_conflict = False
            for kind, suffix in [("committee", "_committee.json"), ("path", "_path.json")]:
                side = day / (path.stem + suffix)
                if side not in sidecar_objects:
                    continue
                obj = sidecar_objects[side]
                if obj is None:
                    source_conflict = True
                    sidecars[kind] = {"source_sha256": sources[side], "association": "invalid", "same_run_verified": False}
                    continue
                conflict = bool(symbol and obj.get("symbol") and symbol != obj.get("symbol"))
                conflict |= bool(obj.get("date") and obj["date"] != day.name)
                if header.get("run_id") and obj.get("run_id"):
                    conflict |= header["run_id"] != obj["run_id"]
                if kind == "committee" and obj.get("saved_at") and header["run_at_status"] == "parsed":
                    try:
                        conflict |= datetime.fromisoformat(obj["saved_at"]) != datetime.fromisoformat(header["run_at"])
                    except (TypeError, ValueError):
                        conflict = True
                if kind == "committee" and header["status"] == "parsed":
                    verdict = obj.get("verdict")
                    if isinstance(verdict, dict):
                        conflict |= verdict.get("verdict") not in (None, header["verdict"])
                        value = _number(verdict.get("confidence"))
                        if "confidence" in verdict and value is None:
                            counts[kind + "_sidecar_invalid_fields"] += 1
                            conflict = True
                        conflict |= value is not None and abs(value - header["confidence"]) > 0.005000001
                source_conflict |= conflict
                counts[kind + "_sidecar_conflict"] += conflict
                counts[kind + "_weak_association"] += not conflict
                sidecars[kind] = {"source_sha256": sources[side], "association": "conflicting" if conflict else "weak_date_symbol", "same_run_verified": False}
            record = {
                "date": day.name, "symbol": symbol, "filename_stem": path.stem,
                "source_sha256": sources[path], "source_path": str(path),
                "encoding_status": encoding_status,
                "header": header, "raw_proposal": raw, "model": model if model_known else "unknown",
                "input_health_status": health_status,
                "worker_failure_evidence": worker_reported,
                "body_marker_present": worker_marker,
                "worker_failure_field_valid": worker_valid,
                "forced_hold_reported": forced, "rewrite_reported": rewrite_reported,
                "sidecars": sidecars, "source_conflict": source_conflict,
                "paired_observation_eligible": paired and not source_conflict,
                "independent_observation": False,
                "repetition_status": "not_established_no_archives_read",
                "confidence_event_semantics": "unknown",
                "probability_score_eligible": False,
                "historical_replay_eligible": False,
                "replay_exclusion_reasons": ["historical_rule_bundle_not_verified", "immutable_input_snapshot_not_verified", "same_run_identity_not_verified"],
                "execution_declaration": None, "actual_execution_verified": False,
            }
            records.append(record)
    for record in records:
        key = (record["date"], record["symbol"] or record["filename_stem"])
        if joins[key] > 1:
            record["paired_observation_eligible"] = False
            record["repetition_status"] = "duplicate_day_asset"
    counts["duplicate_day_asset_groups"] = sum(n > 1 for n in joins.values())
    # Read only the explicit production declaration ledger, never other .dreams
    # files or experiment arms. A declaration is not evidence of a broker fill.
    executions = root.parent / ".dreams" / "executions.jsonl"
    declarations: dict[str, dict[str, Any]] = {}
    if executions.exists():
        execution_data = _read_stable(executions, sources)
        try:
            execution_text = execution_data.decode("utf-8")
        except UnicodeError:
            counts["execution_declarations_encoding_invalid"] += 1
            execution_text = ""
        for line in execution_text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                counts["execution_declarations_invalid"] += 1
                continue
            if not isinstance(value, dict):
                counts["execution_declarations_invalid"] += 1
                continue
            identifier = value.get("decision_id")
            if not isinstance(identifier, str) or "/" not in identifier:
                counts["execution_declarations_invalid"] += 1
                continue
            day, _ = identifier.split("/", 1)
            if not _DAY.fullmatch(day) or day > last_day.isoformat():
                continue
            if not isinstance(value.get("executed"), bool):
                counts["execution_declarations_invalid"] += 1
                continue
            counts["execution_declaration_rows"] += 1
            declarations[identifier] = {"executed": value["executed"], "has_trade_ids": bool(value.get("trade_ids")), "evidence_type": "user_declaration_only"}
        for record in records:
            identifier = f"{record['date']}/{record['symbol']}"
            if record["symbol"] and identifier in declarations:
                record["execution_declaration"] = declarations[identifier]
                counts["documents_with_execution_declaration"] += 1
    # Check again after all joins, detecting mutation while a different file was
    # being read. Errors disclose no path, symbol, or original content.
    for path, digest in sources.items():
        _check_source_path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise HistoryAuditError("selected source disappeared during audit") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise HistoryAuditError("source changed during audit")
    if _inventory(root, last_day) != selected:
        raise HistoryAuditError("selected source inventory changed during audit")
    counts["stable_sources"] = len(sources)
    counts["paired_observations_eligible"] = sum(r["paired_observation_eligible"] for r in records)
    counts["historical_replay_eligible"] = 0
    counts["probability_score_eligible"] = 0
    counts["actual_execution_verified"] = 0
    summary = {
        "counts": dict(sorted(counts.items())),
        "scope": "latest_production_documents_only",
        "sidecar_association": "weak_date_symbol_only",
        "historical_replay_status": "insufficient_persisted_evidence",
        "confidence_semantics": "unknown_not_probability_scoreable",
        "execution_evidence": "declarations_not_broker_fills",
        "independent_sample_count": None,
        "source_stability_verified": True,
    }
    return {"schema": 1, "summary": summary, "private_records": records,
            "private_sources": [{"path": str(path), "sha256": digest} for path, digest in sorted(sources.items())]}
