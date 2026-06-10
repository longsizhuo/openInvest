"""估值 brief（确定性，仅权益类）：trailing PE + 价格分位

对齐 TradingAgents「基本面分析师」维度，但**按资产类型分流**：

- **权益类（ETF/EQUITY/INDEX/基金）** → 出 valuation_brief：trailing PE（绝对水平判定）
  + 价格近 2 年分位（复用 regime/compute_metrics 已算的 price_quantile_2y）。
- **商品/期货/汇率/加密（如黄金 GC=F）** → 返回 ""。它们没有传统基本面，"基本面"=
  货币因素（DXY + 实际利率），走 Macro（见 utils/exchange_fee.get_macro_data），不在这里。

诚实边界：forward 盈利增速 / forward PE v1 不做 —— yfinance 对 ETF 的 forwardPE
返 null 不可靠，硬接会引入脏数据。v1 只给 trailing PE + 价格分位（都确定性可得）。

进决策的方式 = 确定性事实（像 regime_brief），Quant/CIO 必须纳入并说明"贵不贵"，
不是新增投票 agent。
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# yfinance quoteType 属于这些 → 视为权益类，出估值 brief
EQUITY_QUOTE_TYPES = {"ETF", "EQUITY", "MUTUALFUND", "INDEX"}
# PE 分档阈值 → core/config (valuation 节)，defaults.yaml 可调，
# env INVEST_VALUATION_<KEY> 可覆盖


def _pe_level(pe: float) -> str:
    from core.config import load_config
    cfg = load_config().valuation
    if pe >= cfg.pe_expensive:
        return "偏贵"
    if pe < cfg.pe_cheap:
        return "中性偏低"
    return "中性"


def build_valuation_brief(
    symbol: str, price_quantile_2y: Optional[float] = None,
) -> str:
    """组装确定性估值文本（仅权益类）。

    Args:
        symbol: yfinance ticker
        price_quantile_2y: compute_metrics 已算的价格百分位（0-1）；None 则跳过该行

    Returns:
        多行文本；非权益类 / 取不到任何字段 / 异常 → "" （graceful loader 契约）。
    """
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"valuation: {symbol} .info 拉取失败 graceful '': {type(e).__name__}: {e}")
        return ""

    quote_type = str(info.get("quoteType") or "").upper()
    if quote_type and quote_type not in EQUITY_QUOTE_TYPES:
        # 商品 / 期货 / 汇率 / 加密 → 不出估值（黄金等走 Macro 货币因素）
        return ""

    lines = []
    pe = info.get("trailingPE")
    if isinstance(pe, (int, float)) and not isinstance(pe, bool) and pe > 0:
        from core.config import load_config
        _pe_expensive = load_config().valuation.pe_expensive
        lines.append(
            f"VALUATION: trailing_PE={float(pe):.1f} "
            f"(绝对水平: {_pe_level(float(pe))}, 阈值 >{_pe_expensive:g}=偏贵)"
        )

    if price_quantile_2y is not None:
        try:
            lines.append(
                f"PRICE_QUANTILE_2Y: {float(price_quantile_2y) * 100:.0f}% "
                f"(价格在近2年的百分位，越高越贵)"
            )
        except (TypeError, ValueError):
            pass

    if not lines:
        # 权益类但 PE/分位都拿不到 → 不硬凑空 section
        return ""

    lines.append(
        "NOTE: forward 盈利增速 v1 不可得（yfinance ETF forwardPE=null），"
        "估值仅含 trailing PE + 价格分位"
    )
    return "\n".join(lines)


__all__ = ["build_valuation_brief"]
