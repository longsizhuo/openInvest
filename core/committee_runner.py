"""端到端单资产委员会跑（live 端点用）

把 daily_report 的数据准备逻辑抽出来，让 web_api 能用 progress_callback 实时上报进度。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from core.committee import run_committee, run_macro_view
from core.portfolio_manager import PortfolioManager
from core.regime import format_regime_brief
from utils.exchange_fee import (
    analyze_multi_timeframe, get_history_data, get_macro_data,
)
from utils.market_metrics import compute_metrics

log = logging.getLogger(__name__)


def run_committee_for_symbol(
    symbol: str,
    *,
    max_debate_rounds: int = 4,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    shared_macro_view: Optional[str] = None,
) -> Dict[str, Any]:
    """端到端跑单资产 committee

    准备数据 → macro view（如果没传共享版） → multi-round Quant/Risk 辩论 → CIO → 持久化

    Args:
        shared_macro_view: 多资产场景下传共享 macro，避免每个资产都跑一次浪费 token
    """
    def emit(phase: str, **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            # asset 字段方便多资产 progress 区分
            progress_callback({"phase": phase, "asset": symbol, **extra})
        except Exception as e:  # noqa: BLE001
            log.warning(f"progress emit fail: {e}")

    emit("preparing", symbol=symbol)

    # 1. 拉 strategy 找 target
    pm = PortfolioManager()
    target = next(
        (a for a in pm.strategy.get("target_assets", []) if a.get("symbol") == symbol),
        None,
    )
    if target is None:
        emit("error", reason=f"asset {symbol} not in strategy.target_assets")
        return {"error": f"asset {symbol} not in strategy.target_assets"}

    # 2. 行情 + 指标 + regime
    try:
        df = get_history_data(symbol, "2y")
    except Exception as e:  # noqa: BLE001
        emit("error", reason=f"行情拉取失败: {e}")
        return {"error": f"行情拉取失败: {e}"}
    if df is None or df.empty:
        emit("error", reason=f"无 {symbol} 行情数据")
        return {"error": f"无 {symbol} 行情数据"}

    metrics = compute_metrics(df)
    market_data = analyze_multi_timeframe(
        df, f"{target.get('display_name', symbol)} ({symbol})",
    )
    # P1-2: 传 symbol 让 regime 用 per-asset 阈值（黄金/纳指/加密各异）
    regime_brief = format_regime_brief(metrics, symbol=symbol)
    emit("data_ready", regime_brief=regime_brief[:240])

    # 3. Macro view —— 共享版优先（多资产 orchestrator 已经跑过一次）
    if shared_macro_view is not None:
        macro_view = shared_macro_view
        emit("macro_done", macro_preview=macro_view[:240], shared=True)
    else:
        emit("macro_start")
        try:
            macro_data = get_macro_data()
        except Exception as e:  # noqa: BLE001
            log.warning(f"get_macro_data 失败: {e}")
            macro_data = {}
        macro_view = run_macro_view(str(macro_data))
        emit("macro_done", macro_preview=macro_view[:240])

    # 4. Portfolio summary（v2 通用 holdings 接口读）
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    h = pm.holdings.find(symbol)
    units = float(h.get("units", 0) or 0) if h else 0.0
    avg_cost = float(h.get("avg_cost", 0) or 0) if h else 0.0
    cost_ccy = h.get("cost_currency", "") if h else ""
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0) or 0)
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    dry_powder = max(0.0, cash_cny - buffer_cny)

    portfolio_summary = (
        f"用户风险偏好: {risk_level}\n"
        f"CNY 现金: ¥{cash_cny:,.0f}（应急金 ¥{buffer_cny:,.0f}，可投 ¥{dry_powder:,.0f}）\n"
        f"AUD 现金: ${aud_cash:,.0f}\n"
        f"目标资产 {symbol}: {units} 单位"
        + (f"，均价 {avg_cost} {cost_ccy}" if avg_cost else "（暂无持仓）")
    )

    # 5. 跑多轮辩论 + CIO
    return run_committee(
        target,
        market_data=market_data,
        macro_view=macro_view,
        portfolio_summary=portfolio_summary,
        regime_brief=regime_brief,
        max_debate_rounds=max_debate_rounds,
        progress_callback=progress_callback,
    )
