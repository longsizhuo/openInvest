"""utils/quotes.py 测试

3 种 proxy_kind 的分支 + yfinance 失败容忍。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import pytest

from openinvest.utils import quotes
from openinvest.utils.eastmoney_fund import FundNavSnapshot


@dataclass
class FakeGoldSnap:
    gold_usd_per_oz: float = 2400.0
    usdcny_rate: float = 7.2
    spot_cny_per_gram: float = 1031.0
    bank_cny_per_gram: float = 1031.5
    offset_pct: float = 0.0005
    is_stale: bool = False
    gold_asof: str = field(default_factory=lambda:datetime.now(timezone.utc).isoformat())
    fx_asof: str = field(default_factory=lambda:datetime.now(timezone.utc).isoformat())
    source_observation: dict = field(default_factory=lambda:{"fetch_status":"ok","freshness":"fresh_reference",
        "quote_fresh":False,"received_at":datetime.now(timezone.utc).isoformat(),"quote_asof":None,
        "bar_asof":None,"completed_session_dates":[]})


def _fake_df(currency='USD',instrument_type='EQUITY',symbol=None):
    frame = pd.DataFrame(
        {"Close": [50.0, 51.0, 52.0, 51.5, 52.5]},
        index=pd.date_range("2026-04-28", periods=5, freq="D"),
    )
    frame.attrs['source_observation'] = {'currency':currency,'instrument_type':instrument_type,'symbol':symbol}
    return frame


def test_get_quote_direct(monkeypatch):
    """direct: 直接 yfinance 拉"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    h = {"symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
         "unit_label": "股", "proxy_kind": "direct"}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.symbol == "AAPL"
    assert q.price == 52.5
    assert q.currency == "USD"
    assert q.unit == "股"
    assert q.extra["prev_close"] == 51.5
    assert q.extra["day_change_pct"] == pytest.approx(1.9417, rel=1e-3)


def test_get_quote_gold(monkeypatch):
    """gold_cny_per_gram: 走 get_gold_snapshot 反推"""
    monkeypatch.setattr(quotes, "get_gold_snapshot", lambda offset_pct=0.0: FakeGoldSnap(offset_pct=offset_pct))
    h = {"symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
         "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
         "price_offset_pct": 0.0005}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.symbol == "GC=F"
    assert q.price == 1031.0
    assert q.currency == "CNY"
    assert q.unit == "克"
    assert q.extra["bank_cny_per_gram"] == 1031.5
    assert q.extra["offset_pct"] == 0.0005
    assert q.is_stale is False


def test_get_quote_gold_stale(monkeypatch):
    """gold proxy 数据库兜底时 is_stale=True"""
    monkeypatch.setattr(quotes, "get_gold_snapshot",
                        lambda offset_pct=0.0: FakeGoldSnap(is_stale=True))
    q = quotes.get_quote({
        "symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
    })
    assert q.is_stale is True


def test_get_quote_gold_returns_none_when_snapshot_fails(monkeypatch):
    """get_gold_snapshot 返回 None → quote 也返回 None"""
    monkeypatch.setattr(quotes, "get_gold_snapshot", lambda offset_pct=0.0: None)
    q = quotes.get_quote({
        "symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
    })
    assert q is None


def test_get_quote_fx(monkeypatch):
    """fx_pair: yfinance 拉汇率"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df(currency='CNY',instrument_type='CURRENCY',symbol='USDCNY=X'))
    h = {"symbol": "USDCNY", "yfinance_proxy": "USDCNY=X",
         "kind": "fund", "cost_currency": "CNY",
         "unit_label": "USD", "proxy_kind": "fx_pair"}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.unit == "USD" and q.currency == 'CNY'
    assert q.source_observation['quote_unit'] == 'CNY/USD'


def test_get_quote_rejects_an_unconverted_economic_proxy(monkeypatch):
    """A futures quote cannot silently price a different bank asset in grams."""
    captured = []

    def _spy(symbol, period="5d"):
        captured.append(symbol)
        return _fake_df()

    monkeypatch.setattr(quotes, "get_history_data", _spy)
    quote = quotes.get_quote({
        "symbol": "ZSGOLD-CUSTOM", "yfinance_proxy": "GC=F",
        "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "direct",   # 强制走 direct
    })
    assert quote is None and captured == []


def test_get_quote_direct_failure_returns_none(monkeypatch):
    """yfinance 抛异常时静默返回 None，不让上层崩"""
    def _boom(s, p="5d"):
        raise RuntimeError("yfinance API down")
    monkeypatch.setattr(quotes, "get_history_data", _boom)
    q = quotes.get_quote({
        "symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
        "unit_label": "股", "proxy_kind": "direct",
    })
    assert q is None


def test_get_quote_direct_empty_df_returns_none(monkeypatch):
    """空 DataFrame（休市/无数据）→ None"""
    monkeypatch.setattr(quotes, "get_history_data",
                        lambda s, p="5d": pd.DataFrame(columns=["Close"]))
    q = quotes.get_quote({
        "symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
        "unit_label": "股", "proxy_kind": "direct",
    })
    assert q is None


def test_get_quote_default_proxy_kind_is_direct(monkeypatch):
    """holding 没 proxy_kind 字段 → 默认 direct"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    q = quotes.get_quote({"symbol": "AAPL", "cost_currency": "USD", "unit_label": "股"})
    assert q is not None
    assert q.symbol == "AAPL"


def test_get_quote_direct_stale_when_fetch_failed(monkeypatch):
    """get_history_data 标了本次拉取失败（返回库里旧数据）→ direct quote is_stale=True"""
    df = _fake_df()
    df.attrs["yf_fetch_failed"] = True
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": df)
    q = quotes.get_quote({"symbol": "AAPL", "cost_currency": "USD", "proxy_kind": "direct"})
    assert q.is_stale is True
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    # No provider asof/session proof: retained historical values remain references.
    assert quotes.get_quote({"symbol": "AAPL", "cost_currency": "USD"}).is_stale is True


def test_get_quote_eastmoney_fund(monkeypatch):
    monkeypatch.setattr(
        quotes,
        "fetch_fund_nav",
        lambda symbol: FundNavSnapshot(
            code="123456", nav=2.0, nav_date="2026-01-05", is_stale=False,
        ),
    )
    q = quotes.get_quote({
        "symbol": "FUND:123456", "kind": "fund", "cost_currency": "CNY",
        "unit_label": "份", "proxy_kind": "eastmoney_fund",
    })
    assert q is not None
    assert q.price == 2.0
    assert q.currency == "CNY"
    assert q.last_updated == "2026-01-05"
    assert q.extra["source"] == "eastmoney"


def test_is_eastmoney_fund_routing():
    """显式 proxy_kind，或 kind=fund + 六位代码（buy --kind fund 建的仓没有 proxy_kind）。"""
    assert quotes.is_eastmoney_fund({"symbol": "FUND:123456", "proxy_kind": "eastmoney_fund"})
    assert quotes.is_eastmoney_fund({"symbol": "FUND:123456", "kind": "fund"})
    assert quotes.is_eastmoney_fund({"symbol": "123456", "kind": "fund", "proxy_kind": "direct"})
    assert not quotes.is_eastmoney_fund({"symbol": "510300.SS", "kind": "etf"})
    assert not quotes.is_eastmoney_fund({"symbol": "AAPL", "kind": "fund"})


@pytest.mark.parametrize('holding,currency,kind,unit',[
    ({'symbol':'AAPL'},'USD','EQUITY','share'),
    ({'symbol':'NDQ.AX','cost_currency':'AUD','unit_label':'份'},'AUD','ETF','份'),
    ({'symbol':'GC=F','cost_currency':'USD','unit_label':'oz'},'USD','FUTURE','oz'),
    ({'symbol':'GC=F'},'USD','FUTURE','oz'),
    ({'symbol':'BTC-USD'},'USD','CRYPTOCURRENCY','BTC'),
    ({'symbol':'^NDX'},'USD','INDEX','points'),
])
def test_native_quote_uses_provider_currency_and_contract_unit(monkeypatch,holding,currency,kind,unit):
    monkeypatch.setattr(quotes,'get_history_data',lambda *args:_fake_df(currency,kind,holding['symbol']))
    quote = quotes.get_quote(holding)
    assert quote is not None and quote.unit == unit
    assert quote.currency == ('points' if kind=='INDEX' else currency)
    assert quote.price == 52.5  # No implicit FX or quantity conversion.


@pytest.mark.parametrize('holding,currency,kind,reported_symbol',[
    ({'symbol':'AAPL','cost_currency':'CNY'},'USD','EQUITY','AAPL'),
    ({'symbol':'NDQ.AX','cost_currency':'USD'},'AUD','ETF','NDQ.AX'),
    ({'symbol':'AAPL','cost_currency':'USD'},None,'EQUITY','AAPL'),
    ({'symbol':'SHEL.L','cost_currency':'GBP'},'GBp','EQUITY','SHEL.L'),
    ({'symbol':'SHEL.L','cost_currency':'GBP'},'GBX','EQUITY','SHEL.L'),
    ({'symbol':'AAPL','unit_label':'g'},'USD','EQUITY','AAPL'),
    ({'symbol':'GC=F','cost_currency':'CNY','unit_label':'克'},'USD','FUTURE','GC=F'),
    ({'symbol':'GC=F','cost_currency':'USD','unit_label':'share'},'USD','FUTURE','GC=F'),
    ({'symbol':'AAPL'},'USD','EQUITY','MSFT'),
])
def test_unverified_or_mismatched_currency_unit_and_provider_identity_are_unavailable(monkeypatch,holding,currency,kind,reported_symbol):
    rows = _fake_df(currency,kind)
    rows.attrs['source_observation']['provider_symbol'] = reported_symbol
    monkeypatch.setattr(quotes,'get_history_data',lambda *args:rows)
    assert quotes.get_quote(holding) is None


def test_economic_tracking_proxies_are_not_interchangeable_quotes_but_exact_listing_aliases_are(monkeypatch):
    calls = []
    def history(symbol,*args):
        calls.append(symbol)
        return _fake_df('HKD','EQUITY','0005.HK')
    monkeypatch.setattr(quotes,'get_history_data',history)
    assert quotes.get_quote({'symbol':'NDQ.AX','yfinance_proxy':'^NDX','cost_currency':'AUD'}) is None
    assert quotes.get_quote({'symbol':'9988.HK','yfinance_proxy':'BABA','cost_currency':'HKD'}) is None
    assert calls == []
    quote = quotes.get_quote({'symbol':'00005.HK','yfinance_proxy':'0005.HK','cost_currency':'HKD','unit_label':'股'})
    assert quote is not None and quote.currency == 'HKD'


@pytest.mark.parametrize('holding',[
    {'symbol':'USDCNY','yfinance_proxy':'CNYUSD=X','proxy_kind':'fx_pair'},
    {'symbol':'USDCNY=X','cost_currency':'USD','proxy_kind':'fx_pair'},
    {'symbol':'USDCNY=X','cost_currency':'CNY','unit_label':'CNY','proxy_kind':'fx_pair'},
])
def test_fx_pair_orientation_and_position_denomination_must_match(monkeypatch,holding):
    monkeypatch.setattr(quotes,'get_history_data',lambda *args:_fake_df('CNY','CURRENCY','USDCNY=X'))
    assert quotes.get_quote(holding) is None


def test_fx_tracking_without_cost_currency_retains_native_pair_units(monkeypatch):
    monkeypatch.setattr(quotes,'get_history_data',lambda *args:_fake_df('CNY','CURRENCY','USDCNY=X'))
    quote = quotes.get_quote({'symbol':'USDCNY=X'})
    assert quote.currency == 'CNY' and quote.unit == 'USD'
    assert quote.source_observation['quote_unit'] == 'CNY/USD'


@pytest.mark.parametrize('proxy,cost,unit',[
    ('gold_cny_per_gram','USD','克'),('gold_cny_per_gram','CNY','oz'),
    ('eastmoney_fund','USD','份'),('eastmoney_fund','CNY','克'),
])
def test_explicit_conversions_and_nav_do_not_relabel_incompatible_account_scales(monkeypatch,proxy,cost,unit):
    monkeypatch.setattr(quotes,'get_gold_snapshot',lambda **kwargs:pytest.fail('mismatched scale should stop before IO'))
    monkeypatch.setattr(quotes,'fetch_fund_nav',lambda *args:pytest.fail('mismatched scale should stop before IO'))
    assert quotes.get_quote({'symbol':'GC=F' if proxy=='gold_cny_per_gram' else 'FUND:123456',
        'proxy_kind':proxy,'cost_currency':cost,'unit_label':unit}) is None


def test_a_known_wrong_conversion_leg_currency_is_not_ignored(monkeypatch):
    snap = FakeGoldSnap()
    snap.source_observation['legs'] = {'GC=F':{'currency':'USD'},'USDCNY=X':{'currency':'EUR'}}
    monkeypatch.setattr(quotes,'get_gold_snapshot',lambda **kwargs:snap)
    assert quotes.get_quote({'symbol':'GC=F','proxy_kind':'gold_cny_per_gram','cost_currency':'CNY','unit_label':'克'}) is None


def test_a_different_reported_fund_code_is_not_the_requested_nav(monkeypatch):
    monkeypatch.setattr(quotes,'fetch_fund_nav',lambda symbol:FundNavSnapshot(code='654321',nav=2.0,nav_date='2026-01-05',is_stale=False))
    assert quotes.get_quote({'symbol':'FUND:123456','proxy_kind':'eastmoney_fund'}) is None
