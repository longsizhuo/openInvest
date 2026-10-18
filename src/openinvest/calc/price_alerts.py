"""每标的报警线求值（C8，纯计算，禁 IO）。

输入 = 用户在 strategy.target_assets[].alerts 里设的阈值 + 当前价 / 成本 / 今日涨跌，
输出 = 每条已设规则的当前量、离触发线还有多远、是否已触及。
jobs/price_sentinel（判触发）和 status 视图（显示距离）共用这一份，两处口径不会漂。

口径（均为百分点，3 = 3%；与 target_pct 的 0~1 小数不同）：
- price_above / price_below   当前价 vs 价格线；距离 = 现价还要涨 / 跌多少 %（相对现价）
- day_drop_pct / day_rise_pct 最新日线的涨跌（相对前收）vs 阈值；距离 = 阈值 − 已走幅度
- drawdown_from_cost_pct      持仓浮亏（相对成本）vs 阈值；仅有成本的持仓可判
distance_pct > 0 = 还没到，≤ 0 = 已触及（breached）；缺输入的规则 current / distance_pct 为 None、breached=False。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional

RULES = ("price_above", "price_below", "day_drop_pct", "day_rise_pct", "drawdown_from_cost_pct")

# 非价格线的规则：距离是"个百分点"，当前量带自己的标签
_PCT_LABEL = {"day_drop_pct": "今日", "day_rise_pct": "今日", "drawdown_from_cost_pct": "较成本"}
_DAY_RULES = ("day_drop_pct", "day_rise_pct")


def _ok(v: Optional[float]) -> bool:
    return v is not None and math.isfinite(v)


def evaluate(
    alerts: Optional[Mapping[str, Any]],
    *,
    price: Optional[float],
    avg_cost: Optional[float] = None,
    day_change_pct: Optional[float] = None,
    day_bar: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """对每条已设（非 None）规则求值，按 RULES 顺序返回
    [{rule, threshold, current, distance_pct, breached, text}]。

    day_bar：day_change_pct 所属的行情交易日（YYYY-MM-DD，最新一根日线的日期）。休市日行情会一直停在上一
    交易日那根线上，day_* 规则的结果带上 bar_date，调用方（哨兵）据此「同一根线只报一次」。"""
    alerts = alerts or {}
    p = price if _ok(price) and price > 0 else None
    pnl_pct = (p / avg_cost - 1.0) * 100.0 if p is not None and _ok(avg_cost) and avg_cost > 0 else None
    day = day_change_pct if _ok(day_change_pct) else None

    out: List[Dict[str, Any]] = []
    for rule in RULES:
        t = alerts.get(rule)
        if t is None:
            continue
        t = float(t)
        cur: Optional[float]
        dist: Optional[float]
        if rule == "price_above":
            cur, dist = p, None if p is None else (t - p) / p * 100.0
        elif rule == "price_below":
            cur, dist = p, None if p is None else (p - t) / p * 100.0
        elif rule == "day_drop_pct":
            cur, dist = day, None if day is None else day + t
        elif rule == "day_rise_pct":
            cur, dist = day, None if day is None else t - day
        else:  # drawdown_from_cost_pct
            cur, dist = pnl_pct, None if pnl_pct is None else pnl_pct + t
        hit = {
            "rule": rule,
            "threshold": t,
            "current": None if cur is None else round(cur, 4),
            "distance_pct": None if dist is None else round(dist, 2),
            "breached": dist is not None and dist <= 0,
        }
        if rule in _DAY_RULES:
            hit["bar_date"] = day_bar
        hit["text"] = describe(hit)
        out.append(hit)
    return out


def describe(hit: Mapping[str, Any]) -> str:
    """一条规则结果 → 一句人话（status 与报警正文共用）。"""
    rule, t, cur, dist = hit["rule"], hit["threshold"], hit["current"], hit["distance_pct"]
    if dist is None:
        need = {"drawdown_from_cost_pct": "成本", "day_drop_pct": "今日涨跌", "day_rise_pct": "今日涨跌"}.get(rule, "现价")
        return f"{rule} {t:g}：暂无法判定（缺{need}）"
    label = _PCT_LABEL.get(rule)
    if hit["breached"]:
        if hit.get("bar_date"):  # 休市日看的是上一交易日那根线，别写成「今日」
            label = f"{hit['bar_date']} 当日"
        tail = f"（{label} {cur:+.2f}%）" if label else ""
        return f"已触及 {rule} {t:g}{tail}"
    return f"距 {rule} 还有 {dist:.1f}{'个百分点' if label else '%'}"
