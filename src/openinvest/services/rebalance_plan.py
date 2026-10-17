"""再平衡清单服务层（IO 壳）：strategy.md 的目标配置 + 当前持仓 → 该买 / 卖多少。

只出清单，不下单——人执行。计算体在 calc/rebalance.py（纯函数），本层只做取数：
- 持仓 / 现金 / 策略：PortfolioManager
- 现价：utils.quotes.get_quote（黄金 / 场外基金 / yfinance 同一入口）
- 总资产：utils.fx.total_portfolio_value_cny（全仓唯一实现，占比分母与 status 同口径）
- 现金折算：utils.fx.cash_total_in_base（与总资产的现金腿同口径，净负现金不截断）；基准币种：utils.base_currency

口径：
- 参与再平衡的是 strategy.target_assets 里的标的；持仓里有但不在 target_assets 的，
  只计入总资产（占比分母），列在 unmanaged，不出买卖建议。
- 每个标的的目标权重 = target_assets[i].target_pct（小数）；整份都没填的标的把
  target_allocation_stock 平分，行里 target_source = "equal_split" 标注。
- 不考虑 max_single_invest_cny（那是委员会单次入场闸，不是再平衡约束）——清单金额大于它时分批执行。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from openinvest.calc.rebalance import MODES, infer_lot_size, plan_rebalance
from openinvest.calc.regime_probability import quote_currency_iso
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from openinvest.utils import fx
from openinvest.utils.base_currency import base_currency
from openinvest.utils.quotes import get_quote, is_eastmoney_fund

DISCLAIMER = "只出清单，不下单，由你执行。数量按整手向下取整；价格是参考现价，成交以券商为准。"


def _pseudo_holding(symbol: str) -> Dict[str, Any]:
    """target_assets 里有、持仓里没有的标的：造一个 0 持仓壳子，让它也能取价、被建议买入。"""
    return {
        "symbol": symbol, "units": 0.0,
        # kind 决定整手：加密 / 贵金属按金额，写死 equity 会让 BTC-USD、GC=F 按 1 整枚 / 1 盎司取整而永远买不了
        "kind": "fund" if symbol.upper().startswith("FUND:") else _guess_kind_from_symbol(symbol),
        "cost_currency": quote_currency_iso(symbol) or "CNY",
    }


def _target_pct(asset: Dict[str, Any], warnings: List[str]) -> Optional[float]:
    v = asset.get("target_pct")
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = -1.0
    if not 0.0 <= f <= 1.0:
        warnings.append(f"{asset.get('symbol')} 的 target_pct={v!r} 不是 0~1 的小数，已忽略（按平分处理）")
        return None
    return f


def build_rebalance_plan(
    mode: str = "hybrid", band_pct: float = 5.0, *, pm: Optional[PortfolioManager] = None,
) -> Dict[str, Any]:
    """算再平衡清单。mode: cash_only / sell_to_rebalance / hybrid；band_pct: 漂移带（pp，默认 5）。"""
    if mode not in MODES:
        return {"status": "error", "error": f"mode 必须是 {'/'.join(MODES)} 之一，收到 {mode!r}"}
    try:
        band = float(band_pct)
    except (TypeError, ValueError):
        band = -1.0
    if not 0.0 <= band <= 100.0:
        return {"status": "error", "error": f"band_pct 必须在 0~100 之间（百分点），收到 {band_pct!r}"}
    if pm is None:
        try:
            pm = PortfolioManager()
        except FileNotFoundError as e:
            return {"status": "error", "error": str(e),
                    "hint": "memory 还没初始化。先跑 `run.sh init` 完成 onboarding。"}

    strat = pm.strategy.metadata
    assets = [a for a in (strat.get("target_assets") or []) if str(a.get("symbol") or "").strip()]
    try:
        stock_t, cash_t = float(strat["target_allocation_stock"]), float(strat["target_allocation_cash"])
    except (KeyError, TypeError, ValueError):
        return {"status": "error", "error": "strategy.md 缺 target_allocation_stock / target_allocation_cash",
                "hint": "用 set_allocations 设股票 / 现金目标比例"}
    if not assets:
        return {"status": "error", "error": "strategy.md 没有 target_assets，没有目标配置可对照",
                "hint": "用 track_asset 加跟踪标的"}

    base = base_currency()
    held = {str(h["symbol"]): h for h in pm.holdings if h.get("symbol")}
    managed = {str(a["symbol"]).strip(): a for a in assets}
    # 要取价的：所有在手持仓（含不在 target_assets 的，总资产要用）+ 没持仓的目标标的
    to_quote = {s: h for s, h in held.items() if not h.get("is_tracking_only") and float(h.get("units") or 0) > 0}
    for s in managed:
        to_quote.setdefault(s, held.get(s) or _pseudo_holding(s))

    warnings: List[str] = []
    prices: Dict[str, float] = {}
    for s, h in to_quote.items():
        q = get_quote(h)
        if q is not None and math.isfinite(q.price) and q.price > 0:
            prices[s] = q.price
            if s not in held and q.currency:   # 没持仓的标的：币种以行情为准（基金 / 黄金报 CNY），不靠后缀猜
                h["cost_currency"] = q.currency.upper()
            if q.is_stale:
                warnings.append(f"{s} 行情已过期（{q.last_updated or '日期未知'}），数量按过期价估算")

    total, status = fx.total_portfolio_value_cny(pm, prices, base=base)
    if not total > 0:
        return {"status": "error", "error": "总资产为 0（拉不到价格 / 汇率，或账户为空），无法算占比",
                "hint": "先跑 `run.sh status` 看哪些标的缺价"}
    for s in fx.unvalued(status):
        if s not in managed:   # 目标标的在下面统一报（未计入 + 剔除）
            warnings.append(f"{s} 拉不到价格或汇率，未计入总资产；占比以其余部分为准")
    # 现金不用 dry_powder：它把净负现金截到 0，而 total 按原值计入分母，两边会对不上
    cash_nonzero = {c: float(a) for c, a in pm.cash.items() if a}
    cash_value, cash_rates = fx.cash_total_in_base(cash_nonzero, base)
    for c in sorted(c for c, r in cash_rates.items() if r is None):
        warnings.append(f"{c} 现金拉不到汇率，未计入")

    positions: List[Dict[str, Any]] = []
    meta: Dict[str, Dict[str, Any]] = {}
    for s, a in managed.items():
        h = to_quote[s]
        units = 0.0 if h.get("is_tracking_only") else float(h.get("units") or 0)
        ccy = str(h.get("cost_currency") or "CNY").upper()
        rate = fx.get_fx_rate(ccy, base)
        if units > 0:
            st = status.get(s)
            px = prices.get(s) if st == "ok" else (float(h.get("avg_cost") or 0) if st == "cost_fallback" else None)
            value = units * px * rate if px and rate else None
            if st == "cost_fallback":
                warnings.append(f"{s} 无现价，按成本价计入总资产，不出买卖数量")
        else:
            value = 0.0
        if value is None:
            warnings.append(f"{s} 拉不到价格或汇率，未计入总资产，已从清单剔除")
            continue
        lot, lot_note = infer_lot_size(s, str(h.get("kind") or ""), by_amount=is_eastmoney_fund(h))
        positions.append({
            "symbol": s, "value": value, "target_pct": _target_pct(a, warnings),
            "price": prices.get(s) if status.get(s) != "cost_fallback" else None,
            "fx": rate, "lot_size": lot,
        })
        meta[s] = {"name": a.get("display_name") or h.get("display_name") or s, "currency": ccy,
                   **({"lot_note": lot_note} if lot_note else {})}

    if not positions:
        return {"status": "error", "error": "target_assets 里的标的都拉不到价格 / 汇率", "warnings": warnings}
    plan = plan_rebalance(positions, cash=cash_value, total=total, target_stock_pct=stock_t,
                          target_cash_pct=cash_t, band_pct=band, mode=mode, currency=base)
    for r in plan["rows"]:
        r.update(meta[r["symbol"]])
        if r["action"] != "NONE" and r.get("lot_note"):   # agent 常只转述 reason，手数不确定的提示要跟着走
            r["reason"] += f"；{r['lot_note']}"

    unmanaged = []
    for s, h in held.items():
        if s in managed or h.get("is_tracking_only") or s not in status or status[s] not in ("ok", "cost_fallback"):
            continue
        rate = fx.get_fx_rate(str(h.get("cost_currency") or "CNY").upper(), base)
        px = prices.get(s) if status[s] == "ok" else float(h.get("avg_cost") or 0)
        v = float(h.get("units") or 0) * px * rate if rate else 0.0
        unmanaged.append({"symbol": s, "name": h.get("display_name") or s,
                          "current_pct": round(v / total * 100, 2), "note": "不在 target_assets，只计入总资产，不出买卖建议"})

    return {
        "status": "ok", "mode": mode, "band_pct": band, "base_currency": base,
        "total_value": round(total, 2), "rows": plan["rows"], "totals": plan["totals"],
        "unmanaged": unmanaged, "warnings": plan["warnings"] + warnings, "note": DISCLAIMER,
    }
