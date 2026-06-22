"""每日自动定投（DCA）—— 子弹池模型（见 ADR-018）

按 config.dca 配置，对每个 symbol 记一笔 **external_funding** 买入：日定投的钱来自
京东/银行卡（工资），不是 portfolio 抄底子弹池现金 → 不扣 cash。本系统看不到京东
真实成交，按 amount_cny 估算每日买入量记账，月度用真实基金余额对账校准。

幂等（ADR-016）：每个 (date, symbol) 一把 state_claim 闸，杜绝 cron 重跑 / 手动重触发
二次记账。买入失败则 unclaim，让同日可重试。

安全默认：config.dca.auto_dca_enabled 默认 False → fork 用户 / 未配置时本 job 直接
skip，绝不自动动账本。用户经 /api/config（dca.auto_dca_enabled）或 INVEST_DCA_* 开启。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from core.config import load_config
from core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from utils.quotes import get_quote

log = logging.getLogger(__name__)


def _holding_for_quote(pm: PortfolioManager, symbol: str) -> Dict[str, Any]:
    """取价用的 holding：优先真实持仓（带正确 proxy_kind/cost_currency），
    未持有则构造最小 direct holding（默认 CNY——A 股定投常见场景）。"""
    existing = pm.find_holding(symbol)
    if existing:
        return dict(existing)
    return {"symbol": symbol, "proxy_kind": "direct",
            "cost_currency": "CNY", "unit_label": "股"}


def run() -> Dict[str, Any]:
    cfg = load_config()
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
        try:
            holding = _holding_for_quote(pm, symbol)
            snap = get_quote(holding)
            if snap is None or snap.price is None or snap.price <= 0:
                pm.store.state_unclaim("dca_applied", key)
                results.append({"symbol": symbol, "status": "skipped",
                                "reason": "no_price"})
                continue

            ccy = (snap.currency or "CNY").upper()
            # amount_cny 折算到标的计价币种
            if ccy == "CNY":
                amount_local = amount_cny
            else:
                from utils.fx import to_base
                amount_local = to_base("CNY", amount_cny, ccy)
                if not amount_local:
                    pm.store.state_unclaim("dca_applied", key)
                    results.append({"symbol": symbol, "status": "skipped",
                                    "reason": "no_fx"})
                    continue

            units = round(amount_local / snap.price, 6)
            kind = holding.get("kind") or _guess_kind_from_symbol(symbol)
            pm.buy(symbol, units, snap.price, currency=ccy, kind=kind,
                   unit_label=str(holding.get("unit_label", "股")),
                   source="dca_daily", source_type="external_funding")
            results.append({"symbol": symbol, "status": "bought", "units": units,
                            "price": snap.price, "currency": ccy,
                            "amount_cny": amount_cny})
            log.info(f"📈 [DCA] {symbol} +{units} @ {snap.price} {ccy}"
                     f"（≈¥{amount_cny:,.0f} 外部注资，子弹池现金不动）")
        except Exception:
            # 账本没改成 → unclaim，让同日可重试这笔（avoid claim-but-not-applied）
            pm.store.state_unclaim("dca_applied", key)
            raise

    return {"status": "success", "date": today, "results": results}


if __name__ == "__main__":
    print(run())
