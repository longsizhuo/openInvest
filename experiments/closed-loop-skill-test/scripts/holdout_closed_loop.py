"""闭环(有状态)holdout 回测 —— 修 ADR-022 T3。
每个决策日把模拟器【当前真实仓位】(含浮亏/集中度/剩余现金)喂进委员会 prompt,
让它像 live 一样能 de-risk → 才测得出择时 skill。

- 顺序跑(状态依赖,不能分片);周频决策(日频顺序太慢),日频 mark-to-market。
- 防穿越:仓位只由过去决策构成;summary 用 as-of-d 价(mark_to_market/_safe_close 都按 as_of)。
- 基准 = 同资产等权 buy-and-hold(holdout_perf.build_benchmarks)。
- 与"空桩 holdout 基线"对照:两者之差 = 给委员会真实仓位上下文值多少 skill。

用法: PYTHONPATH=/home/ubuntu/projects-review/invest INVEST_HOME=同 INVEST_MAX_DEBATE_ROUNDS=1 \
       uv run python holdout_closed_loop.py [END_DATE]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 同目录的 holdout_perf

from openinvest.core.paper_trade_simulator import PaperTradeSimulator
from openinvest.core.strategy_metrics import evaluate_strategy
from scripts.backtest_committee import run_one_day
from scripts.run_walk_forward import _trading_days_between, _generate_decision_dates, _safe_close
from openinvest.utils.portfolio_summary import portfolio_summary_text
from holdout_perf import build_benchmarks

ASSETS = ["GC=F", "510300.SS", "NDQ.AX"]
START, END = "2025-01-01", "2026-03-20"
STEP = 7            # 周频决策(闭环顺序跑;周频够测 de-risk)
INIT_CASH = 100_000.0


class _SimPM:
    """把 PaperTradeSimulator 状态 duck-type 成 portfolio_summary_text 要的 pm 接口。"""
    def __init__(self, sim, risk="Balanced"):
        self._acct = sim.account
        self.user = {"risk_tolerance": risk, "exchange_buffer_cny": 0.0}

    @property
    def holdings(self):
        return [{"symbol": s, "units": h.get("units", 0), "avg_cost": h.get("avg_cost", 0),
                 "cost_currency": h.get("ccy", "CNY"), "display_name": s,
                 "unit_label": "份", "channel": "", "is_tracking_only": False}
                for s, h in self._acct.holdings.items() if h.get("units", 0) > 0]

    def cash_amount(self, ccy):
        return float(self._acct.cash.get(ccy, 0.0))


def build_summary(sim, d):
    prices = {s: c for s in ASSETS if (c := _safe_close(s, d))}
    return portfolio_summary_text(_SimPM(sim), total_assets_cny=sim.mark_to_market(d),
                                  current_prices=prices)


import pandas as _pd
from openinvest.db.market_store import MarketStore as _MS


def vol_target_factor(sym, d):
    """R3-vol(conditional vol-target):当前 20 日年化波动 vs 该资产 2 年分布。
    >80 分位(高波动)→ 0.6(risk-off 买少);<20 分位(低波动)→ 1.4(用足风险预算买多);
    中间 → 1.0。封顶 [0.6,1.4],只作用于 BUY/ACCUMULATE,直治"欠配/现金空置"。
    零前视:只用 ≤d 数据。这是验证派批的"唯一 OOS 文献最硬、不赌方向"那条。"""
    df = _MS().get_history_df(sym, days=100000)
    if df is None or df.empty:
        return 1.0
    df = df[df.index <= _pd.to_datetime(d)]
    if len(df) < 80:
        return 1.0
    rets = df["Close"].pct_change().dropna()
    cur = rets.tail(20).std() * (252 ** 0.5)
    roll = (rets.rolling(20).std().dropna() * (252 ** 0.5)).tail(504)
    if not (cur > 0) or len(roll) < 60:
        return 1.0
    pct = float((roll < cur).mean())
    return 0.6 if pct >= 0.8 else (1.4 if pct <= 0.2 else 1.0)


def main():
    end = sys.argv[1] if len(sys.argv) > 1 else END
    vol_on = bool(os.getenv("INVEST_VOL_TARGET"))    # R3-vol overlay 开关
    tag = os.getenv("RUN_TAG", "")                    # 多并行跑写独立目录(CI 用)
    out = f".backtest_cl_{tag}" if tag else ".backtest_closedloop"
    dates = _generate_decision_dates(START, end, STEP)
    sim = PaperTradeSimulator(start_date=START, initial_cash_cny=INIT_CASH)
    trims = 0
    for i, d in enumerate(dates, 1):
        summary = build_summary(sim, d)
        res = run_one_day(d, ASSETS, resume=False, portfolio_summary_override=summary,
                          out_subdir=out)
        acts = []
        for sym, vd in res.get("verdicts", {}).items():
            if "error" in vd or vd.get("skipped"):
                continue
            if vol_on and str(vd.get("verdict", "")).upper() in ("BUY", "ACCUMULATE"):
                vd = {**vd, "alloc_cny": float(vd.get("alloc_cny", 0) or 0) * vol_target_factor(sym, d)}
            tx = sim.execute_verdict(d, sym, vd)
            acts.append(f"{sym}:{vd.get('verdict')}")
            if str(vd.get("verdict", "")).upper() in ("TRIM", "SELL"):
                trims += 1
        print(f"[{i}/{len(dates)}] {d} {acts}")
    tdays = _trading_days_between(START, end)
    sim.account.daily_values = [(dd, sim.mark_to_market(dd)) for dd in tdays]
    m = evaluate_strategy(sim.account.daily_values, sim.account.transactions,
                          build_benchmarks(START, end, INIT_CASH, ASSETS))
    print(f"\n=== 闭环 holdout {START}..{end} | {len(dates)} 决策日 | vol_target={vol_on} tag={tag or '-'} | TRIM/SELL={trims} ===")
    print(f"总收益 {m['total_return_pct']:+.2f}% | MaxDD {m['max_drawdown_pct']:.2f}% | Sharpe {m['sharpe_ratio']:.2f}")
    print(f"交易 BUY={m['n_buys']} SELL={m['n_sells']} HOLD={m['n_holds']}")
    for name, vs in m["vs_benchmarks"].items():
        print(f"  vs {name}: alpha {vs['alpha_pct']:+.2f}% (赢 {vs['beat_days_pct']:.0f}% 天)")
    print("\n关键看点:TRIM/SELL 次数 >0(空桩基线≈0)= 委员会真在 de-risk;MaxDD 比 buy-hold 小 = 择时 skill。")


if __name__ == "__main__":
    main()
