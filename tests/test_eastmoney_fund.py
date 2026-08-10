"""中国场外公募基金净值适配器：symbol 规范化 + API 解析 + 失败降级。"""
from __future__ import annotations

from openinvest.utils import eastmoney_fund as emf


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fund_symbol_normalization():
    assert emf.extract_fund_code("FUND:162201") == "162201"
    assert emf.extract_fund_code("162201.SZ") == "162201"
    assert emf.extract_fund_code("AAPL") is None
    assert emf.canonical_fund_symbol("162201.SS") == "FUND:162201"


def test_fetch_fund_nav_exact_match(monkeypatch):
    payload = {
        "Datas": [
            {"CODE": "162201", "NAME": "宏利成长混合", "FundBaseInfo": {
                "SHORTNAME": "宏利成长混合", "DWJZ": 6.6305, "FSRQ": "2026-08-10",
            }},
        ],
    }
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(payload))
    emf.fetch_fund_nav.cache_clear()
    snap = emf.fetch_fund_nav("FUND:162201")
    assert snap is not None
    assert snap.code == "162201"
    assert snap.name == "宏利成长混合"
    assert snap.nav == 6.6305
    assert snap.nav_date == "2026-08-10"


def test_fetch_fund_nav_missing_or_invalid(monkeypatch):
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response({"Datas": []}))
    emf.fetch_fund_nav.cache_clear()
    assert emf.fetch_fund_nav("FUND:999999") is None
    assert emf.fetch_fund_nav("not-a-fund") is None
