"""读已烧好的 .backtest holdout verdicts → PaperTradeSimulator → strategy_metrics。
复用 run_walk_forward 的基准/评估 helper,不重跑委员会(省 957 次)。
只算 holdout 桶(date > CONTAMINATION_CUTOFF),报 alpha vs 同资产 buy-and-hold + Sharpe + MaxDD。
用法: PYTHONPATH=... uv run python holdout_perf.py [end_date]
"""
import re
import sys
from core.memory_store import MemoryStore
from core.committee import safe_symbol
from core.paper_trade_simulator import PaperTradeSimulator, get_asset_currency
from core.strategy_metrics import evaluate_strategy
from scripts.run_walk_forward import _buy_and_hold_curve, _trading_days_between


def build_benchmarks(start, end, cash, assets):
    """T2 正确基准:同资产等权 buy-and-hold(不是 run_walk_forward 那个含 AAPL 的幸存者篮)。
    + 纯现金参照。每个资产用 sim 同款计价币种,保证与策略曲线 apples-to-apples。"""
    tdays = _trading_days_between(start, end)
    per = cash / len(assets)
    eq = {d: 0.0 for d in tdays}
    for sym in assets:
        for d, v in _buy_and_hold_curve(sym, get_asset_currency(sym), start, end, per):
            eq[d] = eq.get(d, 0.0) + v
    return {
        "同资产等权buy&hold": sorted(eq.items()),
        "纯现金": [(d, cash) for d in tdays],
    }

CONTAMINATION_CUTOFF = "2024-12-31"
ASSETS = ["GC=F", "510300.SS", "NDQ.AX"]
START = "2025-01-01"
INIT_CASH = 100_000.0

_V = re.compile(r"\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s*([\d.]+)\)")
_A = re.compile(r"\*\*Suggested allocation CNY\*\*:\s*([\d.]+)")
_D = re.compile(r"\*\*Dominant view\*\*:\s*(.+)")


def parse_md(p):
    t = p.read_text(encoding="utf-8")
    v = _V.search(t)
    if not v:
        return None
    a = _A.search(t)
    d = _D.search(t)
    return {"verdict": v.group(1), "confidence": float(v.group(2)),
            "alloc_cny": float(a.group(1)) if a else 0.0,
            "dominant_view": d.group(1).strip() if d else ""}


def main():
    store = MemoryStore()
    bt = store.root / ".backtest"
    dates = sorted(d.name for d in bt.iterdir()
                   if d.is_dir() and d.name > CONTAMINATION_CUTOFF and d.name >= START)
    if len(sys.argv) > 1:
        end = sys.argv[1]
        dates = [d for d in dates if d <= end]
    if not dates:
        print("无 holdout 日期")
        return
    end = dates[-1]
    sim = PaperTradeSimulator(start_date=START, initial_cash_cny=INIT_CASH)
    n_verdict = 0
    for d in dates:
        for sym in ASSETS:
            f = bt / d / f"{safe_symbol(sym)}.md"
            if not f.exists():
                continue
            vd = parse_md(f)
            if vd:
                sim.execute_verdict(d, sym, vd)
                n_verdict += 1
    tdays = _trading_days_between(START, end)
    sim.account.daily_values = [(d, sim.mark_to_market(d)) for d in tdays]
    benchmarks = build_benchmarks(START, end, INIT_CASH, ASSETS)
    m = evaluate_strategy(sim.account.daily_values, sim.account.transactions, benchmarks)
    print(f"=== HOLDOUT 业绩(干净,1轮) {START}..{end} | {len(dates)} 决议日, {n_verdict} verdict ===")
    print(f"总收益 {m['total_return_pct']:+.2f}% | 年化 {m['annualized_return_pct']:+.2f}% | "
          f"MaxDD {m['max_drawdown_pct']:.2f}% | Sharpe {m['sharpe_ratio']:.2f}")
    print(f"交易 BUY={m['n_buys']} SELL={m['n_sells']} HOLD={m['n_holds']} SKIP={m['n_skips']}")
    print("vs 基准 (alpha = 策略 - 基准):")
    for name, vs in m["vs_benchmarks"].items():
        print(f"  vs {name}: {vs['alpha_pct']:+.2f}% (赢 {vs['beat_days_pct']:.0f}% 天)")


if __name__ == "__main__":
    main()
