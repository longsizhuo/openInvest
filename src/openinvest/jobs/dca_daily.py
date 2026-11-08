"""每日定投跟踪投影—— 不把未观察的第三方成交写成真实持仓。

按 config.dca 配置和行情记录独立 tracking_estimate。系统看不到第三方平台真实成交，
因此不能据估算增加正式持仓、成本或外部现金流；真实余额与逐笔成交另行确认。

幂等（ADR-016）：每个 (date, symbol, plan) 一份不可变投影；重跑保留首次报价，
不重复写入、不更新正式账本。新计划是另一个跟踪版本，不是多一笔真实成交。

休市闸：行情最新 bar 日期 ≠ 今天（北京日期）→ 不动账本：行情源本次没拉到（is_stale）
→ skip(stale_quote) + warning；拉到了但没有今天的 bar → skip(market_closed)。两者都
没有完成收据，yml 一天排 15:30/18:30/21:30 三次，交易日 bar 晚到/行情源抖动由后两次补记，
已记录的后续运行走 already_projected_this_plan_today。2026-10 前不看开没开盘：国庆 / 中秋 / 周六按
节前旧价记了幻影买入（见 scripts/reconcile_dca_phantom.py 对账）。
适用范围：北京时间 15:30 前已收盘的市场（A 股、亚太）。15:30 后才收盘的市场（美股、欧股）
不受支持：多数时刻 skip(market_closed)，但 21:30 档恰逢美东开盘、欧股盘中时可能按盘中价记账。

安全默认：config.dca.auto_dca_enabled 默认 False → fork 用户 / 未配置时本 job 直接
skip，绝不自动动账本。用户经 /api/config（dca.auto_dca_enabled）或 INVEST_DCA_* 开启。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from openinvest.core.config import load_config
from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import _file_lock
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from openinvest.utils.quotes import get_quote

log = logging.getLogger(__name__)


class _DcaSkip(Exception):
    """定投前置条件不满足（未持有 / 取不到价 / 行情旧 / 休市 / 无汇率 / 金额太小）→ 跳过该 symbol。"""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _holding_for_quote(pm: PortfolioManager, symbol: str) -> Optional[Dict[str, Any]]:
    """取价使用已有跟踪行的 proxy_kind/cost_currency/unit_label，不推断实际份额。

    未持有 → None：DCA **不猜币种**——否则会把 USD/AUD 计价的价当 CNY 记账（单位错 +
    币种错，~汇率倍偏差），且绕过黄金 gold_cny_per_gram proxy。定投新标的前先把它加成
    holding（可 is_tracking_only），让 cost_currency / proxy_kind 正确。
    """
    existing = pm.find_holding(symbol)
    return dict(existing) if existing else None


def _positive(value) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(value) and value > 0
    except (TypeError, ValueError):
        return False


def _validate_estimate(estimate, *, symbol, day, plan):
    identity = {"symbol": symbol, "date": day, "plan": plan, "kind": "tracking_estimate"}
    quote = estimate.get("quote")
    if (estimate.get("identity") != identity or estimate.get("estimate_id") != digest(identity)
            or not isinstance(quote, dict) or not all(key in quote for key in
                ("projected_quantity", "price", "currency", "amount_cny", "quote_date"))
            or not all(_positive(quote.get(key)) for key in ("projected_quantity", "price", "amount_cny"))
            or quote.get("amount_cny") != plan["amount_cny"] or not quote.get("currency")
            or quote.get("quote_date") not in (None, day)
            or estimate.get("purpose") != "tracking_projection_not_confirmed_trade"
            or any(estimate.get(key) is not False for key in
                ("execution_observed", "portfolio_mutated", "cash_flow_recorded"))
            or estimate.get("actual_quantity") is not None or estimate.get("actual_fee") is not None):
        raise ValueError("tracking_estimate_receipt_requires_reconciliation")
    recorded_at = datetime.fromisoformat(str(estimate.get("recorded_at") or "").replace("Z", "+00:00"))
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("tracking_estimate_receipt_requires_reconciliation")
    return estimate


def _completed_estimate(store, *, symbol, day, plan):
    """Only a complete immutable receipt means done; legacy claims do not.

    Read under the writer's file lock. A missing file permits normal preparation;
    an invalid file is neither completion nor permission to replace its contents.
    """
    identity = {"symbol": symbol, "date": day, "plan": plan, "kind": "tracking_estimate"}
    path = store.root / ".evidence/tracking_estimates" / (digest(identity) + ".json")
    with _file_lock(path):
        if not path.exists():
            return None
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(envelope, dict) or envelope.get("schema") != 1
                or not isinstance(envelope.get("payload"), dict)
                or envelope.get("sha256") != digest(envelope["payload"])):
            raise ValueError("tracking_estimate_receipt_requires_reconciliation")
        return _validate_estimate(envelope["payload"], symbol=symbol, day=day, plan=plan)


def _result(symbol, estimate, *, replayed):
    quote = estimate["quote"]
    return {"symbol": symbol, "status": "skipped" if replayed else "estimated",
            **({"reason": "already_projected_this_plan_today"} if replayed else {}),
            "estimate_id": estimate["estimate_id"], "projected_quantity": quote["projected_quantity"],
            "price": quote["price"], "currency": quote["currency"], "amount_cny": quote["amount_cny"],
            "execution_observed": False, "portfolio_mutated": False}


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
    if not math.isfinite(amount_cny) or amount_cny <= 0:
        return {"status": "skipped", "reason": "non_positive_amount"}

    pm = PortfolioManager()
    # 北京日期：job 按 Asia/Shanghai 收盘后跑，机器 TZ 可能是 UTC；收据与休市闸同一口径
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        plan={'symbol':symbol,'amount_cny':amount_cny,'purpose':'tracking_projection'}
        # No early claim can survive a crash and impersonate a completed receipt.
        try:
            completed = _completed_estimate(pm.store, symbol=symbol, day=today, plan=plan)
            if completed is not None:
                results.append(_result(symbol, completed, replayed=True))
                continue
            holding = _holding_for_quote(pm, symbol)
            if holding is None:
                raise _DcaSkip("not_tracked")
            snap = get_quote(holding)
            if snap is None or not _positive(snap.price):
                raise _DcaSkip("no_price")
            # 休市闸：最新 bar 不是今天 → 按旧价记账就是幻影买入。先分清是行情源挂了
            # （stale_quote，交易日也可能，告警）还是确实没开盘（market_closed）。
            # ponytail: 按北京日期比——15:30 后收盘的市场不受支持（见模块 docstring），
            # 真要定投这类标的再改成按交易所时区 + 收盘时刻比对。
            # last_updated=None（黄金 proxy 不带日期）无从判断，照旧记账。
            if snap.last_updated is not None and snap.last_updated != today:
                if snap.is_stale:
                    log.warning(f"[DCA] {symbol} 行情源未拉到新数据（最新 bar {snap.last_updated}），"
                                f"本次跳过，当日后续运行重试")
                    raise _DcaSkip("stale_quote")
                raise _DcaSkip("market_closed")
            ccy = (snap.currency or "CNY").upper()
            if ccy == "CNY":
                amount_local = amount_cny
            else:
                from openinvest.utils.fx import to_base
                amount_local = to_base("CNY", amount_cny, ccy)
                if amount_local is None or not math.isfinite(amount_local) or amount_local<=0:
                    raise _DcaSkip("no_fx")
            units = round(amount_local / snap.price, 6)
            if not math.isfinite(units) or units <= 0:
                raise _DcaSkip("amount_too_small")
            price = snap.price
            kind = holding.get("kind") or _guess_kind_from_symbol(symbol)
            unit_label = str(holding.get("unit_label", "股"))
        except _DcaSkip as skip:
            results.append({"symbol": symbol, "status": "skipped", "reason": skip.reason})
            continue
        except Exception as e:  # noqa: BLE001 — 单 symbol 异常不该中断整批
            results.append({"symbol": symbol, "status": "error", "reason": str(e)})
            log.warning(f"[DCA] {symbol} 准备或收据核对失败，已跳过: {e}")
            continue

        # Only this audit projection is written. There is no buy transaction.
        try:
            from openinvest.services.ledger_evidence import record_tracking_estimate
            estimate=record_tracking_estimate(pm.store,symbol=symbol,day=today,plan=plan,
                quote={'projected_quantity':units,'price':price,'currency':ccy,'unit_label':unit_label,
                       'kind':kind,'quote_date':snap.last_updated,'amount_cny':amount_cny})
            _validate_estimate(estimate, symbol=symbol, day=today, plan=plan)
        except Exception as e:  # noqa: BLE001
            # A complete projection receipt is idempotent; a corrupt or unknown
            # receipt is not permission to invent an observed execution.
            results.append({"symbol": symbol, "status": "error", "reason": str(e)})
            log.warning(f"[DCA] {symbol} 跟踪投影记录失败: {e}")
            continue

        # The committed estimate is the response authority even after a retry.
        results.append(_result(symbol, estimate, replayed=estimate.get("replayed") is True))
        log.info('[DCA] %s 跟踪投影已保存；没有记录实际成交或资金流',symbol)

    return {"status": "success", "date": today, "results": results}


if __name__ == "__main__":
    print(run())
