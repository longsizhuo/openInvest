"""Decision quality monitoring independent of trade signals and HOLD silence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import re

from openinvest.core.decision_ledger import parse_committee_file


def findings(memory_root: Path, *, hold_window: int = 20) -> list[dict]:
    hold_window = max(2, int(hold_window))
    by_symbol = defaultdict(list)
    root = Path(memory_root)
    for day in sorted((root / ".committee").glob("????-??-??")):
        if not day.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
            continue
        try:
            dt = datetime.fromisoformat(day.name).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt.weekday() >= 5:
            continue
        for path in day.glob("*.md"):
            row = parse_committee_file(path)
            if row:
                by_symbol[row.get("symbol") or path.stem].append((dt, row))
    out = []
    try:
        delivered = set(json.loads((root / ".state/job_watchdog_alerts.json").read_text()))
    except (OSError, ValueError, TypeError):
        delivered = set()
    for symbol, rows in by_symbol.items():
        tail = rows[-hold_window:]
        if len(tail) == hold_window and all(r.get("verdict") == "HOLD" for _, r in tail):
            # Anchor at the start of the entire streak so reruns do not bypass dedup.
            start = len(rows) - 1
            while start > 0 and rows[start - 1][1].get("verdict") == "HOLD":
                start -= 1
            since = rows[start + hold_window - 1][0]
            out.append({"key": f"quality:long_hold:{symbol}:{rows[start][0].date()}", "since": since,
                        "text": f"[QUALITY_ALERT] {symbol} 连续 {len(rows)-start} 个有记录工作日为 HOLD。请复核买卖两侧证据、规则改写和等待条件；这不是交易指令。"})
        last = rows[-1][1]
        health = last.get("input_health") or {}
        if health.get("decision_status") == "DATA_UNTRUSTED":
            out.append({"key": f"quality:untrusted:{symbol}:{rows[-1][0].date()}", "since": rows[-1][0],
                        "text": f"[QUALITY_ALERT] {symbol} 输入或版本未通过核验，当前无可执行建议，不能当作普通 HOLD。"})
    for path in (root / ".state").glob("review_plan_*.json"):
        try:
            p = json.loads(path.read_text())
            if p.get("state") == "active" and datetime.fromisoformat(p["spec"]["expires_at"]) <= datetime.now(timezone.utc):
                out.append({"key": f"quality:plan_expiry:{p['plan_id']}", "since": datetime.fromisoformat(p["spec"]["expires_at"]),
                            "text": f"[QUALITY_ALERT] {p.get('symbol',path.stem)} 等待计划已到期；报价不可用也必须复核。"})
            for event in p.get("alerts", []):
                key = f"quality:plan_event:{event['id']}"
                if key + "#once" not in delivered:
                    out.append({"key": key, "since": datetime.fromisoformat(event["at"]), "one_shot": True,
                                "text": f"[QUALITY_ALERT] {event['symbol']} 等待计划发生 {event['kind']}（计划 {event.get('plan_id')}）。旧条件及变更记录已保存，需要复核；不自动下单。"})
            if p.get("state") in ("triggered", "expired") or p.get("last_error"):
                out.append({"key": f"quality:plan:{p.get('plan_id', path.stem)}:{p.get('state')}:{p.get('last_error','')}",
                            "since": datetime.fromisoformat(p.get("first_error_at") or p["changed_at"]),
                            "text": f"[QUALITY_ALERT] {p.get('symbol',path.stem)} 等待计划需要复核：{p.get('last_error') or p.get('state')}；不得静默把门槛移到下一价位。"})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            out.append({"key": f"quality:plan_corrupt:{path.name}",
                        "since": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                        "text": f"[QUALITY_ALERT] 复核计划 {path.name} 无法读取，需要修复。"})
    return out


def render(memory_root: Path) -> str:
    from openinvest.core.config import load_config
    rows = findings(memory_root, hold_window=load_config().scheduler.decision_hold_review_days)
    return "\n".join(r["text"] for r in rows)
