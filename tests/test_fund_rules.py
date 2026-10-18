"""calc/fund_rules.py —— 场外基金两段式的日期规则（纯函数，ADR-027 D5）。

守的契约：
- T（定价日）：北京时间 15:00 前下单且当天是交易日 → 当日；15:00 整点及之后 / 周末 → 下一个交易日
- 确认日 = T + lag 个交易日（交易日历 = 周一到周五）
- 基金类型（QDII / FOF）从概况文本里认；lag 按类型取配置，类型缺 / 配置坏 → default
- 定价日净值：当天有就用，当天没有（休市）而更晚的已公布 → 顺延，都还没有 → None
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from openinvest.calc import fund_rules as fr

D = date.fromisoformat


def _at(day: str, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:{ss:02d}")   # naive = 北京时间


def test_fixture_calendar_assumptions():
    assert D("2026-01-05").weekday() == 0 and D("2026-01-09").weekday() == 4    # 周一 / 周五


@pytest.mark.parametrize("ts,lag,t,confirm", [
    # 周一 15:00 前 → T=当日；T+1 = 周二
    (_at("2026-01-05", 10), 1, "2026-01-05", "2026-01-06"),
    (_at("2026-01-05", 14, 59, 59), 1, "2026-01-05", "2026-01-06"),
    # 15:00 整点算收盘后 → T=周二，确认周三
    (_at("2026-01-05", 15), 1, "2026-01-06", "2026-01-07"),
    (_at("2026-01-05", 23, 30), 1, "2026-01-06", "2026-01-07"),
    # 周五 15:00 前 → T=周五，T+1 跨周末 = 下周一
    (_at("2026-01-09", 14), 1, "2026-01-09", "2026-01-12"),
    # 周五 15:00 后 → T=下周一，确认下周二
    (_at("2026-01-09", 16), 1, "2026-01-12", "2026-01-13"),
    # 周末下单 → T=下周一
    (_at("2026-01-10", 10), 1, "2026-01-12", "2026-01-13"),
    (_at("2026-01-11", 9), 1, "2026-01-12", "2026-01-13"),
    # QDII / FOF lag=2：周一上午 → 周三；周四 15:00 后 → T=周五，+2 个交易日 = 下周二
    (_at("2026-01-05", 10), 2, "2026-01-05", "2026-01-07"),
    (_at("2026-01-08", 16), 2, "2026-01-09", "2026-01-13"),
    # lag=0：确认日 = T
    (_at("2026-01-05", 10), 0, "2026-01-05", "2026-01-05"),
])
def test_pricing_and_confirm_dates(ts, lag, t, confirm):
    assert fr.pricing_date(ts) == D(t)
    assert fr.expected_confirm_date(ts, lag) == D(confirm)


def test_timezone_handling_aware_and_naive():
    cst = timezone(timedelta(hours=8))
    # 06:59:59Z = 14:59:59 北京 → 当日；07:00:00Z = 15:00 北京 → 下一交易日
    assert fr.pricing_date(datetime(2026, 1, 5, 6, 59, 59, tzinfo=timezone.utc)) == D("2026-01-05")
    assert fr.pricing_date(datetime(2026, 1, 5, 7, 0, 0, tzinfo=timezone.utc)) == D("2026-01-06")
    # UTC 周日 18:00 = 北京周一 02:00 → 北京日期是周一（<15:00，当日）
    assert fr.pricing_date(datetime(2026, 1, 4, 18, 0, tzinfo=timezone.utc)) == D("2026-01-05")
    assert fr.pricing_date(datetime(2026, 1, 5, 10, tzinfo=cst)) == D("2026-01-05")
    assert fr.pricing_date(_at("2026-01-05", 10)) == D("2026-01-05")          # naive = 北京时间


def test_unknown_calendar_and_negative_lag_rejected():
    with pytest.raises(ValueError, match="未知交易日历"):
        fr.expected_confirm_date(_at("2026-01-05", 10), 1, calendar="cn_holidays")
    with pytest.raises(ValueError):
        fr.add_trading_days(D("2026-01-05"), -1)
    assert fr.CALENDAR_WEEKDAY_ONLY == "weekday_only"


@pytest.mark.parametrize("texts,kind", [
    (("广发纳斯达克100ETF联接人民币(QDII)A", "指数型-海外股票"), "qdii"),
    (("南方全天候策略(FOF)A", "FOF-稳健型"), "fof"),
    (("易方达沪深300ETF联接A", "指数型-股票"), "default"),
    (("某 qdii-fof 基金",), "qdii"),                    # 同时命中 → QDII 优先；不分大小写
    ((None, ""), "default"),
    ((), "default"),
])
def test_classify_fund_kind(texts, kind):
    assert fr.classify_fund_kind(*texts) == kind


def test_resolve_lag_days_by_kind_with_fallbacks():
    table = {"default": 1, "qdii": 2, "fof": 3}
    assert fr.resolve_lag_days("qdii", table) == 2 and fr.resolve_lag_days("fof", table) == 3
    assert fr.resolve_lag_days("default", table) == 1
    assert fr.resolve_lag_days(None, table) == 1                                   # 类型没取到
    assert fr.resolve_lag_days("qdii", {"default": 4}) == 4                        # 表里没有这个类型 → default
    assert fr.resolve_lag_days("qdii", {"qdii": "x", "default": 1}) == 1           # 值坏 → default
    assert fr.resolve_lag_days("qdii", {"qdii": -1, "default": 1}) == 1
    assert fr.resolve_lag_days("qdii", {"qdii": 1.5}) == fr.DEFAULT_LAG_DAYS["default"]   # default 也坏 → 内置 1
    assert fr.resolve_lag_days("qdii", 2) == 1 and fr.resolve_lag_days("qdii", None) == 1  # env 误设成标量


def test_pick_nav_exact_shifted_or_unpublished():
    hist = [("2026-09-29", 1.0), ("2026-09-30", 1.1), ("2026-10-08", 1.2), ("2026-10-09", 1.3)]
    assert fr.pick_nav(hist, "2026-09-30") == ("2026-09-30", 1.1)                  # 当天有
    assert fr.pick_nav(hist, "2026-10-01") == ("2026-10-08", 1.2)                  # 国庆休市 → 顺延到下一个净值日
    assert fr.pick_nav(hist, "2026-10-10") is None                                 # 还没公布
    assert fr.pick_nav([], "2026-10-10") is None


def _o(amount=5000.0, expected="2026-01-06", **kw):
    return {"amount": amount, "currency": "CNY", "expected_confirm_date": expected, "lag_source": "profile", **kw}


def test_pending_summary_line():
    assert fr.pending_summary_line([], "2026-01-05") == ""
    one = fr.pending_summary_line([_o()], "2026-01-05")
    assert one == "在途待确认 1 笔（¥5,000.00），预计 2026-01-06"
    two = fr.pending_summary_line([_o(expected="2026-01-08"), _o(2000.0, expected="2026-01-07")], "2026-01-05")
    assert two == "在途待确认 2 笔（¥7,000.00），预计 2026-01-07"                       # 取最早的预计确认日
    guessed = fr.pending_summary_line([_o(lag_source="default", lag_days=1)], "2026-01-05")
    assert "按 T+1 估计" in guessed
    overdue = fr.pending_summary_line([_o()], "2026-01-08")
    assert "已超预计确认日" in overdue and "confirm_fund_order" in overdue
    assert "已超" not in fr.pending_summary_line([_o()], "2026-01-06")               # 预计日当天还不算超期
