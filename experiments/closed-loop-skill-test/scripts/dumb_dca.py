"""傻瓜加速 DCA 基准 —— 无委员会、无 LLM,纯机械:前 N 个决策周等额把现金投进 3 资产,然后持有。
A/B 对照(验证派铁律):若闭环委员会(±vol-target)跑不赢这个傻瓜基准,说明它的"alpha"只是
"把钱投进了牛市",不是择时/sizing skill → 该退回更简单的 DCA。确定性,跑一次即可(无随机)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openinvest.core.paper_trade_simulator import PaperTradeSimulator
from openinvest.core.strategy_metrics import evaluate_strategy
from scripts.run_walk_forward import _trading_days_between, _generate_decision_dates
from holdout_perf import build_benchmarks

ASSETS = ["GC=F", "510300.SS", "NDQ.AX"]
START, END = "2025-01-01", "2026-03-20"
STEP = 7
INIT_CASH = 100_000.0
N_DEPLOY = 8  # 前 8 个决策周等额投完(加速 DCA)


def main():
    dates = _generate_decision_dates(START, END, STEP)
    sim = PaperTradeSimulator(start_date=START, initial_cash_cny=INIT_CASH)
    per = INIT_CASH / N_DEPLOY / len(ASSETS)
    for i, d in enumerate(dates):
        if i < N_DEPLOY:
            for sym in ASSETS:
                sim.execute_verdict(d, sym, {"verdict": "ACCUMULATE", "alloc_cny": per})
    tdays = _trading_days_between(START, END)
    sim.account.daily_values = [(dd, sim.mark_to_market(dd)) for dd in tdays]
    m = evaluate_strategy(sim.account.daily_values, sim.account.transactions,
                          build_benchmarks(START, END, INIT_CASH, ASSETS))
    print(f"=== 傻瓜加速 DCA(前 {N_DEPLOY} 周等额投完) ===")
    print(f"总收益 {m['total_return_pct']:+.2f}% | MaxDD {m['max_drawdown_pct']:.2f}% | Sharpe {m['sharpe_ratio']:.2f}")
    for name, vs in m["vs_benchmarks"].items():
        print(f"  vs {name}: alpha {vs['alpha_pct']:+.2f}%")


if __name__ == "__main__":
    main()
