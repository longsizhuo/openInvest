"""黄金期货与汇率的参考换算；不是银行牌价或伦敦现货报价。

公式：
    spot_cny_per_gram = (gold_usd_per_oz / 31.1035) * usdcny_rate
    bank_price = spot_cny_per_gram * (1 + offset_pct)

数据源：
- yfinance GC=F (COMEX 黄金期货 USD/oz) — XAUUSD=X 已被 Yahoo 下架
- yfinance USDCNY=X

Auto offset 推断：
- 每次用户在 NapCat 报当日实际买入克价 → 反算 offset_pct 写回 strategy.md
- 这样不用手动维护各家银行（浙商 / 工行 / 招行 / 建行 / 华安 ETF 等）的点差，
  系统自动学习用户实际渠道的溢价
"""
from __future__ import annotations

from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
import math

# 纯计算核已迁 calc 层（ADR-026）——导回保持历史导出面；本文件只剩 IO shell。
from openinvest.calc.gold import (  # noqa: F401
    GOLD_OZ_PER_GRAM,
    GoldPriceSnapshot as _GoldPriceSnapshot,
)
from openinvest.utils.market_observation import history_observation, provider_metadata, observation_now


@dataclass
class GoldPriceSnapshot(_GoldPriceSnapshot):
    gold_asof: Optional[str] = None
    fx_asof: Optional[str] = None
    price_basis: str = 'futures_fx_reference'
    source_observation: dict = field(default_factory=dict)


def format_gold_report(snap):
    """A futures/FX calculation is neither a London spot nor a bank quotation."""
    return (f"--- GOLD PRICE REFERENCE ---\n"
        f"COMEX 黄金期货参考 (GC=F): {snap.gold_usd_per_oz:.2f} USD/oz\n"
        f"USD/CNY参考: {snap.usdcny_rate:.4f}\n"
        f"期货×汇率换算参考: ¥{snap.spot_cny_per_gram:.2f}/g\n"
        f"渠道点差估算 (offset {snap.offset_pct:.2%}): ¥{snap.bank_cny_per_gram:.2f}/g\n"
        f"期货asof: {getattr(snap,'gold_asof',None) or 'unknown'}; "
        f"汇率asof: {getattr(snap,'fx_asof',None) or 'unknown'}\n"
        "非银行牌价或成交报价；换月、渠道点差及两腿不同步的影响未独立核验。")


def _get_db_fallback_snapshot(offset_pct: float) -> Optional[GoldPriceSnapshot]:
    """yfinance 都挂时从行情 DB 拿最近一条记录算克价，避免单点失败让黄金
    estimation 完全死掉（audit algo M7）"""
    try:
        from openinvest.db.market_store import MarketStore
        store = MarketStore()
        gold_usd = store.get_latest_price("GC=F")
        usdcny = store.get_latest_price("USDCNY=X")
        if (not gold_usd or not usdcny or not all(math.isfinite(float(value)) and float(value)>0
                                                for value in (gold_usd,usdcny))):
            return None
        spot = (float(gold_usd) / GOLD_OZ_PER_GRAM) * float(usdcny)
        bank = spot * (1 + offset_pct)
        return GoldPriceSnapshot(
            gold_usd_per_oz=float(gold_usd),
            usdcny_rate=float(usdcny),
            spot_cny_per_gram=spot,
            bank_cny_per_gram=bank,
            offset_pct=offset_pct,
            is_stale=True,
            source_observation={'provider':'cached_futures_fx','fetch_status':'cached',
                'freshness':'reference_only','bank_quote_observed':False,'quote_fresh':False,
                'received_at':None,'bar_asof':None,'quote_asof':None,'completed_session_dates':[],
                'legs':{sym:{'session_date':date if isinstance(date,str) else None,'quote_asof':None}
                        for sym in ('GC=F','USDCNY=X') for date in [store.get_latest_date(sym)]}},
        )
    except Exception as e:
        print(f"⚠️ 黄金 DB 兜底也失败: {e}")
        return None


def _cache_bars(store, symbol: str, df) -> None:
    """按每根 bar 自己的（交易所本地）日期落库，带 High/Low/Volume。

    2026-10 前用进程本地"今天"只写 close：美国假日、周末（FX ``=X`` 豁免幽灵周末闸）、
    日线未出前的盘中都会造出无 H/L 的行，ATR 退化成收盘价差。按 bar 日期写后：
    非交易日只刷新上一交易日已有行（upsert 保留 OHLCV），盘中行带当日 H/L。
    写整段 5d 而不只最后一根：今天这根带了 H/L 后 get_history_data 判"已最新"不再
    5d 刷新，前一交易日的盘中值要靠这里定稿。close=NaN 的半成型 bar 不落库（同
    exchange_fee 数据源闸）。
    """
    def num(v):
        return None if v is None or pd.isna(v) else float(v)

    for idx, row in df.iterrows():
        close = num(row.get("Close"))
        if close is None:
            continue
        store.save_generic_price(
            symbol, idx.strftime("%Y-%m-%d"), close, source="yfinance",
            high=num(row.get("High")), low=num(row.get("Low")),
            volume=num(row.get("Volume")),
        )


def get_gold_snapshot(offset_pct: float = 0.015) -> Optional[GoldPriceSnapshot]:
    """取得期货/汇率参考，保留各腿时间；不能把换算值当银行报价。

    offset_pct: 银行积存金/纸黄金渠道点差（默认 1.5%，由 strategy.md 的 auto
                推断值覆盖；用 /gold_offset 命令报当日实际买入克价让系统学习）

    数据通路（audit algo M7 加了 DB 兜底）：
    1. 主：yfinance GC=F + USDCNY=X 实时 → 按 bar 日期写 DB cache → 返回 fresh snapshot
    2. 兜底：yfinance 失败时从 DB 读最近一条，返回 is_stale=True 的 snapshot
    3. 都失败：返回 None
    """
    try:
        # 5d 而不是 1d：缓存时顺手把前几根定稿（见 _cache_bars）；现价仍取最后一根
        # 收盘前半成型 bar 的 Close 是 NaN：直读 yfinance 不经 exchange_fee 的入库闸，
        # 取 iloc[-1] 会把整个金价算成 NaN —— 先丢掉，现价取最后一根有效收盘
        gold_ticker, fx_ticker = yf.Ticker("GC=F"), yf.Ticker("USDCNY=X")
        gold_df = gold_ticker.history(period="5d").dropna(subset=["Close"])
        gold_received_at = observation_now()
        gold_meta = provider_metadata(gold_ticker)
        usdcny_df = fx_ticker.history(period="5d").dropna(subset=["Close"])
        fx_received_at = observation_now()
        fx_meta = provider_metadata(fx_ticker)
        if gold_df.empty or usdcny_df.empty:
            print("⚠️ 黄金数据为空，尝试 DB 兜底")
            return _get_db_fallback_snapshot(offset_pct)
        gold_usd = float(gold_df["Close"].iloc[-1])
        usdcny = float(usdcny_df["Close"].iloc[-1])
        if not all(math.isfinite(value) and value>0 for value in (gold_usd,usdcny)):
            return _get_db_fallback_snapshot(offset_pct)
        observed_at = observation_now()
        gold_source = history_observation(gold_df, metadata=gold_meta, now=observed_at,received_at=gold_received_at)
        fx_source = history_observation(usdcny_df, metadata=fx_meta,
                                        price_basis='fx_rate', now=observed_at,received_at=fx_received_at)
    except Exception as e:
        print(f"⚠️ 黄金 yfinance 拉取失败 ({e})，尝试 DB 兜底")
        return _get_db_fallback_snapshot(offset_pct)

    # 写 DB cache 给下次兜底用
    try:
        from openinvest.db.market_store import MarketStore
        _store = MarketStore()
        _cache_bars(_store, "GC=F", gold_df)
        _cache_bars(_store, "USDCNY=X", usdcny_df)
    except Exception as e:
        print(f"⚠️ 黄金 DB 写缓存失败（不影响本次返回）: {e}")

    spot = (gold_usd / GOLD_OZ_PER_GRAM) * usdcny
    bank = spot * (1 + offset_pct)
    return GoldPriceSnapshot(
        gold_usd_per_oz=gold_usd,
        usdcny_rate=usdcny,
        spot_cny_per_gram=spot,
        bank_cny_per_gram=bank,
        offset_pct=offset_pct,
        is_stale=not (gold_source['quote_fresh'] and fx_source['quote_fresh']),
        gold_asof=gold_source['quote_asof'], fx_asof=fx_source['quote_asof'],
        source_observation={'provider':'derived_futures_fx','fetch_status':'ok',
            'freshness':'fresh_reference' if gold_source['quote_fresh'] and fx_source['quote_fresh'] else 'reference_only',
            'bank_quote_observed':False,'quote_fresh':False,'received_at':observed_at.isoformat(),
            'bar_asof':None,'quote_asof':None,'completed_session_dates':[],
            'legs':{'GC=F':gold_source,'USDCNY=X':fx_source}},
    )


if __name__ == "__main__":
    snap = get_gold_snapshot()
    if snap:
        print(format_gold_report(snap))
    else:
        print("⚠️ 无法获取黄金数据")
