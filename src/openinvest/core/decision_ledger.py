"""Decision Accounting（issue #133 Decision 9）— 决议 ↔ 执行 ↔ 结果的读时 join。

三份既有数据从未关联：committee md（决议）、trades.db（执行）、
verdict_review.jsonl / interventions.jsonl（结果/干预）。本模块补上 join，
不物化新视图文件——唯一新增持久化是 executions.jsonl（用户执行/拒绝记录）。

decision_id 口径 = "<date>/<symbol>"（如 "2026-07-03/GC=F"），即
memory/.committee/<date>/<sym>.md 的天然主键；trades.db 现有 verdict_id
列填同一格式即完成硬关联，无 schema 变更。

executions.jsonl 是追加账本（ADR-016）：重放/重试跳过完全相同的最新记录；
内容不同则 append 新行，读方取每 decision_id 最后一条（允许改口）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.utils.symbols import safe_symbol

log = logging.getLogger(__name__)

# verdict → 期望交易方向（执行匹配用；HOLD 无方向）
_VERDICT_DIRECTION = {"BUY": "BUY", "ACCUMULATE": "BUY", "SELL": "SELL", "TRIM": "SELL"}

# 自动匹配窗口：决议日起 N 个日历天内的同向同标的成交算"执行了该决议"
MATCH_WINDOW_DAYS = 7

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_committee_file(path: Path) -> Optional[Dict[str, Any]]:
    """从 committee md 抽 verdict + macro snapshot（单一可信源，原 jobs/verdict_review 私有）。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    verdict_m = re.search(r"\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s+([\d.]+)\)", text)
    if not verdict_m:
        return None
    macro_m = re.search(r"## Macro Context Snapshot.*?```json\n(.*?)\n```", text, re.DOTALL)
    macro = {}
    if macro_m:
        try:
            macro = json.loads(macro_m.group(1))
        except json.JSONDecodeError:
            pass
    sym_m = re.search(r"\*\*Symbol\*\*:\s*(\S+)", text)
    alloc_m = re.search(r"\*\*Suggested allocation CNY\*\*:\s*(-?[\d,.]+)", text)
    alloc = None
    if alloc_m:
        try:
            alloc = float(alloc_m.group(1).replace(",", ""))
        except ValueError:
            pass
    return {
        "verdict": verdict_m.group(1).upper(),
        "confidence": float(verdict_m.group(2)),
        "macro_at_decision": macro,
        "symbol": sym_m.group(1) if sym_m else None,
        "alloc_cny": alloc,
    }


def _executions_path() -> Path:
    from openinvest.core.memory_store import MemoryStore
    return MemoryStore().root / ".dreams" / "executions.jsonl"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record_execution(
    decision_id: str,
    executed: bool,
    reason: Optional[str] = None,
    trade_ids: Optional[List[int]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """记录用户对某决议的执行/拒绝（宿主 Agent 通过 CLI/API/MCP 回写）。

    幂等（ADR-016）：与该 decision_id 现存最新记录内容相同 → 跳过 append。
    check-then-append 全程持 fcntl 排他锁——HTTP 重试 / agent 重发 / CLI 与 MCP
    并发同写时不会双记（ADR-016 的原子幂等闸要求）。
    """
    import fcntl

    if "/" not in decision_id:
        raise ValueError(f"decision_id 应为 '<date>/<symbol>'，收到: {decision_id!r}")
    rec = {
        "schema": 1,
        "decision_id": decision_id,
        "executed": bool(executed),
        "reason": reason or None,
        "trade_ids": trade_ids or None,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    p = path or _executions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        latest = None
        for line in f.read().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("decision_id") == decision_id:
                latest = r
        if latest is not None and all(
            latest.get(k) == rec[k] for k in ("executed", "reason", "trade_ids")
        ):
            return latest  # 重放/重试，幂等跳过（锁随 with 释放）
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _safe_stem(symbol: str) -> str:
    """symbol → 落盘文件名 stem（与 persist/coordinator 的 sanitize 同式）。"""
    return safe_symbol(symbol)


def _vid_matches(vid: str, decision_id: str, date: str, symbol: str) -> bool:
    """trades.verdict_id 是否指向该决议。接受两种口径：
    新的 decision_id（"<date>/<symbol>"）+ 历史文档写法（transcript 路径
    "memory/.committee/<date>/<safe>.md"，db/trades_db 旧注释的格式）。"""
    if vid == decision_id:
        return True
    tail = vid.rsplit(".md", 1)[0].split("/")[-2:]
    return tail == [date, _safe_stem(symbol)]


def _match_trades(
    trades: List[Dict[str, Any]], decision_id: str, date: str, symbol: str, verdict: str,
) -> List[Dict[str, Any]]:
    """决议 ↔ 成交自动匹配：显式 verdict_id 优先；否则决议日起 7 天内同标的同向成交。"""
    explicit = [t for t in trades
                if t.get("verdict_id") and _vid_matches(t["verdict_id"], decision_id, date, symbol)]
    if explicit:
        return explicit
    want = _VERDICT_DIRECTION.get(verdict)
    if not want:
        return []  # HOLD/UNCLEAR 无期望方向，不做窗口匹配
    try:
        d0 = datetime.fromisoformat(date)
    except ValueError:
        return []
    d1 = d0 + timedelta(days=MATCH_WINDOW_DAYS)
    out = []
    for t in trades:
        if t.get("symbol") != symbol or t.get("direction") != want:
            continue
        if t.get("status") not in (None, "executed", "planned"):
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        # trades.db 的 ts 是 UTC；决议日期是本地日历日。先转本地再比，
        # 否则 UTC+8 用户决议日早上的成交会落在窗口外（差 8 小时）
        if ts.tzinfo is not None:
            ts = ts.astimezone()
        ts = ts.replace(tzinfo=None)
        if d0 <= ts <= d1:
            out.append(t)
    return out


def list_decisions(days: int = 90) -> List[Dict[str, Any]]:
    """统一决策视图：每条委员会决议 join 干预 / 执行 / 事后结果。最新在前。

    全部读时 join——数据源是四份既有账本，不新增物化文件：
      memory/.committee/<date>/<sym>.md   决议（含被规则改写后的最终 verdict）
      .dreams/interventions.jsonl         规则干预（原始 verdict → 最终 verdict）
      trades.db                           实际成交（显式 verdict_id 或窗口匹配）
      .dreams/verdict_review.jsonl        事后 returns/hits（verdict_review cron 回填）
      .dreams/executions.jsonl            用户执行/拒绝声明（本模块写入）
    """
    from openinvest.core.memory_store import MemoryStore
    from openinvest.db.trades_db import TradesDB

    root = MemoryStore().root / ".committee"
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    interventions = _load_jsonl(MemoryStore().root / ".dreams" / "interventions.jsonl")
    reviews = _load_jsonl(MemoryStore().root / ".dreams" / "verdict_review.jsonl")
    executions: Dict[str, Dict[str, Any]] = {}
    for r in _load_jsonl(_executions_path()):
        if r.get("decision_id"):
            executions[r["decision_id"]] = r  # 后写覆盖 → 最新记录生效

    try:
        trades = TradesDB().list_trades(limit=10000)
    except Exception as e:  # noqa: BLE001  账本读失败不挡视图
        log.warning(f"decision_ledger 读 trades.db 失败 graceful：{e}")
        trades = []

    # sanitized stem → 真实 symbol 回退映射（Coordinator/旧版 md 无 **Symbol**: 行时用；
    # 否则 decision_id 退化成 "GC_F" 与 interventions/trades 的 "GC=F" 永远 join 不上）
    known_symbols = ({i.get("asset") for i in interventions}
                     | {r.get("asset") for r in reviews}
                     | {t.get("symbol") for t in trades})
    stem_to_symbol = {_safe_stem(s): s for s in known_symbols if s}

    window_open_after = (datetime.now() - timedelta(days=MATCH_WINDOW_DAYS)).strftime("%Y-%m-%d")

    decisions: List[Dict[str, Any]] = []
    if not root.exists():
        return decisions
    for day_dir in sorted(root.iterdir(), reverse=True):
        if not day_dir.is_dir() or not _DATE_DIR.match(day_dir.name):
            continue  # 跳过 <task_id>/status.json 等非日期目录
        if day_dir.name < cutoff:
            continue
        for md in sorted(day_dir.glob("*.md")):
            parsed = parse_committee_file(md)
            if not parsed:
                continue
            date = day_dir.name
            symbol = parsed["symbol"] or stem_to_symbol.get(md.stem, md.stem)
            decision_id = f"{date}/{symbol}"

            iv = next((i for i in interventions
                       if i.get("date") == date and i.get("asset") == symbol), None)
            rv = next((r for r in reviews
                       if r.get("date") == date and r.get("asset") == symbol
                       and r.get("source") == "live"), None)
            matched = _match_trades(trades, decision_id, date, symbol, parsed["verdict"])
            decl = executions.get(decision_id)

            executed: Optional[bool] = None  # None = 未知（无声明也无匹配依据）
            if decl is not None:
                executed = bool(decl["executed"])
            elif matched:
                executed = True
            elif parsed["verdict"] in _VERDICT_DIRECTION and date < window_open_after:
                # 7 天匹配窗已关、无成交无声明 → 判未执行。
                # 窗还开着（date ≥ 今天-7d）时保持 None——用户可能明天才下单，
                # 过早判 False 会把新决议全算进 not_executed 拉低采纳率
                executed = False

            decisions.append({
                "decision_id": decision_id,
                "date": date,
                "symbol": symbol,
                "verdict": parsed["verdict"],
                "confidence": parsed["confidence"],
                "alloc_cny": parsed["alloc_cny"],
                "intervention": None if iv is None else {
                    "rule": iv.get("rule"),
                    "rule_family": iv.get("rule_family"),
                    "original_verdict": iv.get("original_verdict"),
                    "original_alloc": iv.get("original_alloc"),
                },
                "executed": executed,
                "execution": decl,
                "matched_trades": [
                    {k: t.get(k) for k in ("id", "ts", "direction", "units", "price", "status")}
                    for t in matched
                ],
                "outcome": None if rv is None else {
                    "actual_returns": rv.get("actual_returns"),
                    "hits": rv.get("hits"),
                    "macro_shock": (rv.get("macro_shock") or {}).get("detected"),
                },
            })
    return decisions


def summarize_decisions(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """采纳率汇总：有方向建议里 executed/rejected/unknown 各多少，命中拆桶。"""
    directional = [d for d in decisions if d["verdict"] in _VERDICT_DIRECTION]
    out = {
        "total": len(decisions),
        "directional": len(directional),
        "executed": sum(1 for d in directional if d["executed"] is True),
        "not_executed": sum(1 for d in directional if d["executed"] is False),
        "unknown": sum(1 for d in directional if d["executed"] is None),
        "overridden_by_rule": sum(1 for d in decisions if d["intervention"]),
        "with_reason": sum(1 for d in directional
                           if d["execution"] and d["execution"].get("reason")),
    }
    out["adoption_rate"] = (
        round(out["executed"] / len(directional), 3) if directional else None
    )
    return out


__all__ = [
    "parse_committee_file",
    "record_execution",
    "list_decisions",
    "summarize_decisions",
    "MATCH_WINDOW_DAYS",
]
