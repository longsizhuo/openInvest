"""每标的报警线求值的 IO 壳（C8）：拉价 + 今日涨跌 → calc.price_alerts.evaluate。

jobs/price_sentinel（判触发）和 services/skill_views（status 显示距离）共用，保证两处
价格口径一致：持仓走 utils.quotes.get_quote（黄金积存金按克价，同 status），
只跟踪未持仓的标的走行情源原价。纯规则求值在 calc/price_alerts.py。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from openinvest.calc.price_alerts import evaluate

log = logging.getLogger(__name__)

_DAY_RULES = ("day_drop_pct", "day_rise_pct")


def fetch_quote(symbol: str, holding: Optional[Dict[str, Any]] = None):
    """最新报价（QuoteSnapshot | None）。持仓行与 status 同口径（克价黄金按 gold_cny_per_gram）；
    没有持仓行的跟踪标的用裸 symbol 走 direct。"""
    from openinvest.services.skill_views import _is_gold_gram
    from openinvest.utils.quotes import get_quote

    h = holding or {"symbol": symbol}
    return get_quote({**h, "proxy_kind": "gold_cny_per_gram"} if holding and _is_gold_gram(h) else h)


def _day_change(
    symbol: str, holding: Optional[Dict[str, Any]], extra: Mapping[str, Any], last_updated: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    """(最新日线涨跌 %（最新价 vs 前收）, 这根日线的日期 YYYY-MM-DD)。direct 行情自带
    extra.day_change_pct + last_updated；黄金克价等没有的，取行情代理（yfinance_proxy 或 symbol）最近两根日线
    （美元口径，日内汇率变动忽略）。拉不到 → (None, None)。
    日期是休市日去重的依据：休市时这根线停在上一交易日，不是「今日」。"""
    v = (extra or {}).get("day_change_pct")
    if v is not None and last_updated:
        return float(v), str(last_updated)[:10]
    try:
        from openinvest.utils.exchange_fee import get_history_data

        closes = get_history_data(str((holding or {}).get("yfinance_proxy") or symbol), "5d")["Close"].dropna()
        if len(closes) < 2 or not closes.iloc[-2]:
            return None, None
        pct = (float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1.0) * 100.0
        return (pct, closes.index[-1].strftime("%Y-%m-%d")) if math.isfinite(pct) else (None, None)
    except Exception as e:  # noqa: BLE001  行情代理挂了只让日涨跌规则"无法判定"，不拖垮其它规则
        log.warning("[%s] 今日涨跌拉取失败: %s", symbol, e)
        return None, None


def evaluate_symbol(
    symbol: str,
    alerts: Mapping[str, Any],
    holding: Optional[Dict[str, Any]],
    *,
    price: Optional[float],
    extra: Optional[Mapping[str, Any]] = None,
    last_updated: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """某标的的全部已设规则求值（calc.price_alerts.evaluate 的结果）。

    price / extra / last_updated 由调用方给（哨兵刚拉的 quote；status 已拉过的持仓价），本函数只在设了
    日涨跌规则且 quote 没带时才补拉日涨跌。drawdown_from_cost 只对有成本的在持仓位判（追踪仓 / 零仓位无成本 → 无法判定）。
    """
    avg_cost = None
    if holding and not holding.get("is_tracking_only") and float(holding.get("units") or 0) > 0:
        avg_cost = float(holding.get("avg_cost") or 0) or None
    day, bar = _day_change(symbol, holding, extra or {}, last_updated) if any(
        alerts.get(r) is not None for r in _DAY_RULES) else (None, None)
    return evaluate(alerts, price=price, avg_cost=avg_cost, day_change_pct=day, day_bar=bar)
