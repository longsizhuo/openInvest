"""calc/account_metrics.py —— 手工构造 5~10 天序列，TWR / XIRR / MDD / 波动 / vs 基准逐个对数。"""
from __future__ import annotations

import math

import pytest

from openinvest.calc.account_metrics import (
    account_metrics,
    account_xirr,
    annual_volatility_pct,
    daily_returns,
    linked_index,
    max_drawdown,
    twr_windows,
    xirr,
)

# (date, nav_end, inflow, outflow)
ROWS = [
    ("2026-01-05", 1100.0, 0.0, 0.0),     # 1000 → 1100：+10%
    ("2026-01-06", 1700.0, 500.0, 0.0),   # 存 500：(1700−1100−500)/(1100+500) = +6.25%
    ("2026-01-07", 1600.0, 0.0, 200.0),   # 取 200：(1600+200−1700)/1700 = +5.882%
    ("2026-01-08", 1600.0, 0.0, 0.0),     # 0%
    ("2026-01-09", 1440.0, 0.0, 0.0),     # −10%
]


def test_daily_returns_wealthfolio_formula():
    rets = dict(daily_returns(ROWS, opening_nav=1000.0))
    assert rets["2026-01-05"] == pytest.approx(0.10)
    assert rets["2026-01-06"] == pytest.approx(0.0625)
    assert rets["2026-01-07"] == pytest.approx(100 / 1700)
    assert rets["2026-01-08"] == pytest.approx(0.0)
    assert rets["2026-01-09"] == pytest.approx(-0.10)


def test_twr_all_is_chain_linked_not_money_weighted():
    # 手算：1.1 × 1.0625 × (1800/1700) × 1.0 × 0.9 − 1 = 11.375%
    expected = (1.1 * 1.0625 * (1800 / 1700) * 0.9 - 1) * 100
    w = twr_windows(daily_returns(ROWS, 1000.0), today="2026-01-09")
    assert w["all"] == pytest.approx(round(expected, 2))
    # 成立不足 30 天 → 30d 不报；成立在今年内 → ytd = 成立以来
    assert w["30d"] is None
    assert w["ytd"] == w["all"]


def test_twr_30d_and_ytd_anchor_on_dates():
    rows = [
        ("2025-12-30", 1000.0, 0.0, 0.0),
        ("2025-12-31", 1100.0, 0.0, 0.0),   # +10%，去年
        ("2026-01-10", 1210.0, 0.0, 0.0),   # +10%，今年但在 30d 窗外（今天 2026-02-15）
        ("2026-01-20", 1331.0, 0.0, 0.0),   # +10%，30d 窗内
        ("2026-02-14", 1464.1, 0.0, 0.0),   # +10%，30d 窗内
    ]
    w = twr_windows(daily_returns(rows, 1000.0), today="2026-02-15")
    assert w["all"] == pytest.approx(46.41)
    assert w["ytd"] == pytest.approx(33.1)        # 12-31 之后三段
    assert w["30d"] == pytest.approx(21.0)        # 01-16 之后两段


def test_xirr_two_cashflows_hand_computable():
    # −1000 → 一年后 +1100：(1.1)^(365.25/365) − 1 ≈ 10.007%
    r = xirr([("2026-01-01", -1000.0), ("2027-01-01", 1100.0)])
    assert r == pytest.approx(((1.1) ** (365.25 / 365) - 1) * 100, abs=0.01)
    # 单向现金流无解
    assert xirr([("2026-01-01", -1.0), ("2026-06-01", -1.0)]) is None


def test_account_xirr_needs_30_days_and_includes_terminal_nav():
    assert account_xirr(ROWS, 1000.0) is None      # 跨度 4 天
    rows = [("2026-01-01", 1000.0, 0.0, 0.0), ("2026-07-02", 1050.0, 0.0, 0.0)]
    r = account_xirr(rows, 1000.0)
    assert r is not None and r == pytest.approx(((1.05) ** (365.25 / 182) - 1) * 100, abs=0.05)


def test_max_drawdown_peak_and_trough_dates():
    idx = linked_index([("d1", 0.2), ("d2", -0.25), ("d3", 0.1), ("d4", -0.5)])
    # 指数 1.2 → 0.9 → 0.99 → 0.495：最深 = 0.495/1.2 − 1 = −58.75%
    mdd = max_drawdown(idx, opening_date="d0")
    assert mdd == {"pct": -58.75, "peak": "d1", "trough": "d4"}
    # 单调上涨：无回撤
    assert max_drawdown(linked_index([("d1", 0.1), ("d2", 0.1)]), "d0")["pct"] == 0.0


def test_volatility_is_stdev_of_log_returns_times_sqrt_252():
    rets = daily_returns(ROWS, 1000.0)
    logs = [math.log1p(r) for _, r in rets]
    mean = sum(logs) / len(logs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in logs) / (len(logs) - 1))
    assert annual_volatility_pct(rets) == pytest.approx(round(sd * math.sqrt(252) * 100, 2))
    assert annual_volatility_pct(rets[:1]) is None


def test_account_metrics_block_with_benchmark():
    closes = {"2026-01-02": 100.0, "2026-01-05": 102.0, "2026-01-08": 110.0}   # 01-06/07/09 前值填充
    m = account_metrics(ROWS, 1000.0, "2026-01-09",
                        benchmark_symbol="000300.SS", benchmark_closes=closes)
    assert m["since"] == "2026-01-05" and m["as_of"] == "2026-01-09" and m["nav"] == 1440.0
    b = m["benchmark"]
    # 锚在首日 01-05 收盘（与 opening_nav 同一时点）：110/102 − 1，不是 01-02 的 100 起算
    assert b["symbol"] == "000300.SS" and b["return_same_window"] == pytest.approx(7.8431, abs=1e-3)
    assert b["excess"] == pytest.approx(m["twr_all"] - 7.8431, abs=0.01)   # 各自 round
    assert m["xirr"] is None and m["max_drawdown"]["pct"] == -10.0
    # 没有基准 → None，其余字段照常
    assert account_metrics(ROWS, 1000.0, "2026-01-09")["benchmark"] is None
    # 基准历史晚于首日 → None（IO 壳标 starts_after_inception）
    assert account_metrics(ROWS, 1000.0, "2026-01-09", benchmark_symbol="X",
                           benchmark_closes={"2026-01-08": 1.0, "2026-01-09": 1.1})["benchmark"] is None


def test_external_flow_does_not_count_as_return():
    # 只存钱不涨跌：TWR 0、回撤 0
    rows = [("2026-01-05", 1000.0, 1000.0, 0.0), ("2026-01-06", 3000.0, 2000.0, 0.0)]
    m = account_metrics(rows, 0.0, "2026-01-06")
    assert m["twr_all"] == 0.0 and m["max_drawdown"]["pct"] == 0.0
