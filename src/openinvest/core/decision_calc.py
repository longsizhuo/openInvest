"""决策账本聚合纯核（域绑定纯模块，ADR-026）

从 core/decision_ledger.py 拆出：决议↔成交匹配（显式 verdict_id 优先 /
7 天窗口同向）、采纳率汇总。全部函数吃传入的 trades/decisions 列表，零 IO——
四份账本的读时 join（jsonl/SQLite/datetime.now）留在 decision_ledger.py。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from openinvest.calc.symbols import safe_symbol

# verdict → 期望交易方向（执行匹配用；HOLD 无方向）
_VERDICT_DIRECTION = {"BUY": "BUY", "ACCUMULATE": "BUY", "SELL": "SELL", "TRIM": "SELL"}


# 自动匹配窗口：决议日起 N 个日历天内的同向同标的成交算"执行了该决议"
MATCH_WINDOW_DAYS = 7


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
    "_VERDICT_DIRECTION",
    "MATCH_WINDOW_DAYS",
    "_safe_stem",
    "_vid_matches",
    "_match_trades",
    "summarize_decisions",
]
