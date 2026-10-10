"""中国场外公募基金净值适配器：symbol 规范化 + API 解析 + 缓存语义 + 失败降级。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from openinvest.utils import eastmoney_fund as emf

CN_TZ = timezone(timedelta(hours=8))


class _Response:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _lsjz(nav="2.0000", nav_date=None):
    nav_date = nav_date or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    return {"Data": {"LSJZList": [{"FSRQ": nav_date, "DWJZ": nav}]}, "ErrCode": 0}


@pytest.fixture(autouse=True)
def _clear_cache():
    emf.clear_nav_cache()
    yield
    emf.clear_nav_cache()


def test_fund_symbol_normalization():
    assert emf.extract_fund_code("FUND:123456") == "123456"
    assert emf.extract_fund_code("123456.SZ") == "123456"
    assert emf.extract_fund_code("AAPL") is None
    assert emf.canonical_fund_symbol("123456.SS") == "FUND:123456"


def test_fetch_fund_nav_parses_latest_confirmed_nav(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        return _Response(_lsjz("2.0000", "2026-01-05"))

    monkeypatch.setattr(emf.requests, "get", fake_get)
    snap = emf.fetch_fund_nav("FUND:123456")
    assert snap is not None
    assert snap.code == "123456"
    assert snap.nav == 2.0
    assert snap.nav_date == "2026-01-05"
    url, kw = calls[0]
    assert url == emf._LSJZ_URL
    assert kw["params"]["fundCode"] == "123456"
    assert kw["headers"]["Referer"].startswith("https://fundf10.eastmoney.com")


def test_fetch_fund_nav_missing_or_invalid(monkeypatch):
    monkeypatch.setattr(
        emf.requests, "get",
        lambda *a, **k: _Response({"Data": {"LSJZList": []}, "ErrCode": 0}),
    )
    assert emf.fetch_fund_nav("FUND:999999") is None
    assert emf.fetch_fund_nav("not-a-fund") is None
    # 货币基金等没有单位净值
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(_lsjz(nav="")))
    assert emf.fetch_fund_nav("FUND:999998") is None


def test_fetch_fund_nav_marks_old_nav_stale(monkeypatch):
    old = (datetime.now(CN_TZ) - timedelta(days=emf._STALE_DAYS + 1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(_lsjz(nav_date=old)))
    assert emf.fetch_fund_nav("FUND:123456").is_stale is True
    emf.clear_nav_cache()
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(_lsjz()))
    assert emf.fetch_fund_nav("FUND:123456").is_stale is False


def _pingzhong_text(points):
    """points: [(YYYY-MM-DD, nav)] → pingzhongdata JS（x = 北京时间零点毫秒）。"""
    trend = [
        {"x": int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000),
         "y": nav, "equityReturn": 0, "unitMoney": ""}
        for d, nav in points
    ]
    return f"var Data_netWorthTrend = {json.dumps(trend)};"


def _by_host(lsjz, pingzhong, calls):
    """按 URL 分流的假 requests.get；lsjz / pingzhong 是 Exception 实例或 _Response。"""
    def fake_get(url, **kw):
        calls.append(url)
        r = lsjz if url == emf._LSJZ_URL else pingzhong
        if isinstance(r, Exception):
            raise r
        return r
    return fake_get


def test_failed_fetch_is_not_cached(monkeypatch):
    """两个源都失败 → None 且不缓存：一次网络抖动不能让该基金在常驻进程（MCP / scheduler）里一直缺价。"""
    calls = []
    monkeypatch.setattr(emf.requests, "get", _by_host(
        requests.ConnectionError("transient"), requests.Timeout("slow"), calls))
    assert emf.fetch_fund_nav("FUND:123456") is None
    assert calls == [emf._LSJZ_URL, emf._PINGZHONG_URL.format(code="123456")]

    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(_lsjz()))
    snap = emf.fetch_fund_nav("FUND:123456")
    assert snap is not None and snap.nav == 2.0


def test_lsjz_timeout_falls_back_to_pingzhongdata_last_point(monkeypatch):
    """2026-10-10 复现：lsjz 间歇超时 → 用 pingzhongdata 最后一个点（净值 + 日期），成功结果照常缓存。"""
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    calls = []
    monkeypatch.setattr(emf.requests, "get", _by_host(
        requests.ReadTimeout("read timeout=8"),
        _Response(text=_pingzhong_text([("2026-01-05", 1.5), (today, 1.7321)])),
        calls))
    snap = emf.fetch_fund_nav("FUND:123456")
    assert snap == emf.FundNavSnapshot(code="123456", nav=1.7321, nav_date=today, is_stale=False)
    assert emf.fetch_fund_nav("123456.OF") == snap          # 命中缓存，不再打网络
    assert len(calls) == 2


def test_fallback_applies_same_stale_rule(monkeypatch):
    old = (datetime.now(CN_TZ) - timedelta(days=emf._STALE_DAYS + 1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(emf.requests, "get", _by_host(
        requests.ConnectionError("down"), _Response(text=_pingzhong_text([(old, 1.2)])), []))
    snap = emf.fetch_fund_nav("FUND:123456")
    assert snap.nav_date == old and snap.is_stale is True


def test_successful_fetch_cached_until_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Response(_lsjz(nav=f"{calls['n']}.0000"))

    clock = {"t": 1000.0}
    monkeypatch.setattr(emf.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(emf.requests, "get", fake_get)

    assert emf.fetch_fund_nav("FUND:123456").nav == 1.0
    clock["t"] += emf._NAV_TTL_SECONDS - 1
    assert emf.fetch_fund_nav("123456.SZ").nav == 1.0      # 同一代码命中缓存
    assert calls["n"] == 1
    clock["t"] += 2
    assert emf.fetch_fund_nav("FUND:123456").nav == 2.0    # 过了 TTL 重新拉
    assert calls["n"] == 2


def test_fetch_fund_nav_history_dates_in_china_time(monkeypatch):
    """pingzhongdata 的 x 是北京时间零点；UTC 宿主机上也必须标成当天而不是前一天。"""
    def ms(y, m, d):
        return int(datetime(y, m, d, tzinfo=CN_TZ).timestamp() * 1000)

    trend = [
        {"x": ms(2026, 1, 5), "y": 1.5, "equityReturn": 0, "unitMoney": ""},
        {"x": ms(2026, 1, 6), "y": None, "equityReturn": 0, "unitMoney": ""},
        {"x": ms(2026, 1, 7), "y": 1.6, "equityReturn": 0, "unitMoney": ""},
    ]
    text = f'var fS_name = "Demo Fund";var Data_netWorthTrend = {json.dumps(trend)};var x = 1;'
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        return _Response(text=text)

    monkeypatch.setattr(emf.requests, "get", fake_get)
    assert emf.fetch_fund_nav_history("FUND:123456") == [("2026-01-05", 1.5), ("2026-01-07", 1.6)]
    assert urls == [emf._PINGZHONG_URL.format(code="123456")]


def test_fetch_fund_nav_history_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(emf.requests, "get", boom)
    assert emf.fetch_fund_nav_history("FUND:123456") is None
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _Response(text="var other = 1;"))
    assert emf.fetch_fund_nav_history("FUND:123456") is None
    assert emf.fetch_fund_nav_history("AAPL") is None


def test_benchmark_fund_series_uses_shared_fetcher(monkeypatch):
    from openinvest.core import benchmarks

    monkeypatch.setattr(benchmarks, "fetch_fund_nav_history", lambda code: [
        ("2026-01-02", 1.0), ("2026-01-05", 1.1), ("2026-01-06", 1.2), ("2026-01-07", 1.3),
    ])
    assert benchmarks._fetch_eastmoney_fund("123456", "2026-01-05", "2026-01-06") == {
        "2026-01-05": 1.1, "2026-01-06": 1.2,
    }
    monkeypatch.setattr(benchmarks, "fetch_fund_nav_history", lambda code: None)
    assert benchmarks._fetch_eastmoney_fund("123456", "2026-01-01", "2026-12-31") == {}
