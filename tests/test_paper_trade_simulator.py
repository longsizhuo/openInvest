"""阶段 3 测试：PaperTradeSimulator + strategy_metrics

mock 掉 yfinance + fx，只验交易逻辑、cash 守恒、mark-to-market 算式正确。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============ Fixtures ============

@pytest.fixture
def mock_market(monkeypatch):
    """Mock get_history_data + get_fx_rate，返回稳定可预测的价格 + 汇率"""
    import openinvest.utils.exchange_fee as ef
    import openinvest.utils.fx as fx
    import openinvest.core.paper_trade_simulator as pts

    # 价格表：每个 (symbol, date) → close 价
    # 模拟 NDQ.AX 在 AUD 计价，AAPL 在 USD 计价
    prices = {
        ("NDQ.AX", "2024-01-02"): 50.0,
        ("NDQ.AX", "2024-02-01"): 55.0,
        ("NDQ.AX", "2024-03-01"): 60.0,
        ("AAPL", "2024-01-02"): 180.0,
        ("AAPL", "2024-02-01"): 200.0,
        ("510300.SS", "2024-01-02"): 4.0,
        ("510300.SS", "2024-02-01"): 4.5,
    }

    def fake_get_history(symbol, period="2y", as_of_date=None):
        key = (symbol.upper(), as_of_date)
        if key in prices:
            return pd.DataFrame(
                {"Close": [prices[key]]},
                index=pd.to_datetime([as_of_date]),
            )
        # 默认返回某价（让 fx_rate 也能 work）
        return pd.DataFrame(
            {"Close": [100.0]},
            index=pd.to_datetime([as_of_date or "2024-01-02"]),
        )

    monkeypatch.setattr(ef, "get_history_data", fake_get_history)
    monkeypatch.setattr(pts, "get_history_data", fake_get_history)

    # FX：USDCNY=7.1 / AUDCNY=4.7 固定
    def fake_get_fx(quote, base="CNY", as_of_date=None):
        if quote == base:
            return 1.0
        return {"USD": 7.1, "AUD": 4.7, "HKD": 0.91}.get(quote.upper(), 1.0)

    monkeypatch.setattr(fx, "get_fx_rate", fake_get_fx)
    monkeypatch.setattr(pts, "get_fx_rate", fake_get_fx)

    return prices


# ============ PaperTradeSimulator 单测 ============

def test_initial_account(mock_market):
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02", initial_cash_cny=100000)
    assert sim.account.cash["CNY"] == 100000.0
    assert sim.account.cash["AUD"] == 0.0
    assert len(sim.account.holdings) == 0


def test_hold_verdict_no_action(mock_market):
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    tx = sim.execute_verdict("2024-01-02", "NDQ.AX",
                              {"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0})
    assert tx.action == "HOLD"
    assert sim.account.cash["CNY"] == 100000.0   # 没动
    assert "NDQ.AX" not in sim.account.holdings


def test_buy_uses_fx_to_convert_cny(mock_market):
    """BUY NDQ.AX 5000 CNY → 换汇 5000/4.7 ≈ 1063.83 AUD → 买 21.28 股 @ $50"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    tx = sim.execute_verdict("2024-01-02", "NDQ.AX",
                              {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5000})
    assert tx.action == "BUY"
    # 5000 CNY / 4.7 AUD/CNY = 1063.83 AUD / 50 USD = 21.28 股
    assert abs(tx.units - 21.276) < 0.01
    assert tx.cost_currency == "AUD"

    # cash 应扣 5000 CNY（先换汇成 1063.83 AUD 又花掉，所以 AUD=0 CNY=95000）
    assert abs(sim.account.cash["CNY"] - 95000) < 0.01
    assert abs(sim.account.cash["AUD"]) < 0.01

    # 持仓
    assert "NDQ.AX" in sim.account.holdings
    h = sim.account.holdings["NDQ.AX"]
    assert abs(h["units"] - 21.276) < 0.01
    assert abs(h["avg_cost"] - 50.0) < 0.001
    assert h["ccy"] == "AUD"


def test_buy_then_sell_round_trip(mock_market):
    """买 5000 CNY NDQ → 卖等额 → cash 守恒（无手续费）"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    sim.execute_verdict("2024-01-02", "NDQ.AX",
                        {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5000})
    # 卖出全部（在涨价那天）
    tx = sim.execute_verdict("2024-02-01", "NDQ.AX",
                              {"verdict": "SELL", "confidence": 0.9, "alloc_cny": 10000})
    assert tx.action == "SELL"
    # 持仓应清空
    assert "NDQ.AX" not in sim.account.holdings
    # AUD cash 应有钱（卖出 21.276 股 @ 55 = 1170.18 AUD）
    assert sim.account.cash["AUD"] > 1100


def test_buy_weighted_avg_cost(mock_market):
    """两笔 BUY 不同价位 → avg_cost 是加权平均"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    # 第一笔 @ 50：单价低
    sim.execute_verdict("2024-01-02", "NDQ.AX",
                        {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5000})
    # 第二笔 @ 55：单价高
    sim.execute_verdict("2024-02-01", "NDQ.AX",
                        {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5500})
    h = sim.account.holdings["NDQ.AX"]
    # avg_cost 应在 50-55 之间（加权平均，单位数也加权）
    assert 50 < h["avg_cost"] < 55


def test_sell_no_holding_skips(mock_market):
    """没持仓时 SELL → SKIP，不报错"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    tx = sim.execute_verdict("2024-01-02", "NDQ.AX",
                              {"verdict": "SELL", "confidence": 0.5, "alloc_cny": 5000})
    assert tx.action == "SKIP"
    assert "无持仓" in tx.note
    # cash 不变
    assert sim.account.cash["CNY"] == 100000


def test_buy_insufficient_cash_skips(mock_market):
    """现金不够时 SKIP"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02", initial_cash_cny=100)  # 只有 100 CNY
    tx = sim.execute_verdict("2024-01-02", "NDQ.AX",
                              {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5000})
    assert tx.action == "SKIP"
    assert "现金不足" in tx.note


def test_mark_to_market(mock_market):
    """买入后 mark-to-market 反映涨价"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    sim.execute_verdict("2024-01-02", "NDQ.AX",
                        {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 5000})
    # 2024-01-02 mark：用 50 价
    v1 = sim.mark_to_market("2024-01-02")
    assert abs(v1 - 100000) < 1  # 应跟初始资金接近

    # 2024-02-01 mark：用 55 价，账户应升值
    v2 = sim.mark_to_market("2024-02-01")
    assert v2 > v1   # NDQ 涨了，账户也涨


# ============ strategy_metrics 单测 ============

def test_total_return_pct():
    from openinvest.core.strategy_metrics import total_return_pct
    # 起始 100k → 120k：+20%
    daily = [("2024-01-01", 100000), ("2024-12-31", 120000)]
    assert total_return_pct(daily) == 20.0


def test_max_drawdown_pct():
    from openinvest.core.strategy_metrics import max_drawdown_pct
    daily = [("2024-01-01", 100), ("2024-02-01", 120),
             ("2024-03-01", 90), ("2024-04-01", 110)]
    # max=120 → drawdown 到 90 = -25%
    assert max_drawdown_pct(daily) == 25.0


def test_vs_benchmark_alpha():
    """策略 +20%，基准 +10% → alpha = 10%"""
    from openinvest.core.strategy_metrics import vs_benchmark
    strat = [("2024-01-01", 100), ("2024-12-31", 120)]
    bench = [("2024-01-01", 100), ("2024-12-31", 110)]
    result = vs_benchmark(strat, bench)
    assert result["alpha_pct"] == 10.0


def test_evaluate_strategy_full():
    """完整 evaluate_strategy 跑通不报错"""
    from openinvest.core.strategy_metrics import evaluate_strategy
    from openinvest.core.paper_trade_simulator import Transaction

    daily = [("2024-01-01", 100000), ("2024-06-30", 110000), ("2024-12-31", 115000)]
    txs = [
        Transaction("2024-01-01", "NDQ.AX", "BUY", 10, 50, "AUD", 2350, 4.7),
        Transaction("2024-06-30", "NDQ.AX", "SELL", 10, 60, "AUD", 2820, 4.7),
    ]
    benchmarks = {
        "余额宝": [("2024-01-01", 100000), ("2024-12-31", 101300)],   # +1.3%
        "沪深300": [("2024-01-01", 100000), ("2024-12-31", 95000)],   # -5%
    }
    metrics = evaluate_strategy(daily, txs, benchmarks)

    assert metrics["total_return_pct"] == 15.0
    assert "annualized_return_pct" in metrics
    assert "sharpe_ratio" in metrics
    assert metrics["n_buys"] == 1
    assert metrics["n_sells"] == 1
    # vs 余额宝 alpha ≈ +13.7%
    assert metrics["vs_benchmarks"]["余额宝"]["alpha_pct"] == 13.7
    # vs 沪深300 alpha ≈ +20%
    assert metrics["vs_benchmarks"]["沪深300"]["alpha_pct"] == 20.0


def test_gold_price_includes_usdcny_leg(mock_market):
    """回归（issue #179 P1-A①）：GC=F 报价是 USD/oz，但模拟器把黄金计价当 CNY
    （用户持积存金）。_get_price 必须补 USDCNY 腿，否则窗口收益漏掉人民币汇率漂移。
    """
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator

    sim = PaperTradeSimulator(initial_cash_cny=100000.0, start_date="2024-01-02")
    # mock_market 里未登记 GC=F → fake_get_history 回落 100.0；fake fx USD→CNY = 7.1
    price = sim._get_price("GC=F", "2024-01-02")
    assert price == pytest.approx(100.0 * 7.1)
    # 对照：非黄金资产不叠汇率腿（AAPL 的估值换算在别处按 USD 计价走 fx）
    assert sim._get_price("AAPL", "2024-01-02") == pytest.approx(180.0)


def test_buy_converts_non_cny_alloc_currency(mock_market):
    """verdict 带 alloc_currency=AUD 时，alloc_cny 实为澳元金额：1000 AUD = 4700 CNY（回测不能把它当人民币）"""
    from openinvest.core.paper_trade_simulator import PaperTradeSimulator
    sim = PaperTradeSimulator(start_date="2024-01-02")
    tx = sim.execute_verdict("2024-01-02", "NDQ.AX",
                              {"verdict": "BUY", "confidence": 0.8, "alloc_cny": 1000,
                               "alloc_currency": "AUD"})
    assert abs(sim.account.cash["CNY"] - (100000 - 4700)) < 0.01
    assert abs(tx.units - 20.0) < 0.01       # 1000 AUD / 50


def _curve_daily(returns: list[float], start: float = 100000.0) -> list[tuple[str, float]]:
    """把日收益率序列摊成 daily_values，省得每个用例重复写日期循环。"""
    import datetime as dt

    out: list[tuple[str, float]] = []
    val = start
    d = dt.date(2024, 1, 2)
    out.append((d.isoformat(), val))
    for r in returns:
        val *= 1 + r
        d += dt.timedelta(days=1)
        out.append((d.isoformat(), val))
    return out


def test_sortino_constant_loss_days_not_zero():
    """回归 #539：跌幅恒定时旧实现的分母是 0（对亏损日取 std），整条曲线退化成 0.0。"""
    from openinvest.core.strategy_metrics import sortino_ratio

    daily = _curve_daily([0.01] * 50 + [-0.005] * 10)
    s = sortino_ratio(daily)
    assert s != 0.0, "跌幅相同时分母仍为 0，说明下行偏差还在对负收益子集求 std"
    assert s == pytest.approx(57.33, abs=0.01)


def test_sortino_repo_own_case_not_zero():
    """回归 #539：仓库自带的 54 涨 / 6 跌 场景，旧实现同样返回 0.0。"""
    from openinvest.core.strategy_metrics import sortino_ratio

    daily = _curve_daily([0.005] * 54 + [-0.017] * 6)
    assert sortino_ratio(daily) == pytest.approx(8.09, abs=0.01)


def test_sortino_few_loss_days_monotonic():
    """边界 #539：亏损日少（1~4 天）给真实值，不再被下限压成 0；超剪裁线截到 100 而不是返 0，
    亏得越少 Sortino 越高（单调）。原 test_sortino_doesnt_explode 防的「小样本 1e15 爆掉」也由这里守。"""
    from openinvest.core.strategy_metrics import sortino_ratio

    assert sortino_ratio(_curve_daily([0.01] * 56 + [-0.02] * 4)) == pytest.approx(24.37, abs=0.01)
    by_loss = [sortino_ratio(_curve_daily([0.005] * 59 + [-x])) for x in (0.03, 0.01, 0.005, 0.001)]
    assert by_loss[0] == pytest.approx(17.86, abs=0.01)
    assert by_loss == sorted(by_loss)
    assert by_loss[-1] == 100.0
    assert sortino_ratio(_curve_daily([0.01] * 60)) == 100.0  # 没亏过 = 无穷大 → 上限
