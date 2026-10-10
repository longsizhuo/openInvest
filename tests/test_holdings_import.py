"""holdings_import:无 LLM key 报错 + kind 映射 + commit 非破坏(只加新/不覆盖)。
跑:uv run pytest tests/test_holdings_import.py -q"""
from contextlib import contextmanager

import pytest

from openinvest.services.holdings_import import (
    _normalize_holding,
    commit_parsed,
    enrich_fund_holdings,
    parse_holdings,
)
from openinvest.utils.eastmoney_fund import FundNavSnapshot


class FakePM:
    """只实现 commit_parsed 用到的两个接口。"""
    def __init__(self, state):
        self.state = state
        self.reloaded = False

    @contextmanager
    def with_portfolio_tx(self):
        yield self.state

    def _reload(self):
        self.reloaded = True


def test_parse_holdings_no_key_raises(monkeypatch):
    monkeypatch.setattr("openinvest.utils.llm.get_llm_config_safe", lambda *a, **k: (None, "", "", ""))
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        parse_holdings("510300 ETF 3000股")


def test_normalize_kind_and_defaults():
    h = _normalize_holding({"symbol": "AAPL", "kind": "stock", "units": "5"})
    assert h["kind"] == "equity"                 # parser 出 stock → schema 要 equity
    assert h["unit_label"] == "股" and h["cost_currency"] == "CNY" and h["channel"] == "未指定"
    assert h["display_name"] == "AAPL"
    assert _normalize_holding({"symbol": "X", "kind": "weird"})["kind"] == "other"


def test_portfolio_schema_migrates_legacy_stock_kind():
    from openinvest.core.schemas import validate_portfolio

    out = validate_portfolio({
        "schema_version": 2,
        "cash": {},
        "holdings": [{
            "symbol": "TEST.SS",
            "kind": "stock",
            "units": 100,
            "avg_cost": 10.0,
            "cost_currency": "CNY",
        }],
    })

    assert out["holdings"][0]["kind"] == "equity"


def test_fund_enrichment_derives_units_and_avg_cost(monkeypatch):
    monkeypatch.setattr(
        "openinvest.utils.eastmoney_fund.fetch_fund_nav",
        lambda symbol: FundNavSnapshot(
            code="123456", nav=2.0, nav_date="2026-01-05", is_stale=False,
        ),
    )
    parsed = enrich_fund_holdings({
        "cash": {},
        "holdings": [{
            "symbol": "123456.SZ", "kind": "fund", "units": 0, "avg_cost": 0,
            "market_value": 10000.0, "pnl": -1000.0,
            "display_name": "Demo Fund",
        }],
    })
    h = parsed["holdings"][0]
    assert h["symbol"] == "FUND:123456"
    assert h["proxy_kind"] == "eastmoney_fund"
    assert h["units"] == pytest.approx(5000.0)
    assert h["avg_cost"] == pytest.approx(2.2)      # (10000 + 1000) / 5000
    assert h["nav_date_at_import"] == "2026-01-05"
    assert "warnings" not in parsed

    normalized = _normalize_holding(h)
    assert normalized["symbol"] == "FUND:123456"
    assert normalized["proxy_kind"] == "eastmoney_fund"
    assert normalized["kind"] == "fund"


def test_fund_enrichment_keeps_zero_units_and_warns_when_nav_unavailable(monkeypatch):
    """两个净值源都挂（真实 requests 层失败，走 fetch_fund_nav 兜底链）→ 不编份额，但必须点名告警。"""
    import requests

    from openinvest.utils import eastmoney_fund as emf

    emf.clear_nav_cache()
    urls = []

    def down(url, **kw):
        urls.append(url)
        raise requests.Timeout("read timeout")

    monkeypatch.setattr(emf.requests, "get", down)
    parsed = enrich_fund_holdings({"cash": {}, "holdings": [
        {"symbol": "123456", "kind": "fund", "units": 0, "avg_cost": 0,
         "market_value": 10000.0, "pnl": -1000.0, "display_name": "Demo Fund"},
        {"symbol": "510300.SS", "kind": "etf", "units": 100, "avg_cost": 4.2},
    ]})
    assert parsed["holdings"][0]["units"] == 0 and parsed["holdings"][0]["avg_cost"] == 0
    assert len(urls) == 2                                   # lsjz + pingzhongdata 都试过
    [w] = parsed["warnings"]                                # 只点名没换算成的那只
    assert w.startswith("fund not converted: FUND:123456")
    assert "Demo Fund" in w and "10,000.00" in w and "--existing-position" in w


def test_commit_non_destructive():
    pm = FakePM({"holdings": [{"symbol": "GC=F", "units": 10}], "cash": {"CNY": 5000}})
    parsed = {
        "holdings": [
            {"symbol": "GC=F", "kind": "metal", "units": 99},                                  # 已存在 → skip
            {"symbol": "510300.SS", "kind": "stock", "units": 3000, "avg_cost": 4.2, "cost_currency": "cny"},  # 新 → add
        ],
        "cash": {"CNY": 99999, "AUD": 300},  # CNY 已有>0 → skip;AUD 新 → set
    }
    s = commit_parsed(pm, parsed)

    assert s["added_holdings"] == ["510300.SS"]
    assert s["skipped_holdings"] == ["GC=F"]
    assert s["cash_set"] == {"AUD": 300.0}
    assert s["cash_skipped"] == {"CNY": 99999.0}

    # 已存在 GC=F 的 units 没被覆盖
    gc = next(h for h in pm.state["holdings"] if h["symbol"] == "GC=F")
    assert gc["units"] == 10
    # 新加 510300 kind 映射 + 币种大写
    new = next(h for h in pm.state["holdings"] if h["symbol"] == "510300.SS")
    assert new["kind"] == "equity" and new["cost_currency"] == "CNY"
    # cash:CNY 不动、AUD 新填
    assert pm.state["cash"]["CNY"] == 5000 and pm.state["cash"]["AUD"] == 300.0
    assert pm.reloaded


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
