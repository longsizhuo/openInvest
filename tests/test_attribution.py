"""calc/attribution.py —— 持仓当日涨跌归因，全部手算用例（纯函数，零 IO / 零 mock）。

守的契约：
- 每持仓 day_return = local_return + fx_contribution（汇率 × 价格交叉项算进汇率）
- Σ contribution = 组合日收益（昨日权重 × 当日收益，严格相加）；组合不含现金
- 缺价 / 缺汇率 / 非正数 → skipped，不进权重分母
- 基准同窗口 + 超额；top 涨 / 跌按贡献排序
"""
from __future__ import annotations

import math

import pytest

from openinvest.calc.attribution import attribute


def _h(symbol, units, p0, p1, f0=1.0, f1=1.0, **extra):
    return {"symbol": symbol, "units": units, "prev_close": p0, "close": p1, "fx_prev": f0, "fx": f1, **extra}


def _by(res, symbol):
    return next(r for r in res["holdings"] if r["symbol"] == symbol)


def test_base_currency_only_hand_computed():
    """A: 100×10→11 (+10%, V0=1000)；B: 50×20→19 (−5%, V0=1000)。组合 2000→2050 = +2.5%。"""
    res = attribute([_h("A", 100, 10, 11), _h("B", 50, 20, 19)],
                    {"prev_close": 100, "close": 101})
    a, b = _by(res, "A"), _by(res, "B")
    assert a["day_return_pct"] == pytest.approx(10.0) and b["day_return_pct"] == pytest.approx(-5.0)
    assert a["weight_pct"] == pytest.approx(50.0)
    assert a["contribution_pct"] == pytest.approx(5.0) and b["contribution_pct"] == pytest.approx(-2.5)
    assert res["portfolio"]["return_pct"] == pytest.approx(2.5)
    assert res["portfolio"]["value_prev_base"] == pytest.approx(2000) and res["portfolio"]["value_base"] == pytest.approx(2050)
    # 同币种：没有汇率影响
    assert a["fx_contribution_pct"] == 0 and res["portfolio"]["fx_contribution_pct"] == 0
    # 基准 +1%：组合超额 +1.5pp，A +9pp，B −6pp
    assert res["benchmark_return_pct"] == pytest.approx(1.0) and res["excess_pct"] == pytest.approx(1.5)
    assert a["excess_vs_benchmark_pct"] == pytest.approx(9.0) and b["excess_vs_benchmark_pct"] == pytest.approx(-6.0)
    # 按贡献降序；top 涨 / 跌
    assert [r["symbol"] for r in res["holdings"]] == ["A", "B"]
    assert res["top_contributors"] == ["A"] and res["top_detractors"] == ["B"]
    assert res["skipped"] == []


def test_foreign_holding_splits_fx_from_local_move():
    """USD 持仓 10 股：价 100→102（本币 +2%），汇率 7.00→7.14（+2%）。
    V0 = 10×100×7 = 7000，V1 = 10×102×7.14 = 7282.8 → 基准币种 +4.04%；
    汇率贡献 = 4.04 − 2 = 2.04%（= 2% × 1.02，含交叉项）。CNY 现金类持仓 1000 平盘。
    组合 8000 → 8282.8 = +3.535%；USD 持仓贡献 282.8/8000 = 3.535pp；组合汇率部分 = 7/8 × 2.04 = 1.785%。"""
    res = attribute([_h("US", 10, 100, 102, 7.0, 7.14, currency="USD"), _h("CN", 1000, 1, 1)])
    us = _by(res, "US")
    assert us["local_return_pct"] == pytest.approx(2.0)
    assert us["fx_return_pct"] == pytest.approx(2.0)
    assert us["day_return_pct"] == pytest.approx(4.04)
    assert us["fx_contribution_pct"] == pytest.approx(2.04)
    assert us["day_return_pct"] == pytest.approx(us["local_return_pct"] + us["fx_contribution_pct"])
    assert us["contribution_pct"] == pytest.approx(282.8 / 8000 * 100)
    assert res["portfolio"]["return_pct"] == pytest.approx(282.8 / 8000 * 100)
    assert res["portfolio"]["fx_contribution_pct"] == pytest.approx(7 / 8 * 2.04)
    assert _by(res, "CN")["contribution_pct"] == 0
    assert res["top_detractors"] == [] and res["top_contributors"] == ["US"]    # 零贡献不进榜


def test_fx_drag_turns_local_gain_negative():
    """本币 +1%，汇率 −3% → 基准币种 (1.01×0.97−1) = −2.03%；汇率贡献 −3.03%。"""
    res = attribute([_h("X", 100, 10, 10.1, 5.0, 4.85)])
    x = _by(res, "X")
    assert x["local_return_pct"] == pytest.approx(1.0)
    assert x["day_return_pct"] == pytest.approx(-2.03)
    assert x["fx_contribution_pct"] == pytest.approx(-3.03)
    assert res["top_detractors"] == ["X"]


def test_contributions_sum_to_portfolio_and_decompose():
    """任意组合：Σ contribution = 组合收益；每持仓 day = local + fx；Σ weight = 100。"""
    res = attribute([
        _h("A", 37, 12.3, 12.9, 1.0, 1.0),
        _h("B", 210, 3.14, 3.02, 6.8, 6.9),
        _h("C", 0.5, 2150.0, 2210.5, 7.1, 7.05),
        _h("D", 1200, 0.87, 0.87),
    ])
    assert sum(r["contribution_pct"] for r in res["holdings"]) == pytest.approx(res["portfolio"]["return_pct"], abs=1e-3)
    assert sum(r["weight_pct"] for r in res["holdings"]) == pytest.approx(100.0, abs=1e-3)
    for r in res["holdings"]:
        assert r["day_return_pct"] == pytest.approx(r["local_return_pct"] + r["fx_contribution_pct"], abs=1e-3)
    assert sum(r["weight_pct"] / 100 * r["fx_contribution_pct"] for r in res["holdings"]) \
        == pytest.approx(res["portfolio"]["fx_contribution_pct"], abs=1e-3)


def test_skipped_reasons_and_denominator_excludes_them():
    res = attribute([
        _h("OK", 100, 10, 11),
        _h("NOPX", 100, None, 11),
        _h("NOFX", 100, 10, 11, f1=None),
        _h("NAN", 100, 10, float("nan")),
        _h("ZERO", 0, 10, 11),
        _h("NEG", 100, -1, 11),
    ])
    assert res["skipped"] == [
        {"symbol": "NOPX", "reason": "missing_price"}, {"symbol": "NOFX", "reason": "missing_fx"},
        {"symbol": "NAN", "reason": "missing_price"}, {"symbol": "ZERO", "reason": "no_units"},
        {"symbol": "NEG", "reason": "missing_price"},
    ]
    assert [r["symbol"] for r in res["holdings"]] == ["OK"]
    assert res["holdings"][0]["weight_pct"] == pytest.approx(100.0)       # 分母只有 OK
    assert res["portfolio"]["return_pct"] == pytest.approx(10.0)


def test_nothing_computable_returns_none_portfolio():
    res = attribute([_h("A", 100, None, 1)], {"prev_close": 10, "close": 11})
    assert res["portfolio"] is None and res["holdings"] == [] and res["excess_pct"] is None
    assert res["benchmark_return_pct"] == pytest.approx(10.0)             # 基准照样算
    assert attribute([])["portfolio"] is None


@pytest.mark.parametrize("bench", [None, {}, {"prev_close": 0, "close": 5}, {"prev_close": 5},
                                   {"prev_close": 5, "close": float("nan")}])
def test_missing_or_invalid_benchmark_is_none_not_error(bench):
    res = attribute([_h("A", 100, 10, 11)], bench)
    assert res["benchmark_return_pct"] is None and res["excess_pct"] is None
    assert res["holdings"][0]["excess_vs_benchmark_pct"] is None
    assert res["portfolio"]["return_pct"] == pytest.approx(10.0)


def test_passthrough_fields_survive_and_inputs_are_not_rounded():
    """name/date 等多余字段带进输出；prev_close / fx 这类输入原样（4 位取整只作用在计算字段上）。"""
    res = attribute([_h("JPY", 1000, 100.0, 101.0, 0.04512345, 0.04523456, name="日元资产", date="2026-01-09")])
    r = res["holdings"][0]
    assert r["name"] == "日元资产" and r["date"] == "2026-01-09"
    assert r["fx_prev"] == 0.04512345 and r["fx"] == 0.04523456 and r["units"] == 1000


def test_top_n_and_ordering_are_deterministic():
    hs = [_h(s, 100, 10, 10 + d) for s, d in
          [("A", 1), ("B", 0.5), ("C", 0.5), ("D", 2), ("E", -1), ("F", -2), ("G", -0.3), ("H", -3)]]
    res = attribute(hs, top_n=2)
    assert res["top_contributors"] == ["D", "A"]
    assert res["top_detractors"] == ["H", "F"]
    # 同贡献按 symbol 稳定排序
    assert [r["symbol"] for r in res["holdings"]][:4] == ["D", "A", "B", "C"]
    assert all(math.isfinite(r["contribution_pct"]) for r in res["holdings"])
