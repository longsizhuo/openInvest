"""通用行情接入（v2 重构产物）

设计：
- 把不同 proxy_kind 的行情拉取逻辑统一进 `get_quote(holding)` 接口
- daily_report / web_api / pnl_snapshot 都走这个统一入口，不再写死 NDQ/黄金分支
- 失败容忍：yfinance 挂时回落到 db/market_store 兜底（is_stale=True）

支持的 proxy_kind:
- `direct`     — 直接拉 holding.symbol（yfinance 兼容的所有 symbol）
- `gold_cny_per_gram` — GC=F + USDCNY=X 反推 CNY/克（浙商积存金这类）
- `fx_pair`    — 货币对（USDCNY=X / EURUSD=X 等）
- `eastmoney_fund` — 中国场外公募基金最新已确认单位净值
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from openinvest.utils.exchange_fee import get_history_data
from openinvest.utils.eastmoney_fund import extract_fund_code, fetch_fund_nav
from openinvest.utils.gold_price import get_gold_snapshot
from openinvest.utils.market_observation import DEFAULT_QUOTE_MAX_AGE, frame_observation, utc_time, observation_now

log = logging.getLogger(__name__)


def _same_listing(symbol, proxy, holding):
    from openinvest.services.symbol_map import canonical_symbol
    return canonical_symbol(symbol,holding.get('kind')) == canonical_symbol(proxy,holding.get('kind'))


def _reported_currency(value):
    # In particular, GBp must not silently become GBP (a factor of 100).
    return value if isinstance(value,str) and re.fullmatch(r'[A-Z]{3}',value) and value not in {'GBX','ZAC','ILA'} else None


def _holding_scale_matches(holding, currency, unit, aliases):
    declared = str(holding.get('cost_currency') or '').strip()
    if declared and declared != currency:
        return False
    declared_unit = str(holding.get('unit_label') or '').strip()
    return not declared_unit or declared_unit in {*aliases,unit}


def _native_contract(source, proxy):
    from openinvest.services.symbol_map import canonical_symbol
    for key in ('symbol','provider_symbol'):
        if source.get(key) and canonical_symbol(source[key]) != canonical_symbol(proxy):
            return None
    currency = _reported_currency(source.get('currency'))
    if currency is None:
        return None
    kind = str(source.get('instrument_type') or '').upper()
    if proxy.upper() == 'GC=F' and currency == 'USD' and kind in {'FUTURE',''}:
        return currency,'oz',{'oz','盎司','troy_oz'}
    if kind in {'EQUITY','ETF','MUTUALFUND'}:
        return currency,'share',{'share','shares','股','份','unit','units'}
    if kind == 'INDEX':
        return 'points','points',{'points','point','点'}
    if kind == 'CRYPTOCURRENCY' and proxy.endswith('-'+currency):
        base = proxy[:-(len(currency)+1)]
        return currency,base,{base}
    return None


def _fx_contract(symbol):
    text = symbol.upper()
    if not text.endswith('=X'):
        return None
    pair = text[:-2]
    if not re.fullmatch(r'[A-Z]{3}|[A-Z]{6}',pair):
        return None
    return ('USD',pair) if len(pair)==3 else (pair[:3],pair[3:])


@dataclass
class QuoteSnapshot:
    """统一行情快照（任何 holding 拉到的行情都包成这个结构）

    字段语义：
    - price：单位价格，单位为 currency（如 CNY/克 → price=1031, currency=CNY, unit=克）
    - last_updated：行情日期 YYYY-MM-DD；None 表示数据源不提供时间戳
    - is_stale：不足以作为即时价；来源失败、闭市参考和时间未知由 source_observation 分别披露
    - extra：proxy 特有的额外字段（金价的 bank_cny_per_gram、direct 的 day_change_pct 等）
    """
    symbol: str
    price: float
    currency: str
    unit: str
    last_updated: Optional[str] = None
    is_stale: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    price_basis: str = "unverified"
    observed_at: Optional[str] = None
    is_reference: bool = True
    source_observation: Dict[str, Any] = field(default_factory=dict)


def get_quote(holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """根据 holding 配置拉行情。失败返回 None（caller 自己决定怎么处理）

    holding 至少包含：
    - symbol: str
    - cost_currency/unit_label: optional declarations, checked against native scale
    - proxy_kind: 'direct' | 'gold_cny_per_gram' | 'fx_pair' | 'eastmoney_fund'
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
    if is_eastmoney_fund(holding):
        return _quote_eastmoney_fund(symbol, holding)
    if _fx_contract(proxy_symbol):
        return _quote_fx(symbol,proxy_symbol,holding)
    # 默认 direct
    return _quote_direct(symbol, proxy_symbol, holding)


def is_eastmoney_fund(holding: Dict[str, Any]) -> bool:
    """场外公募基金持仓（走东方财富净值，不走 yfinance）。

    显式 proxy_kind=eastmoney_fund，或 kind=fund 且 symbol 能解析出六位基金代码。
    status / committee / daily_report 各自的取价循环都用这个判断，保证同一持仓同口径估值。
    """
    if str(holding.get("proxy_kind") or "") == "eastmoney_fund":
        return True
    return (
        str(holding.get("kind") or "").lower() == "fund"
        and extract_fund_code(str(holding.get("symbol") or "")) is not None
    )


def _quote_eastmoney_fund(
    symbol: str,
    holding: Dict[str, Any],
) -> Optional[QuoteSnapshot]:
    """中国场外公募基金：返回最新已确认单位净值（CNY/份）。"""
    if not _holding_scale_matches(holding,'CNY','份',{'share','shares','份','unit','units'}):
        return None
    snap = fetch_fund_nav(symbol)
    if snap is None or snap.code != extract_fund_code(symbol):
        return None
    return QuoteSnapshot(
        symbol=symbol,
        price=snap.nav,
        currency="CNY",
        unit=str(holding.get("unit_label") or "份"),
        last_updated=snap.nav_date or None,
        is_stale=snap.is_stale,
        price_basis='confirmed_unit_nav',
        source_observation={'provider':'eastmoney','currency':'CNY','quote_unit':'CNY/unit',
            'fetch_status':'failed' if snap.is_stale else 'ok',
            'freshness':'confirmed_nav_reference','session_date':snap.nav_date or None,'quote_fresh':False,
            'received_at':None,'bar_asof':snap.nav_date or None,'quote_asof':None,'completed_session_dates':[]},
        extra={
            "source": "eastmoney",
            "fund_code": snap.code,
            "nav_type": "confirmed_unit_nav",
        },
    )


def _quote_gold(symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """黄金 CNY/克 反推（spot_cny_per_gram = GC=F USD/oz / 31.1035 * USDCNY）"""
    if (not _holding_scale_matches(holding,'CNY','克',{'g','gram','grams','克'})
            or str(holding.get('yfinance_proxy') or 'GC=F').upper() != 'GC=F'):
        return None
    offset = float(holding.get("price_offset_pct") or 0.0)
    snap = get_gold_snapshot(offset_pct=offset)
    if snap is None:
        return None
    gold_asof, fx_asof = utc_time(getattr(snap,'gold_asof',None)), utc_time(getattr(snap,'fx_asof',None))
    dated = gold_asof is not None and fx_asof is not None
    source = getattr(snap,'source_observation',{}) or {}
    legs = source.get('legs') or {}
    for leg, currency in (('GC=F','USD'),('USDCNY=X','CNY')):
        reported = (legs.get(leg) or {}).get('currency')
        if reported is not None and _reported_currency(reported) != currency:
            return None
    source = {**source,'currency':'CNY','quote_unit':'CNY/g',
              'unit_basis':'explicit_futures_fx_per_gram_conversion'}
    now = observation_now()
    fresh = (dated and source.get('fetch_status') == 'ok' and source.get('freshness') == 'fresh_reference'
        and all(0 <= (now-stamp).total_seconds() <= DEFAULT_QUOTE_MAX_AGE.total_seconds()
                for stamp in (gold_asof,fx_asof)))
    return QuoteSnapshot(
        symbol=symbol,
        price=snap.spot_cny_per_gram,
        currency="CNY",
        unit=holding.get("unit_label") or "克",
        last_updated=min(gold_asof,fx_asof).date().isoformat() if dated else None,
        is_stale=snap.is_stale or not fresh,
        price_basis='futures_fx_reference', is_reference=True,
        observed_at=min(gold_asof,fx_asof).isoformat() if dated else None,
        source_observation=source,
        extra={
            "bank_cny_per_gram": snap.bank_cny_per_gram,
            "offset_pct": snap.offset_pct,
            "gold_usd_per_oz": snap.gold_usd_per_oz,
            "usdcny_rate": snap.usdcny_rate,
            "bank_quote_observed":False, "bank_price_basis":"offset_estimate",
            "gold_asof":getattr(snap,'gold_asof',None), "fx_asof":getattr(snap,'fx_asof',None),
        },
    )


def _quote_fx(symbol: str, proxy_symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """货币对（USDCNY=X 等）。yfinance 直接给汇率"""
    contract = _fx_contract(proxy_symbol)
    alias = symbol.upper() if symbol.upper().endswith('=X') else symbol.upper()+'=X'
    if contract is None or alias != proxy_symbol.upper():
        return None
    base, currency = contract
    if not _holding_scale_matches(holding,currency,base,{base}):
        return None
    try:
        df = get_history_data(proxy_symbol, "5d")
    except Exception as e:  # noqa: BLE001
        log.warning(f"fx_pair {proxy_symbol} 拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None
    if not math.isfinite(float(df['Close'].iloc[-1])) or not float(df['Close'].iloc[-1]) > 0:
        return None
    source = frame_observation(df)
    if source.get('currency') is not None and _reported_currency(source['currency']) != currency:
        return None
    from openinvest.services.symbol_map import canonical_symbol
    if any(source.get(key) and canonical_symbol(source[key]) != canonical_symbol(proxy_symbol)
           for key in ('symbol','provider_symbol')):
        return None
    source = {**source,'currency':currency,'base_currency':base,'quote_unit':currency+'/'+base,
              'unit_basis':'explicit_yahoo_currency_pair'}
    return QuoteSnapshot(
        symbol=symbol,
        price=float(df["Close"].iloc[-1]),
        currency=currency,
        unit=base,
        last_updated=df.index[-1].strftime("%Y-%m-%d"),
        is_stale=bool(df.attrs.get("yf_fetch_failed", False)) or not source.get('quote_fresh'),
        price_basis=source.get('price_basis','unverified'),
        observed_at=source.get('quote_asof'), is_reference=not source.get('quote_fresh',False),
        source_observation=source,
    )


def _quote_direct(symbol: str, proxy_symbol: str, holding: Dict[str, Any]) -> Optional[QuoteSnapshot]:
    """yfinance 直接拉 5d 历史，取最新 close"""
    if not symbol or not _same_listing(symbol,proxy_symbol,holding):
        return None
    try:
        df = get_history_data(proxy_symbol, "5d")
    except Exception as e:  # noqa: BLE001  yfinance 抛各种网络异常都接住
        log.warning(f"direct {proxy_symbol} 行情拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None
    if not math.isfinite(float(df['Close'].iloc[-1])) or not float(df['Close'].iloc[-1]) > 0:
        return None
    source = frame_observation(df)
    contract = _native_contract(source,proxy_symbol)
    if contract is None:
        return None
    currency, unit, aliases = contract
    if not _holding_scale_matches(holding,currency,unit,aliases):
        return None
    unit = holding.get('unit_label') or unit
    source = {**source,'currency':currency,'quote_unit':'points' if unit=='points' else currency+'/'+unit,
              'unit_basis':'provider_instrument_and_native_contract'}
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    pct = (last / prev - 1) * 100 if prev else 0.0
    history_qualified = source.get('history_basis_qualified') is not False
    return QuoteSnapshot(
        symbol=symbol,
        price=last,
        currency=currency,
        unit=unit,
        last_updated=df.index[-1].strftime("%Y-%m-%d"),
        is_stale=bool(df.attrs.get("yf_fetch_failed", False)) or not source.get('quote_fresh'),
        price_basis=source.get('price_basis','unverified'),
        observed_at=source.get('quote_asof'), is_reference=not source.get('quote_fresh',False),
        source_observation=source,
        extra={
            "prev_close": prev if history_qualified else None,
            "day_change_pct": round(pct, 4) if history_qualified else None,
            "day_change_source_qualified":history_qualified,
        },
    )
