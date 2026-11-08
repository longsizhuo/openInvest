"""决策账本聚合纯核（域绑定纯模块，ADR-026）

从 core/decision_ledger.py 拆出：决议↔成交匹配（显式 verdict_id 优先 /
7 天窗口同向）、采纳率汇总。全部函数吃传入的 trades/decisions 列表，零 IO——
四份账本的读时 join（jsonl/SQLite/datetime.now）留在 decision_ledger.py。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from openinvest.calc.ledger_replay import execution_domain as _ledger_execution_domain
from openinvest.calc.ledger_replay import execution_basis, replay_ledger, unobserved_execution
from openinvest.calc.symbols import safe_symbol

# verdict → 期望交易方向（执行匹配用；HOLD 无方向）
_VERDICT_DIRECTION = {"BUY": "BUY", "ACCUMULATE": "BUY", "SELL": "SELL", "TRIM": "SELL"}


# 自动匹配窗口：决议日起 N 个日历天内的同向同标的成交算"执行了该决议"
MATCH_WINDOW_DAYS = 7

# Activity.action → trades.db 的 direction。buy/sell 已含 PortfolioManager.buy/sell 与 CommSec 邮件回灌
# （bought/sold 在 replay_ledger 里归一）。deposit/withdraw/gift/delete_holding/correction 不是成交，不映射。
_HISTORY_DIRECTION = {"buy": "BUY", "sell": "SELL",
                      "fund_order_placed": "BUY", "fund_order_confirmed": "BUY"}

# 不是"人看了决议后下的单"的自动写入源：定投 cron（jobs/dca_daily.py）按固定计划买，生产 90 天
# 66 笔买入里 64 笔是它——若算作执行了同标的的 ACCUMULATE，采纳率会虚高；dca_reconcile 是对账脚本
# 补/冲的定投行（replay_ledger 把 dca_reconcile_backfill/reverse 归一成 buy/sell 并标 source=dca_reconcile，
# 旧版它们因 action 不在映射里被跳过）。CommSec 邮件回灌保留其报告成交及来源，不升级为独立核验。
_AUTO_SOURCES = frozenset({"dca_daily", "dca_reconcile", "drip"})   # drip = 分红再投资（ADR-027），不是"执行了决议"
_REAL_EXECUTION_DOMAINS = frozenset({"client_reported", "broker_live", "real", "user_ledger", "live"})


def execution_domain(record: Dict[str, Any]) -> str:
    """Known paper producers stay simulated, including their legacy feedback rows."""
    reason = str(record.get("reason") or "")
    if reason.split(":", 1)[0] == "paper_autopilot":
        return "paper_simulation"
    return _ledger_execution_domain(record)


def history_as_trades(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """memory/portfolio_history.jsonl 的成交记录 → 与 trades.db 行同形的 dict，喂给 _match_trades。

    MCP / CLI 的 buy/sell 只写 portfolio_history.jsonl、从不写 trades.db——不做这一步，
    "7 天内同标的同向成交自动匹配"只对 REST /api/trades/record 成立，agent 路径永远匹配不上。
    补录已有持仓（source 带 ':existing_position'）和自动定投 / 分红再投资等 _AUTO_SOURCES 不是"执行了决议"，
    跳过。id 用 "history:<行号>" 与 trades.db 的整数 id 区分。
    """
    out: List[Dict[str, Any]] = []
    activities, _ = replay_ledger(records, alias={})
    cancelled = {a.raw.get("order_id") for a in activities if a.action == "fund_order_cancelled"} - {None}
    for a in sorted(activities, key=lambda x: x.seq):      # 文件顺序（回放序是日期升序）
        direction = _HISTORY_DIRECTION.get(a.action)
        source = a.source or ""
        if not direction or ":existing_position" in source or source in _AUTO_SOURCES:
            continue
        if a.action == "fund_order_placed" and a.raw.get("order_id") in cancelled:
            continue
        domain = execution_domain({
            **a.raw, "evidence_basis": a.evidence_basis,
            "execution_domain": getattr(a, "execution_domain", None) or a.raw.get("execution_domain"),
        })
        # 下单意向与确认分开保留。自动 NAV 确认仍是估算，只有具备成交依据的确认才可匹配。
        if a.action == "fund_order_placed":
            status = "planned"
        elif a.raw.get("status") not in (None, "executed"):
            status = a.raw["status"]
        elif domain == "paper_simulation" and a.evidence_basis == "paper_simulated_fill" and a.replayable:
            status = "simulated"
        elif not a.execution_eligible:
            status = "unobserved"
        else:
            status = "executed"
        out.append({
            "id": f"history:{a.seq}", "ts": a.ts, "verdict_id": a.raw.get("verdict_id"),
            "symbol": a.symbol, "direction": direction,
            # 保留历史 matched_trades 的 units/price 形状。
            "units": a.raw.get("units"), "price": a.raw.get("price"), "status": status,
            "source": a.source, "evidence_basis": a.evidence_basis,
            "execution_domain": domain,
            "fee_status": getattr(a, "fee_status", a.raw.get("fee_status")),
            "source_reference": getattr(a, "source_reference", a.raw.get("source_reference")),
        })
    return out


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


def _local_naive(dt: datetime) -> datetime:
    """trades.db 的 ts 是 UTC（带 tz）；决议日期 / Run at 是本地日历时间。统一转本地 naive 再比。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(tzinfo=None)


def _match_trades(
    trades: List[Dict[str, Any]], decision_id: str, date: str, symbol: str, verdict: str,
    run_at: Optional[str] = None, *, execution_domain: str = "real",
) -> List[Dict[str, Any]]:
    """决议 ↔ 成交自动匹配：显式 verdict_id 优先；否则决议后 7 天内同标的同向成交。

    窗口下界：有 run_at（transcript 的 `**Run at**`，本地时间）→ 只算成交时间 ≥ run_at 的单
    （裁决之前的同日买入不是"执行了该决议"）；无 run_at（老 transcript）→ 退回决议日 00:00。
    """
    # Explicit verdict links identify a decision; they never turn a plan or a
    # simulated/projection row into evidence of a real execution.
    eligible = [t for t in trades if _trade_matches_domain(t, execution_domain)]
    explicit = [t for t in eligible
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
    if run_at:
        try:
            lo = _local_naive(datetime.fromisoformat(run_at))
        except ValueError:
            lo = d0  # run_at 坏值 → 退回同日口径
    else:
        lo = d0
    out = []
    for t in eligible:
        if t.get("symbol") != symbol or t.get("direction") != want:
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        # trades.db 的 ts 是 UTC；决议日期是本地日历日。先转本地再比，
        # 否则 UTC+8 用户决议日早上的成交会落在窗口外（差 8 小时）
        ts = _local_naive(ts)
        if lo <= ts <= d1:
            out.append(t)
    return out


def _trade_matches_domain(trade: Dict[str, Any], domain: str) -> bool:
    actual = execution_domain(trade)
    if domain == "real":
        if actual not in _REAL_EXECUTION_DOMAINS:
            return False
    elif actual != domain:
        return False
    if domain == "paper_simulation":
        return (trade.get("status") in ("executed", "simulated")
                and execution_basis(trade) == "paper_simulated_fill")
    return trade.get("status") == "executed" and not unobserved_execution(trade)


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
