"""通用行情接入（v2 重构产物）

设计：
- 把不同 proxy_kind 的行情拉取逻辑统一进 `get_quote(holding)` 接口
- daily_report / web_api / pnl_snapshot 都走这个统一入口，不再写死 NDQ/黄金分支
- 失败容忍：yfinance 挂时回落到 db/market_store 兜底（is_stale=True）

支持的 proxy_kind:
- `direct`     — 直接拉 holding.symbol（yfinance 兼容的所有 symbol）
- `gold_cny_per_gram` — GC=F + USDCNY=X 反推 CNY/克（浙商积存金这类）
- `fx_pair`    — 货币对（USDCNY=X / EURUSD=X 等）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from openinvest.utils.exchange_fee import get_history_data
from openinvest.utils.gold_price import get_gold_snapshot

log = logging.getLogger(__name__)


@dataclass
class QuoteSnapshot:
    """统一行情快照（任何 holding 拉到的行情都包成这个结构）

    字段语义：
    - price：单位价格，单位为 currency（如 CNY/克 → price=1031, currency=CNY, unit=克）
    - last_updated：行情日期 YYYY-MM-DD；None 表示数据源不提供时间戳
    - is_stale：来自 DB / 历史均值兜底（yfinance 挂或休市）
    - extra：proxy 特有的额外字段（金价的 bank_cny_per_gram、direct 的 day_change_pct 等）
    """
    symbol: str
    price: float
    currency: str
    unit: str
    last_updated: Optional[str] = None
    is_stale: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


def get_quote(holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """根据 holding 配置拉行情。失败返回 None（caller 自己决定怎么处理）

    holding 至少包含：
    - symbol: str
    - cost_currency: str
    - unit_label: str
    - proxy_kind: 'direct' | 'gold_cny_per_gram' | 'fx_pair'
    - yfinance_proxy: Optional[str]（不填则用 symbol）
    - price_offset_pct: Optional[float]（gold proxy 用）
    """
    symbol = str(holding.get("symbol", ""))
    proxy_kind = str(holding.get("proxy_kind") or "direct")
    proxy_symbol = str(holding.get("yfinance_proxy") or symbol)

    if proxy_kind == "gold_cny_per_gram":
        return _quote_gold(symbol, holding)
    if proxy_kind == "fx_pair":
        return _quote_fx(symbol, proxy_symbol, holding)
    # 默认 direct
    return _quote_direct(symbol, proxy_symbol, holding)


def _quote_gold(symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """黄金 CNY/克 反推（spot_cny_per_gram = GC=F USD/oz / 31.1035 * USDCNY）"""
    offset = float(holding.get("price_offset_pct") or 0.0)
    snap = get_gold_snapshot(offset_pct=offset)
    if snap is None:
        return None
    return QuoteSnapshot(
        symbol=symbol,
        price=snap.spot_cny_per_gram,
        currency="CNY",
        unit=holding.get("unit_label", "克"),
        last_updated=None,                     # gold_snapshot 不带日期
        is_stale=snap.is_stale,
        extra={
            "bank_cny_per_gram": snap.bank_cny_per_gram,
            "offset_pct": snap.offset_pct,
            "gold_usd_per_oz": snap.gold_usd_per_oz,
            "usdcny_rate": snap.usdcny_rate,
        },
    )


def _quote_fx(symbol: str, proxy_symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """货币对（USDCNY=X 等）。yfinance 直接给汇率"""
    try:
        df = get_history_data(proxy_symbol, "5d")
    except Exception as e:  # noqa: BLE001
        log.warning(f"fx_pair {proxy_symbol} 拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None
    return QuoteSnapshot(
        symbol=symbol,
        price=float(df["Close"].iloc[-1]),
        currency=str(holding.get("cost_currency", "")),
        unit="rate",
        last_updated=df.index[-1].strftime("%Y-%m-%d"),
    )


def _quote_direct(symbol: str, proxy_symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """yfinance 直接拉 5d 历史，取最新 close"""
    try:
        df = get_history_data(proxy_symbol, "5d")
    except Exception as e:  # noqa: BLE001  yfinance 抛各种网络异常都接住
        log.warning(f"direct {proxy_symbol} 行情拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    pct = (last / prev - 1) * 100 if prev else 0.0
    return QuoteSnapshot(
        symbol=symbol,
        price=last,
        currency=str(holding.get("cost_currency", "")),
        unit=str(holding.get("unit_label", "share")),
        last_updated=df.index[-1].strftime("%Y-%m-%d"),
        extra={
            "prev_close": prev,
            "day_change_pct": round(pct, 4),
        },
    )
