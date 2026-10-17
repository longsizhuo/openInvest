"""calc/rebalance.py —— 再平衡清单纯计算：带宽 / 三种模式 / 整手取整 / 资金不足缩放 / 目标缺省平分。

全部手算可对账：总资产 100k，价格 / 汇率直接给，不碰 IO。
"""
from __future__ import annotations

import pytest

from openinvest.calc.rebalance import infer_lot_size, plan_rebalance


def _pos(symbol, value, target, price=10.0, lot=100, fx=1.0):
    return {"symbol": symbol, "value": value, "target_pct": target, "price": price, "fx": fx, "lot_size": lot}


def _plan(positions, **kw):
    kw.setdefault("cash", 26_000.0)
    kw.setdefault("total", 100_000.0)
    kw.setdefault("target_stock_pct", 0.82)
    kw.setdefault("target_cash_pct", 0.18)
    return plan_rebalance(positions, **kw)


def _by(plan):
    return {r["symbol"]: r for r in plan["rows"]}


# A 超配 +10pp（超 10k）、B 低配 −15pp（缺 15k）、C 低配 −3pp（带内）、现金多于目标 8k。
# 守恒：超配 10 + 多余现金 8 = 缺口 15 + 带内低配 3，所以 hybrid 只需卖 A 的 70%。
def _scenario():
    return [_pos("A", 40_000, 0.30, price=50), _pos("B", 10_000, 0.25, price=10), _pos("C", 24_000, 0.27, price=10)]


def test_within_band_does_nothing():
    plan = _plan([_pos("A", 32_000, 0.30), _pos("B", 38_000, 0.40)], cash=30_000, target_stock_pct=0.7, target_cash_pct=0.3)
    assert [r["action"] for r in plan["rows"]] == ["NONE", "NONE"]
    assert all("带宽内" in r["reason"] for r in plan["rows"])
    assert plan["totals"]["n_trades"] == 0 and plan["totals"]["turnover"] == 0
    assert plan["totals"]["cash_after"] == 30_000


def test_band_edge_is_inside_and_pct_reported_in_pp():
    plan = _plan([_pos("A", 35_000, 0.30)], cash=65_000, target_stock_pct=0.3, target_cash_pct=0.7)
    r = _by(plan)["A"]
    assert (r["current_pct"], r["target_pct"], r["drift_pct"]) == (35.0, 30.0, 5.0)   # 恰在 5pp 带边 → 不动
    assert r["action"] == "NONE"
    # 带宽收窄到 4pp 就超了；hybrid 没有要买的就不卖，sell_to_rebalance 才卖回目标
    narrow = dict(cash=65_000, target_stock_pct=0.3, target_cash_pct=0.7, band_pct=4)
    assert _by(_plan([_pos("A", 35_000, 0.30)], **narrow))["A"]["action"] == "NONE"
    assert _by(_plan([_pos("A", 35_000, 0.30)], mode="sell_to_rebalance", **narrow))["A"]["action"] == "SELL"


def test_cash_only_buys_with_spare_cash_and_never_sells():
    plan = _plan(_scenario(), mode="cash_only")
    r = _by(plan)
    assert r["A"]["action"] == "NONE" and "cash_only" in r["A"]["reason"]
    assert r["C"]["action"] == "NONE"
    assert (r["B"]["action"], r["B"]["units"], r["B"]["amount"]) == ("BUY", 800, 8_000.0)   # 多余现金 8k
    assert "资金不足" in r["B"]["reason"]
    assert plan["totals"]["cash_after"] == 18_000.0 and plan["totals"]["sell_total"] == 0


def test_hybrid_sells_only_what_the_shortfall_needs():
    plan = _plan(_scenario(), mode="hybrid")
    r = _by(plan)
    # 缺口 15k − 多余现金 8k = 7k → 卖 A 的 70% = 7k，一手 5k → 取整卖 1 手 = 5k
    assert (r["A"]["action"], r["A"]["units"], r["A"]["amount"]) == ("SELL", 100, 5_000.0)
    assert "70%" in r["A"]["reason"]
    assert (r["B"]["action"], r["B"]["units"], r["B"]["amount"]) == ("BUY", 1_300, 13_000.0)   # 8k + 5k
    assert plan["totals"]["cash_after"] == 18_000.0       # 回到目标现金 18%
    assert plan["totals"]["n_trades"] == 2 and plan["totals"]["turnover"] == 18_000.0
    assert [x["action"] for x in plan["rows"]] == ["SELL", "BUY", "NONE"]   # 卖在前买在后


def test_hybrid_does_not_sell_when_cash_covers_the_buys():
    # A 超配 8pp，但 C、D 各低配 4pp（带内）把它吸收了：B 缺口 7k ≤ 多余现金 7k → 不用卖 A
    pos = [_pos("A", 38_000, 0.30, price=50), _pos("B", 12_000, 0.19), _pos("C", 5_000, 0.09), _pos("D", 5_000, 0.09)]
    plan = _plan(pos, cash=40_000, target_stock_pct=0.67, target_cash_pct=0.33, mode="hybrid")
    r = _by(plan)
    assert r["A"]["action"] == "NONE" and "不卖" in r["A"]["reason"]
    assert (r["B"]["action"], r["B"]["amount"]) == ("BUY", 7_000.0) and "回到目标" in r["B"]["reason"]
    assert plan["totals"]["sell_total"] == 0 and plan["totals"]["cash_after"] == 33_000.0


def test_sell_to_rebalance_sells_all_overweight_back_to_target():
    plan = _plan(_scenario(), mode="sell_to_rebalance")
    r = _by(plan)
    assert (r["A"]["action"], r["A"]["units"], r["A"]["amount"]) == ("SELL", 200, 10_000.0)   # 全部超配 10k
    assert "卖出回到目标" in r["A"]["reason"]
    assert (r["B"]["action"], r["B"]["amount"]) == ("BUY", 15_000.0)                           # 缺口全补
    assert "回到目标" in r["B"]["reason"] and "资金不足" not in r["B"]["reason"]
    assert plan["totals"]["cash_after"] == 21_000.0       # 多卖的钱留在现金


def test_lot_rounding_floors_and_flags_sub_lot():
    # B 一手 = 100 股 × 100 = 10k；只有 8k 现金 → 不足一手
    pos = [_pos("A", 40_000, 0.30, price=50), _pos("B", 10_000, 0.25, price=100), _pos("C", 24_000, 0.27, price=10)]
    r = _by(_plan(pos, mode="cash_only"))
    assert r["B"]["action"] == "NONE" and r["B"]["units"] == 0 and "不足一手" in r["B"]["reason"]
    # 非整手价：B 一手 = 100 × 30 = 3k，8k → 2 手 = 6k（不是 8k）
    pos[1] = _pos("B", 10_000, 0.25, price=30)
    b = _by(_plan(pos, mode="cash_only"))["B"]
    assert (b["units"], b["amount"]) == (200, 6_000.0)


def test_by_amount_asset_has_no_lot_and_floors_to_whole_currency_unit():
    pos = [_pos("F", 10_000, 0.25, price=1.2345, lot=None), _pos("C", 24_000, 0.27, price=10),
           _pos("A", 40_000, 0.30, price=50)]
    f = _by(_plan(pos, mode="cash_only"))["F"]
    assert f["action"] == "BUY" and f["amount"] == 8_000.0 and f["lot_size"] is None
    assert f["units"] == pytest.approx(8_000 / 1.2345, abs=1e-3)


def test_foreign_currency_price_converted_through_fx():
    # 价格 100 USD，汇率 7 → 每股 700 基准币种，一手 70k > 8k → 不足一手；lot=1 时 11 股 = 7,700
    pos = [_pos("U", 10_000, 0.25, price=100, fx=7.0, lot=1), _pos("C", 24_000, 0.27), _pos("A", 40_000, 0.30, price=50)]
    u = _by(_plan(pos, mode="cash_only"))["U"]
    assert (u["units"], u["amount"]) == (11, 7_700.0)


def test_cash_shortfall_splits_by_target_weight_with_caps():
    # 现金 50k（目标 10% → 多余 40k）；B 缺 50k（目标 50%），C 缺 5k（目标 20%，带宽 3）
    pos = [_pos("A", 35_000, 0.20, price=1, lot=1), _pos("B", 0, 0.50, price=1, lot=1), _pos("C", 15_000, 0.20, price=1, lot=1)]
    r = _by(plan_rebalance(pos, cash=50_000, total=100_000, target_stock_pct=0.9, target_cash_pct=0.1,
                           band_pct=3, mode="cash_only"))
    # 按权重 .5:.2 本该 C 拿 11.4k，但缺口只有 5k → 封顶，多的额度给 B（35k，仍小于缺口 50k）
    assert r["C"]["amount"] == 5_000.0 and r["B"]["amount"] == 35_000.0
    assert "资金不足" in r["B"]["reason"] and "回到目标" in r["C"]["reason"]


def test_cash_shortfall_without_caps_is_proportional_to_target_weights():
    pos = [_pos("B", 0, 0.50, price=1, lot=1), _pos("C", 0, 0.20, price=1, lot=1)]
    plan = plan_rebalance(pos, cash=70_000, total=100_000, target_stock_pct=0.7, target_cash_pct=0.3, mode="cash_only")
    r = _by(plan)
    # 多余现金 40k，缺口 50k + 20k（都没封顶）→ 按权重 5:2 分
    assert r["B"]["amount"] == pytest.approx(40_000 * 5 / 7, abs=1)
    assert r["C"]["amount"] == pytest.approx(40_000 * 2 / 7, abs=1)
    assert plan["totals"]["buy_total"] <= 40_000


def test_no_spare_cash_explains_why():
    plan = _plan([_pos("B", 0, 0.50)], cash=30_000, target_stock_pct=0.5, target_cash_pct=0.3, mode="cash_only")
    r = _by(plan)["B"]
    assert r["action"] == "NONE" and "可用资金为 0" in r["reason"]


def test_missing_price_marks_none_with_reason():
    pos = [_pos("A", 40_000, 0.30, price=None), _pos("B", 10_000, 0.25)]
    a = _by(_plan(pos))["A"]
    assert a["action"] == "NONE" and "现价" in a["reason"]


def test_default_targets_split_stock_allocation_equally_and_are_labelled():
    pos = [_pos("A", 20_000, None), _pos("B", 20_000, None)]
    plan = _plan(pos, cash=60_000, target_stock_pct=0.7, target_cash_pct=0.3)
    r = _by(plan)
    assert (r["A"]["target_pct"], r["B"]["target_pct"]) == (35.0, 35.0)
    assert r["A"]["target_source"] == r["B"]["target_source"] == "equal_split"
    # 显式 + 缺省混用：缺省的拿「股票目标 − 已显式」
    mixed = _by(_plan([_pos("A", 20_000, 0.5), _pos("B", 20_000, None)], cash=60_000,
                      target_stock_pct=0.7, target_cash_pct=0.3))
    assert (mixed["A"]["target_source"], mixed["B"]["target_source"]) == ("strategy", "equal_split")
    assert mixed["B"]["target_pct"] == 20.0
    assert any("没写 target_pct" in w for w in plan["warnings"])
    assert not any("没写 target_pct" in w for w in _plan(_scenario())["warnings"])


def test_target_sum_mismatch_warns():
    plan = _plan([_pos("A", 20_000, 0.5)], cash=80_000, target_stock_pct=0.5, target_cash_pct=0.1)
    assert any("≠ 100%" in w for w in plan["warnings"])
    assert _plan(_scenario())["warnings"] == []


def test_cash_far_from_target_after_plan_warns_unless_fully_rebalanced():
    # 现金 10% 远低于目标 30%：hybrid 只卖够买入所需，现金拉不回去 → 提示；sell_to_rebalance 连现金一起回到目标
    pos = [_pos("A", 60_000, 0.30, price=1, lot=1), _pos("B", 30_000, 0.40, price=1, lot=1)]
    kw = dict(cash=10_000, target_stock_pct=0.7, target_cash_pct=0.3)
    hybrid = _plan(pos, mode="hybrid", **kw)
    assert hybrid["totals"]["cash_after"] == 10_000.0
    assert any("计划后现金" in w and "sell_to_rebalance" in w for w in hybrid["warnings"])
    full = _plan(pos, mode="sell_to_rebalance", **kw)
    assert full["totals"]["cash_after"] == 30_000.0 and full["warnings"] == []


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        _plan(_scenario(), mode="yolo")
    with pytest.raises(ValueError):
        _plan(_scenario(), total=0)


@pytest.mark.parametrize("symbol,kind,by_amount,lot,note", [
    ("510300.SS", "equity", False, 100, ""),
    ("000001.SZ", "equity", False, 100, ""),
    ("832000.BJ", "equity", False, 100, ""),
    ("688981.SS", "equity", False, 100, "科创板"),
    ("113050.SS", "bond", False, 10, "可转债"),
    ("127045.SZ", "equity", False, 10, "可转债"),          # 代码段识别，kind 没标 bond 也算
    ("FUND:110020", "fund", True, None, ""),
    ("1024.HK", "equity", False, 1, "手数未知"),
    ("AAPL", "equity", False, 1, ""),
    ("NDQ.AX", "etf", False, 1, ""),
    ("BTC-USD", "crypto", False, None, ""),
    ("GC=F", "metal", False, None, ""),
])
def test_infer_lot_size(symbol, kind, by_amount, lot, note):
    got_lot, got_note = infer_lot_size(symbol, kind, by_amount)
    assert got_lot == lot and note in got_note
