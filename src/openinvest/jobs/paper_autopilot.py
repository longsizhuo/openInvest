"""全自动模拟账户（paper autopilot）—— 把当日委员会裁决在本地账本上按收盘价确定性成交。

目的：验证"人是执行里最不稳定的因素"。同一套委员会、同一个模型、同一起点，一个账户由人执行，
一个由程序 100% 照做。所以这是**运行在独立 INVEST_HOME 里的 opt-in 执行层**：不下真单、不碰真实账户。
安全默认：config.autopilot.enabled 默认 False → 本 job 直接 skip（fork 用户 / 生产 hub 零影响）。

成交规则（裁决规则本身一字不改）：
  HOLD / UNCLEAR / 无 alloc   不动、不记 execution（和人一样"没事做"）
  ACCUMULATE / BUY            买 |alloc_cny| 等值，当日首个缓存 bar（通常为收盘价），整手向下取整，现金不足先 CNY 换汇再缩量
  TRIM                        卖 |alloc_cny| 等值（整手、≤持有量）
  SELL                        全部卖出
  场外基金（FUND:xxx）       不成交（T+1 两段式），记 executed=False / unsupported_otc_fund
  手续费 = 成交额 × commission_bps/1e4，用 ADR-027 的 fee 活动（record_fee）另记一行。

幂等（ADR-016，同 dca_daily）：每个 (date, symbol, side) 一把 state_claim；buy/sell/fee/fx 各带
idempotency_key；**账本写入一旦提交就不再 unclaim**（防"已成交 → 后续报错 → unclaim → 重跑二次成交"）。
只有"什么都没写"的前置失败（陈旧行情 / 休市 / 取价失败）才 unclaim，当日可重试。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from openinvest.calc.regime_probability import quote_currency_iso
from openinvest.calc.rebalance import infer_lot_size
from openinvest.core.config import load_config
from openinvest.core.decision_ledger import list_decisions, record_execution
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol, gold_booking
from openinvest.utils.quotes import get_quote, is_eastmoney_fund

log = logging.getLogger(__name__)

_BUY = ("ACCUMULATE", "BUY")
_SELL = ("TRIM", "SELL")
_CLAIM = "paper_autopilot"
_CASH_SLACK = 0.01   # 现金按分取整，留一分钱余量防 买入后 现金 < 手续费 差半分的边界


class _Skip(Exception):
    """前置条件不满足。terminal=True：判定已定（金额太小 / 没仓位 / 没钱），留 claim 并记 executed=False；
    False：什么都没写、条件可能自愈（陈旧行情 / 休市 / 取不到价 / 无汇率）→ unclaim 当日重试。
    record=False：terminal 但不写 execution 声明（duplicate_key：这笔其实已成交，写 False 会盖掉匹配出的 executed=True）。"""
    def __init__(self, reason: str, terminal: bool = False, record: bool = True):
        super().__init__(reason)
        self.reason, self.terminal, self.record = reason, terminal, record


def _lot(symbol: str, kind: str) -> Optional[int]:
    """整手股数；None = 可小数（金属 / 加密，保留 4 位）。港股按 100（infer_lot_size 对港股只给 1 并提示未知）。"""
    if symbol.upper().endswith(".HK"):
        return 100
    return infer_lot_size(symbol, kind)[0]


def round_down_units(units: float, lot: Optional[int]) -> float:
    """向下取整到整手；lot=None 向下取到 4 位小数。"""
    if lot is None:
        return math.floor(units * 1e4 + 1e-9) / 1e4
    return float(math.floor(units / lot + 1e-9) * lot)


def _holding_for(pm: PortfolioManager, symbol: str) -> Dict[str, Any]:
    """取价用 holding：有持仓用持仓（币种 / 单位 / proxy_kind 以账本为准）；没有则按 symbol 猜一个 0 持仓壳子。
    黄金 GC=F 走 gold_booking 的 CNY/克 口径；price_offset_pct 取自 strategy.target_assets。"""
    existing = pm.find_holding(symbol)
    if existing:
        return dict(existing)
    kind = "fund" if symbol.upper().startswith("FUND:") else _guess_kind_from_symbol(symbol)
    h: Dict[str, Any] = {"symbol": symbol, "kind": kind, "units": 0.0, "unit_label": "股",
                         "cost_currency": quote_currency_iso(symbol) or "CNY", "proxy_kind": "direct"}
    gold = gold_booking(symbol, "CNY", "克", None)   # 非 GC=F → None
    if gold:
        h.update({"cost_currency": "CNY", **gold})
    ta = next((t for t in (pm.strategy.get("target_assets") or []) if t.get("symbol") == symbol), {})
    if ta.get("price_offset_pct") is not None:
        h["price_offset_pct"] = ta["price_offset_pct"]
    return h


def _quote(holding: Dict[str, Any], today: str) -> tuple[float, str]:
    """(当日价, 币种)。必须有今天的 bar：行情源没拉到 → stale_quote；拉到了但最新 bar 不是今天 → market_closed
    （同 dca_daily 的休市闸，按北京日期比，所以美股 / 欧股收盘晚于北京 16:35 的市场不受支持）。
    黄金 last_updated=None（proxy 不带日期）无从判断，照旧成交。"""
    snap = get_quote(holding)
    if snap is None or snap.price is None or snap.price <= 0:
        raise _Skip("no_price")
    if snap.last_updated is not None and snap.last_updated != today:
        raise _Skip("stale_quote" if snap.is_stale else "market_closed")
    if snap.is_stale and snap.last_updated is None:
        raise _Skip("stale_quote")
    return float(snap.price), (snap.currency or holding.get("cost_currency") or "CNY").upper()


def _fx(ccy: str) -> float:
    if ccy == "CNY":
        return 1.0
    from openinvest.utils.fx import get_fx_rate
    r = get_fx_rate(ccy, "CNY")
    if not r or r <= 0:
        raise _Skip("no_fx")
    return float(r)


def _plan_buy(pm: PortfolioManager, holding: Dict[str, Any], price: float, ccy: str, fx: float,
              amount_cny: float, rate: float, min_order_cny: float) -> Dict[str, Any]:
    """算买入：units / cost / fee / 需从 CNY 换入的 ccy 金额。可用资金 = ccy 现金 + CNY 现金/汇率；
    买入连手续费不得超过它（现金不能为负），不够就缩量到整手。"""
    if amount_cny < min_order_cny:
        raise _Skip("too_small", terminal=True)
    lot = _lot(holding["symbol"], str(holding.get("kind") or ""))
    want = round_down_units(amount_cny / fx / price, lot)
    if want <= 0:
        raise _Skip("too_small", terminal=True)
    cash_ccy = pm.cash_amount(ccy)
    cny_avail = pm.cash_amount("CNY") if ccy != "CNY" else 0.0
    funds = cash_ccy + max(cny_avail - 0.05, 0.0) / fx
    budget = max(min(want * price, funds / (1 + rate) - _CASH_SLACK), 0.0)
    units = round_down_units(budget / price, lot)
    cost = units * price
    if units <= 0 or cost * fx < min_order_cny:
        raise _Skip("no_cash", terminal=True)
    fee = round(cost * rate, 2)
    topup = 0.0
    if cash_ccy < cost + fee:
        topup = math.ceil((cost + fee + _CASH_SLACK - cash_ccy) * 100) / 100   # 向上取到分
    return {"units": units, "cost": cost, "fee": fee, "topup": topup}


def _plan_sell(holding: Dict[str, Any], price: float, fx: float, verdict: str, amount_cny: float,
               min_order_cny: float) -> Dict[str, Any]:
    held = float(holding.get("units") or 0)
    if held <= 0:
        raise _Skip("no_position", terminal=True)
    if verdict == "SELL":
        units = held   # 清仓不受最小单限制，否则小尾巴永远清不掉
    else:
        if amount_cny < min_order_cny:
            raise _Skip("too_small", terminal=True)
        lot = _lot(holding["symbol"], str(holding.get("kind") or ""))
        units = min(round_down_units(amount_cny / fx / price, lot), held)
        if units <= 0:
            raise _Skip("too_small", terminal=True)
    return {"units": units, "cost": units * price}


def _alloc_cny(d: Dict[str, Any]) -> float:
    """决议里的 |alloc| 折成人民币。TRIM / SELL 的 alloc 是负数（方向），取量级；基准币种非 CNY 时按汇率换回。"""
    amt = abs(float(d.get("alloc_cny") or 0))
    ccy = str(d.get("alloc_currency") or "CNY").upper()
    return amt * _fx(ccy) if amt and ccy != "CNY" else amt


def _paper_home_error(pm: PortfolioManager) -> Optional[str]:
    """Enabling a job never converts the user's current ledger into a paper one."""
    origin_path = pm.store.root / ".autopilot_origin.json"
    try:
        origin = json.loads(origin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "paper_home_requires_independent_clone"
    if not isinstance(origin, dict) or not origin.get("cloned_at"):
        return "paper_home_requires_independent_clone"
    domain = pm.portfolio.get("ledger_domain")
    if domain not in (None, "paper_simulation"):
        return "paper_home_has_non_simulated_ledger_domain"
    from openinvest.core.decision_calc import execution_domain
    history = pm.store.read_history()
    if any(execution_domain(row) != "paper_simulation" for row in history):
        return "paper_home_has_non_simulated_history"
    paper_symbols = {row.get("symbol") for row in history}
    if domain is None and any(h.get("channel") != _CLAIM and h.get("symbol") not in paper_symbols
                              for h in pm.holdings):
        return "paper_home_has_non_simulated_holdings"
    return None


def _fx_recording_scope(pm: PortfolioManager, d: Dict[str, Any], today: str, *, rollback=False):
    return pm.recording_scope(
        evidence_basis="paper_simulated_fill", execution_domain="paper_simulation",
        source_reference={"kind": "fx_conversion_rollback" if rollback else "fx_conversion",
                          "producer": _CLAIM, "decision_id": d["decision_id"]},
        trade_date=today, fee_status="estimated",
    )


def _fill(pm: PortfolioManager, d: Dict[str, Any], today: str, cfg) -> Dict[str, Any]:
    """All fills, commissions and FX legs belong to the same paper domain."""
    with pm.recording_scope(
        evidence_basis="paper_simulated_fill", execution_domain="paper_simulation",
        source_reference=f"paper_autopilot:{d['decision_id']}", trade_date=today,
        fee_status="estimated",
    ):
        return _fill_simulated(pm, d, today, cfg)


def _fill_simulated(pm: PortfolioManager, d: Dict[str, Any], today: str, cfg) -> Dict[str, Any]:
    """成交一条决议；返回 filled 行。_Skip 由调用方处理。"""
    symbol, verdict = d["symbol"], d["verdict"]
    side = "buy" if verdict in _BUY else "sell"
    holding = _holding_for(pm, symbol)
    if symbol.upper().startswith("FUND:") or is_eastmoney_fund(holding):
        raise _Skip("unsupported_otc_fund", terminal=True)   # 场外基金 T+1 两段式确认，autopilot 不模拟
    price, ccy = _quote(holding, today)
    held_ccy = str(holding.get("cost_currency") or ccy).upper()
    if pm.find_holding(symbol) and held_ccy != ccy:
        raise _Skip("currency_mismatch", terminal=True)   # 买卖按持仓币种入账，报价币种不一致会记错现金 / 手续费
    fx = _fx(ccy)
    rate = float(cfg.commission_bps) / 1e4
    unit_label = str(holding.get("unit_label") or "股")
    kind = str(holding.get("kind") or _guess_kind_from_symbol(symbol))
    min_cny = float(cfg.min_order_cny)

    if side == "buy":
        plan = _plan_buy(pm, holding, price, ccy, fx, _alloc_cny(d), rate, min_cny)
        cny_out = math.ceil(plan["topup"] * fx * 100) / 100
        k = f"paper-fx-{today}-{symbol}-{ccy}"
        if plan["topup"] > 0:   # CNY 换汇补足：两笔各带幂等键（同 key 会被账本当重复，所以 -out / -in 分开）
            with _fx_recording_scope(pm, d, today):
                pm.withdraw_cash("CNY", cny_out, source=_CLAIM, idempotency_key=f"{k}-out")
                try:
                    pm.deposit_cash(ccy, plan["topup"], source=_CLAIM, idempotency_key=f"{k}-in")
                except Exception:
                    with _fx_recording_scope(pm, d, today, rollback=True):
                        pm.deposit_cash("CNY", cny_out, source=_CLAIM, idempotency_key=f"{k}-undo")
                    raise
        try:
            res = pm.buy(symbol, plan["units"], price, currency=ccy, kind=kind, unit_label=unit_label,
                         source=_CLAIM, idempotency_key=f"paper-{today}-{symbol}-buy")
        except Exception as e:  # noqa: BLE001
            if plan["topup"] > 0:   # 换汇已提交而买入没成：反向换回，别留下没有成交的现金结构。
                # 换汇键已烧，重试会被判重复 → 本次判终局（不 unclaim）
                log.warning(f"[autopilot] {symbol} 买入失败，回滚换汇: {e}")
                with _fx_recording_scope(pm, d, today, rollback=True):
                    pm.withdraw_cash(ccy, plan["topup"], source=_CLAIM, idempotency_key=f"{k}-rollback-out")
                    pm.deposit_cash("CNY", cny_out, source=_CLAIM, idempotency_key=f"{k}-rollback-in")
                raise _Skip("buy_failed", terminal=True) from e
            raise
    else:
        plan = _plan_sell(holding, price, fx, verdict, _alloc_cny(d), min_cny)
        plan["fee"] = round(plan["cost"] * rate, 2)
        res = pm.sell(symbol, plan["units"], price, source=_CLAIM,
                      idempotency_key=f"paper-{today}-{symbol}-sell")
    if res.get("status") == "duplicate":
        raise _Skip("duplicate_key", terminal=True, record=False)

    # —— 账本已提交：之后只做收尾记账，失败只告警，绝不 unclaim ——
    if plan["fee"] > 0:
        try:
            pm.record_fee(plan["fee"], ccy, symbol, trade_date=today, source=_CLAIM,
                          note=f"paper autopilot commission {cfg.commission_bps:g}bps",
                          idempotency_key=f"paper-fee-{today}-{symbol}-{side}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"[autopilot] {symbol} 手续费记账失败（成交已提交）: {e}")
    try:
        record_execution(d["decision_id"], True, reason=_CLAIM,
                         execution_domain="paper_simulation", evidence_basis="paper_simulated_fill",
                         source_reference=f"paper_autopilot:{d['decision_id']}")
    except Exception as e:  # noqa: BLE001
        log.warning(f"[autopilot] {symbol} record_execution 失败（成交已提交）: {e}")
    return {"symbol": symbol, "side": side, "verdict": verdict, "units": plan["units"], "price": price,
            "currency": ccy, "amount_cny": round(plan["cost"] * fx, 2), "fee_cny": round(plan["fee"] * fx, 2),
            "fx_topup": plan.get("topup", 0.0) or None,
            "execution_domain": "paper_simulation", "evidence_basis": "paper_simulated_fill"}


def run(today: Optional[str] = None) -> Dict[str, Any]:
    # _force_reload：scheduler 长驻，开关经 CLI/API 落盘在别的进程（同 dca_daily）
    cfg = load_config(_force_reload=True).autopilot
    if not cfg.enabled:
        return {"status": "skipped", "reason": "autopilot_disabled"}

    pm = PortfolioManager()
    home_error = _paper_home_error(pm)
    if home_error:
        return {"status": "error", "reason": home_error}
    if pm.portfolio.get("ledger_domain") is None:
        with pm.with_portfolio_tx() as portfolio:
            portfolio["ledger_domain"] = "paper_simulation"
        pm._reload()
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    # 只取今天的、还没有 execution 记录的方向性裁决；先卖后买（卖出回款可供当天买入），其余按 symbol 字母序，保证确定性
    todo = sorted(
        (d for d in list_decisions(days=3)
         if d["date"] == today and d.get("simulated_execution") is None and d["verdict"] in _BUY + _SELL),
        key=lambda d: (d["verdict"] in _BUY, d["symbol"]))
    filled: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for d in todo:
        symbol, verdict = d["symbol"], d["verdict"]
        if verdict in _BUY + ("TRIM",) and not abs(float(d.get("alloc_cny") or 0)):
            skipped.append({"symbol": symbol, "reason": "no_alloc"})   # 和 HOLD 一样没事做，不记 execution
            continue
        key = f"{today}:{symbol}:{'buy' if verdict in _BUY else 'sell'}"
        if not pm.store.state_claim(_CLAIM, key):
            skipped.append({"symbol": symbol, "reason": "already_done_today"})
            continue
        try:
            filled.append(_fill(pm, d, today, cfg))
            log.info(f"📒 [autopilot] {filled[-1]}")
        except _Skip as s:
            if s.terminal and not s.record:
                pass
            elif s.terminal:
                try:
                    record_execution(d["decision_id"], False, reason=f"{_CLAIM}:{s.reason}",
                                     execution_domain="paper_simulation", evidence_basis="paper_simulated_fill",
                                     source_reference=f"paper_autopilot:{d['decision_id']}")
                except Exception as e:  # noqa: BLE001
                    log.warning(f"[autopilot] {symbol} 记 skip 失败: {e}")
            else:
                pm.store.state_unclaim(_CLAIM, key)
            skipped.append({"symbol": symbol, "reason": s.reason})
        except Exception as e:  # noqa: BLE001 — 单 symbol 失败不中断整批
            # 前置阶段（取价 / 换汇 / buy 回滚）失败 → 账本没提交成交 → unclaim 可重试；
            # 若失败发生在 buy/sell 提交之后，_fill 内部已 try/except 吞掉，到不了这里。
            pm.store.state_unclaim(_CLAIM, key)
            skipped.append({"symbol": symbol, "reason": f"error: {e}"})
            log.warning(f"[autopilot] {symbol} 失败，已 unclaim: {e}")

    return {"status": "ok", "date": today, "filled": filled, "skipped": skipped,
            "fees_cny": round(sum(f["fee_cny"] for f in filled), 2)}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, default=str))
