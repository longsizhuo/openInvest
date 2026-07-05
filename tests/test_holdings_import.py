"""holdings_import:无 LLM key 报错 + kind 映射 + commit 非破坏(只加新/不覆盖)。
跑:uv run pytest tests/test_holdings_import.py -q"""
from contextlib import contextmanager

import pytest

from openinvest.services.holdings_import import _normalize_holding, commit_parsed, parse_holdings


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
