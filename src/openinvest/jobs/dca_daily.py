"""每日自动定投（DCA）—— 子弹池模型（见 ADR-018）

按 config.dca 配置，对每个 symbol 记一笔 **external_funding** 买入：日定投的钱来自
京东/银行卡（工资），不是 portfolio 抄底子弹池现金 → 不扣 cash。本系统看不到京东
真实成交，按 amount_cny 估算每日买入量记账，月度用真实基金余额对账校准。

幂等（ADR-016）：每个 (date, symbol) 一把 state_claim 闸，杜绝 cron 重跑 / 手动重触发
二次记账。**只在 buy() 落盘前的异常才 unclaim**（取价/算量/买入失败可重试）；buy 一旦
提交，后续记账不再 unclaim，防「已买 → 后续报错 → unclaim → 次日重买」二次记账。

安全默认：config.dca.auto_dca_enabled 默认 False → fork 用户 / 未配置时本 job 直接
skip，绝不自动动账本。用户经 /api/config（dca.auto_dca_enabled）或 INVEST_DCA_* 开启。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from openinvest.core.config import load_config
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from openinvest.utils.quotes import get_quote

log = logging.getLogger(__name__)


class _DcaSkip(Exception):
    """定投前置条件不满足（未持有 / 取不到价 / 无汇率 / 金额太小）→ 跳过该 symbol。"""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _holding_for_quote(pm: PortfolioManager, symbol: str) -> Optional[Dict[str, Any]]:
    """取价用的 holding：**必须是真实持仓**（含正确 proxy_kind/cost_currency/unit_label）。

    未持有 → None：DCA **不猜币种**——否则会把 USD/AUD 计价的价当 CNY 记账（单位错 +
    币种错，~汇率倍偏差），且绕过黄金 gold_cny_per_gram proxy。定投新标的前先把它加成
    holding（可 is_tracking_only），让 cost_currency / proxy_kind 正确。
    """
    existing = pm.find_holding(symbol)
    return dict(existing) if existing else None


def run() -> Dict[str, Any]:
    # _force_reload：scheduler 是长驻进程，用户经 /api/config 改 dca 开关落盘在别的进程；
    # 不强制重读会命中本进程旧缓存 → 改了不生效。每日一次，重读成本可忽略。
    cfg = load_config(_force_reload=True)
    if not cfg.dca.auto_dca_enabled:
        return {"status": "skipped", "reason": "auto_dca_disabled"}

    symbols = list(cfg.dca.auto_dca_symbols)
    if not symbols:
        return {"status": "skipped", "reason": "no_dca_symbols"}

    amount_cny = float(cfg.dca.auto_dca_amount_cny)
    if amount_cny <= 0:
        return {"status": "skipped", "reason": "non_positive_amount"}

    pm = PortfolioManager()
    today = datetime.now().strftime("%Y-%m-%d")
    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        key = f"{today}:{symbol}"
        # 幂等闸：同日同 symbol 只记一次（claim 失败=已记过，跳过）
        if not pm.store.state_claim("dca_applied", key):
            results.append({"symbol": symbol, "status": "skipped",
                            "reason": "already_dca_today"})
            continue

        # —— 前置阶段：取价 + 算量。任何失败都在 buy 落盘前 → unclaim 可重试，不中断整批 ——
        try:
            holding = _holding_for_quote(pm, symbol)
            if holding is None:
                raise _DcaSkip("not_tracked")
            snap = get_quote(holding)
            if snap is None or snap.price is None or snap.price <= 0:
                raise _DcaSkip("no_price")
            ccy = (snap.currency or "CNY").upper()
            if ccy == "CNY":
                amount_local = amount_cny
            else:
                from openinvest.utils.fx import to_base
                amount_local = to_base("CNY", amount_cny, ccy)
                if not amount_local:
                    raise _DcaSkip("no_fx")
            units = round(amount_local / snap.price, 6)
            if units <= 0:
                raise _DcaSkip("amount_too_small")
            price = snap.price
            kind = holding.get("kind") or _guess_kind_from_symbol(symbol)
            unit_label = str(holding.get("unit_label", "股"))
        except _DcaSkip as skip:
            pm.store.state_unclaim("dca_applied", key)
            results.append({"symbol": symbol, "status": "skipped", "reason": skip.reason})
            continue
        except Exception as e:  # noqa: BLE001 — 单 symbol 异常不该中断整批
            pm.store.state_unclaim("dca_applied", key)
            results.append({"symbol": symbol, "status": "error", "reason": str(e)})
            log.warning(f"[DCA] {symbol} 准备阶段失败，已 unclaim 跳过: {e}")
            continue

        # —— 买入：失败 = tx 已回滚 → unclaim 可重试 ——
        try:
            pm.buy(symbol, units, price, currency=ccy, kind=kind,
                   unit_label=unit_label, source="dca_daily",
                   source_type="external_funding")
        except Exception as e:  # noqa: BLE001
            pm.store.state_unclaim("dca_applied", key)
            results.append({"symbol": symbol, "status": "error", "reason": str(e)})
            log.warning(f"[DCA] {symbol} 买入失败，已 unclaim: {e}")
            continue

        # buy 已提交 → 之后只读记账，**绝不再 unclaim**（防二次记账）
        results.append({"symbol": symbol, "status": "bought", "units": units,
                        "price": price, "currency": ccy, "amount_cny": amount_cny})
        log.info(f"📈 [DCA] {symbol} +{units} @ {price} {ccy}"
                 f"（≈¥{amount_cny:,.0f} 外部注资，子弹池现金不动）")

    return {"status": "success", "date": today, "results": results}


if __name__ == "__main__":
    print(run())
