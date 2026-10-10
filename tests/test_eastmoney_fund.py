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


def test_failed_fetch_is_not_cached(monkeypatch):
    """一次网络失败不能让该基金在常驻进程（MCP / scheduler）里一直缺价。"""
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("transient")
        return _Response(_lsjz())

    monkeypatch.setattr(emf.requests, "get", flaky_get)
    assert emf.fetch_fund_nav("FUND:123456") is None
    snap = emf.fetch_fund_nav("FUND:123456")
    assert snap is not None and snap.nav == 2.0
    assert calls["n"] == 2


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


def _pingzhong(trend):
    return _Response(text=f"var Data_netWorthTrend = {json.dumps(trend)};var x = 1;")


def _ms(day):
    return int(datetime(2026, 1, day, tzinfo=CN_TZ).timestamp() * 1000)


def test_fetch_fund_nav_history_adjusted_removes_dividend_gap(monkeypatch):
    """分红日单位净值从 2.0 掉到 1.8（除息），真实日收益 +1%：前复权后不能是 -10% 的假暴跌，
    最新一天必须等于真实单位净值。"""
    trend = [
        {"x": _ms(5), "y": 1.0, "equityReturn": 0, "unitMoney": ""},
        {"x": _ms(6), "y": 2.0, "equityReturn": 100, "unitMoney": ""},
        {"x": _ms(7), "y": 1.8, "equityReturn": 1.0, "unitMoney": "分红：每10份派现金2.2000元"},
        {"x": _ms(8), "y": 1.89, "equityReturn": 5.0, "unitMoney": ""},
    ]
    monkeypatch.setattr(emf.requests, "get", lambda *a, **k: _pingzhong(trend))
    raw = emf.fetch_fund_nav_history("FUND:123456")
    assert [v for _, v in raw] == [1.0, 2.0, 1.8, 1.89]          # 默认口径不变

    adj = emf.fetch_fund_nav_history("FUND:123456", adjusted=True)
    assert [d for d, _ in adj] == [d for d, _ in raw]
    v = [x for _, x in adj]
    assert v[-1] == 1.89
    assert v[3] / v[2] == pytest.approx(1.05)                     # 平日用净值比
    assert v[2] / v[1] == pytest.approx(1.01)                     # 分红日用 equityReturn
    assert v[1] / v[0] == pytest.approx(2.0)


def test_get_history_data_fund_uses_eastmoney_not_yfinance(monkeypatch, tmp_path):
    """FUND: 不走 yfinance：东方财富前复权净值入库；TTL 内不重拉；拉取失败用库里已有的并标 stale。"""
    from openinvest.db import market_store
    import openinvest.utils.exchange_fee as ef

    monkeypatch.setattr(market_store, "DB_PATH", str(tmp_path / "market.db"))
    monkeypatch.setattr(ef, "_STORE", market_store.MarketStore())
    monkeypatch.setattr(ef, "_FUND_REFRESHED", {})
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: pytest.fail("yfinance must not be called"))
    calls = {"n": 0}

    def fake_history(symbol, adjusted=False):
        calls["n"] += 1
        assert adjusted is True
        return [("2026-01-05", 1.0), ("2026-01-06", 1.1), ("2026-01-07", 1.2)]

    monkeypatch.setattr(ef, "fetch_fund_nav_history", fake_history)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ef.time, "monotonic", lambda: clock["t"])

    df = ef.get_history_data("fund:123456", "max")
    assert list(df["Close"]) == [1.0, 1.1, 1.2]
    assert df.attrs["yf_fetch_failed"] is False
    ef.get_history_data("FUND:123456", "max")
    assert calls["n"] == 1                                        # TTL 内命中库

    clock["t"] += ef._FUND_REFRESH_TTL
    monkeypatch.setattr(ef, "fetch_fund_nav_history", lambda s, adjusted=False: None)
    df = ef.get_history_data("FUND:123456", "max", as_of_date="2026-01-06")
    assert list(df["Close"]) == [1.0, 1.1]                        # 回测截断照常生效（含当日）
    assert df.attrs["yf_fetch_failed"] is True
