"""loaders — sentiment / valuation / insights / 默认组合摘要 loader（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.exchange_fee import get_history_data

log = logging.getLogger(__name__)

def load_prior_insights(asset: Dict[str, Any], pm: Optional[PortfolioManager] = None) -> str:
    """读 memory/insights/*.md → Dreaming 长期行为模式

    历史漂移背景（2026-05-16）: 这段代码原本在 jobs/daily_report.py:_gather_relevant_insights
    和 scripts/skill.py:_gather_relevant_insights 各有一份完全相同的副本，且
    run_committee_for_symbol（service layer）从来没调，导致 Web/GUI 路径的 LLM
    永远看不到 Dreaming long-term insights。统一提到这里作为 shared loader 后，
    三路径自动同步。

    Args:
        asset: strategy.target_assets 单项（dict 含 symbol/display_name 等）
        pm: PortfolioManager 实例（不传则现 new 一个，复用 store.root 路径）

    Returns:
        所有匹配 insights 拼接的 markdown，空字符串表示无相关。
    """
    try:
        if pm is None:
            pm = PortfolioManager()
        store = pm.store
        insights_dir = store.root / "insights"
        if not insights_dir.exists():
            return ""
        sym = asset.get("symbol", "").lower().replace("=", "_")
        matches = []
        for f in sorted(insights_dir.glob("*.md")):
            if sym in f.stem.lower() or any(
                tok in f.stem.lower() for tok in ["gold", "ndq"] if tok in sym
            ):
                matches.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')[:600]}")
        return "\n\n".join(matches)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_prior_insights graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def load_sentiment_brief(event_brief: str = "") -> str:
    """市场情绪表盘 shared loader（确定性：VIX 分位保底 + CNN 锦上添花）。

    市场级跨资产共享 loader：session 跑一次（VIX/CNN 是市场级、跨资产相同），
    结果注入每个 run_committee(..., sentiment_brief=...)。

    任何失败 graceful 退化空字符串，不阻断 committee。CNN 不可达时 VIX 分位照常输出
    （绝不单点故障，见 utils/sentiment.py 设计红线）。
    """
    try:
        from openinvest.utils.sentiment import build_sentiment_brief
        return build_sentiment_brief(event_brief)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_sentiment_brief graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def load_valuation_brief(
    symbol: str, price_quantile_2y: Optional[float] = None,
) -> str:
    """估值 shared loader（确定性：trailing PE + 价格分位，仅权益类，per-asset）。

    黄金/商品类返回 ""（它们的"基本面"=货币因素走 Macro）。任何失败 graceful 退化 ""。
    """
    try:
        from openinvest.utils.valuation import build_valuation_brief
        return build_valuation_brief(symbol, price_quantile_2y)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_valuation_brief graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def _build_default_portfolio_summary(pm: PortfolioManager) -> str:
    """service layer 默认 portfolio_summary 拼装（含集中度 + 总资产 + 浮盈）

    2026-05-19 修复：Direct 路径（scripts/skill.py:cmd_run_committee）调
    run_committee_session 时**没传** portfolio_summary_override，service layer
    之前自拼的简化版只 4 行，缺总资产 / 缺所有持仓 / 缺集中度数字。Risk Officer
    自己算集中度连续 6 天错（真实 33.4% → 算成 81.6%），推荐错误"减仓"。

    现在默认行为：
    1. 遍历 pm.holdings 一次性拉所有实仓 current_prices（5d yfinance close）。
       N 个资产 = N 次 get_history_data，多数用户 ≤5 个 holding，总耗时 < 5s。
    2. 用 utils.fx.total_portfolio_value_cny 多币种折算总资产。
    3. 调 utils.portfolio_summary.portfolio_summary_text 拼完整版（与 cron 路径
       daily_report.py 用同一个 helper）。

    cron 路径仍走 portfolio_summary_override（daily_report.py 自己拼的版本
    包含 data_warnings 陈旧告警，service layer 拿不到那些）。

    Args:
        pm: PortfolioManager 实例

    Returns:
        portfolio_summary 完整文本（含集中度数字）。任何子步骤失败都 graceful
        退化到旧版精简版，不阻断 committee 主流程。
    """
    try:
        from openinvest.utils.exchange_fee import get_history_data
        from openinvest.utils.fx import total_portfolio_value_cny
        from openinvest.utils.portfolio_summary import portfolio_summary_text

        # 一次性拉所有实仓 current_prices。
        # 黄金 GC=F 必须走 get_gold_snapshot 反推 spot_cny_per_gram（与持仓的
        # unit=克、cost_currency=CNY 同口径）；直接用 get_history_data("GC=F")
        # 返回的是 USD/oz，会让市值算错百倍。其他 yfinance symbol 走 5d close。
        current_prices: Dict[str, float] = {}
        for h in pm.holdings:
            if h.get("is_tracking_only"):
                continue
            sym = str(h.get("symbol") or "")
            if not sym:
                continue
            units = float(h.get("units", 0) or 0)
            if units <= 0:
                continue
            try:
                if sym == "GC=F":
                    # 黄金：克价 in CNY（与持仓 cost_currency=CNY 同口径，
                    # 避免 USD/oz × FX 算错百倍）
                    from openinvest.utils.gold_price import get_gold_snapshot
                    snap = get_gold_snapshot(offset_pct=0.0)
                    if snap is not None:
                        current_prices[sym] = float(snap.spot_cny_per_gram)
                else:
                    df = get_history_data(sym, "5d")
                    if df is not None and not df.empty:
                        # 当日 close=NULL（yfinance 收盘前半成型 bar）会读成 NaN：
                        # NaN 不入 current_prices（belt-and-suspenders，下游 fx 已防）。
                        c = float(df["Close"].iloc[-1])
                        if math.isfinite(c):
                            current_prices[sym] = c
            except Exception as e:  # noqa: BLE001
                log.warning(
                    f"_build_default_portfolio_summary: {sym} 价拉取失败已跳过: "
                    f"{type(e).__name__}: {e}"
                )

        total_cny, _status = total_portfolio_value_cny(pm, current_prices, base="CNY")
        return portfolio_summary_text(pm, total_cny, current_prices)
    except Exception as e:  # noqa: BLE001
        # 兜底：拉价/折算彻底失败 → 用历史简化版，至少 Risk Officer 还有点上下文
        log.warning(
            f"_build_default_portfolio_summary 失败，退化到精简版: "
            f"{type(e).__name__}: {e}"
        )
        cash_cny = pm.cash_amount("CNY")
        aud_cash = pm.cash_amount("AUD")
        risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
        return (
            f"用户风险偏好: {risk_level}\n"
            f"CNY 现金: ¥{cash_cny:,.0f}（可投 ¥{max(0.0, cash_cny):,.0f}）\n"
            f"AUD 现金: ${aud_cash:,.0f}\n"
            f"⚠️ portfolio_summary 完整版构建失败，请勿据此做集中度判断"
        )

__all__ = [
    "load_prior_insights",
    "load_sentiment_brief",
    "load_valuation_brief",
    "_build_default_portfolio_summary",
]
