"""回归:TRIM/SELL 的负 alloc_cny 必须真成交,不能被 `<=0` 守卫吞成 HOLD。

闭环 skill 测试(experiments/closed-loop-skill-test)发现的 bug:委员会 TRIM 输出负
alloc_cny(如 -5000),旧 execute_verdict 的 `if alloc_cny <= 0: HOLD` 把它全吞了 →
SELL/TRIM 从不成交(16 次 TRIM 全 no-op),曾被误判成"委员会会 de-risk"。修复:取
qty_cny = abs(alloc_cny),方向由 direction 决定。详见 docs/wiki/17。
"""
from core.paper_trade_simulator import PaperTradeSimulator


def test_trim_negative_alloc_executes_sell(monkeypatch):
    sim = PaperTradeSimulator(start_date="2025-01-02", initial_cash_cny=100_000.0)
    monkeypatch.setattr(sim, "_get_price", lambda asset, date: 600.0)  # 固定价,不依赖行情库

    # 1) 先用正 alloc 建仓
    sim.execute_verdict("2025-01-02", "GC=F",
                        {"verdict": "ACCUMULATE", "confidence": 0.6, "alloc_cny": 50_000})
    held = sim.account.holdings.get("GC=F", {}).get("units", 0)
    assert held > 0, "建仓失败,无法测减仓"

    # 2) 负 alloc 的 TRIM —— 旧 bug 会被吞成 HOLD
    tx = sim.execute_verdict("2025-01-09", "GC=F",
                             {"verdict": "TRIM", "confidence": 0.6, "alloc_cny": -20_000})
    assert tx.action in ("SELL", "TRIM"), f"TRIM 负 alloc 被吞成 {tx.action}(回归 <=0 bug)"
    assert sim.account.holdings.get("GC=F", {}).get("units", 0) < held, "TRIM 后持仓未减少"
    n_sells = sum(1 for t in sim.account.transactions if t.action in ("SELL", "TRIM"))
    assert n_sells >= 1, "无 SELL/TRIM 成交"


def test_hold_and_zero_alloc_still_noop(monkeypatch):
    sim = PaperTradeSimulator(start_date="2025-01-02", initial_cash_cny=100_000.0)
    monkeypatch.setattr(sim, "_get_price", lambda asset, date: 600.0)
    assert sim.execute_verdict("2025-01-02", "GC=F", {"verdict": "HOLD", "alloc_cny": 0}).action == "HOLD"
    assert sim.execute_verdict("2025-01-02", "GC=F", {"verdict": "ACCUMULATE", "alloc_cny": 0}).action == "HOLD"
