"""run_walk_forward.py — paper trading walk-forward 主入口

把 backtest_committee 的 verdict 流 + PaperTradeSimulator 串起来：
1. 在 backtest_runner 的隔离 workspace 里跑
2. 沿时间线推进，每个 decision_date 跑 4 角色委员会
3. 用 PaperTradeSimulator 模拟"每次都听 AI"的账户操作
4. 沿时间线每天 mark-to-market 得到完整 P&L 曲线
5. 同期算基准曲线（buy-and-hold / 余额宝 / 沪深300 / 等权）
6. 用 strategy_metrics 算完整 metrics
7. 落 JSON + Markdown 报告

用法（必须先 setup workspace 隔离，所以走 backtest_runner 包装）：
    python -m scripts.backtest_runner \\
        --workspace /tmp/wf_test \\
        --start 2024-01-02 --end 2024-03-29 --step 5 \\
        --assets NDQ.AX,GC=F,510300.SS,AAPL \\
        --walk-forward

注：必须由 backtest_runner 调用（它已 monkey-patch 数据路径），不直接跑。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


def _generate_decision_dates(start: str, end: str, step_days: int) -> List[str]:
    """生成 walk-forward 的 decision_date 列表（跳周末）

    边界处理：start 落周末时先推到下个周一，否则 step=7 永远落周末，
    decision_dates 会是空列表（曾 silent fail 让 Optuna trial 全 reward=-10）。
    """
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    # start 落周六/周日 → 推到周一
    while start_d.weekday() >= 5 and start_d <= end_d:
        start_d += timedelta(days=1)
    dates: List[str] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=step_days)
    return dates


def _trading_days_between(start: str, end: str) -> List[str]:
    """生成 [start, end] 之间的所有交易日（跳周末）"""
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    dates: List[str] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates


def _build_benchmark_curves(
    start: str, end: str, initial_cash_cny: float, assets: "list | None" = None,
) -> Dict[str, List[Tuple[str, float]]]:
    """基准曲线:`buy_hold`(实际交易资产的等权 buy-and-hold,T2 正确的 headline,reward 锚=它)
    + 余额宝 + 沪深300 + 纯现金 + 等权组合(legacy,含 AAPL,勿当 headline)。"""
    from utils.exchange_fee import get_history_data
    from core.paper_trade_simulator import get_asset_currency
    trading_days = _trading_days_between(start, end)
    if not trading_days:
        return {}

    benchmarks: Dict[str, List[Tuple[str, float]]] = {}

    # 1. 余额宝：年化 1.3%，按日复利
    yuebao_curve = []
    daily_rate = 0.013 / 252
    val = initial_cash_cny
    for d in trading_days:
        yuebao_curve.append((d, val))
        val *= (1 + daily_rate)
    benchmarks["余额宝"] = yuebao_curve

    # 2. 沪深 300（510300.SS 代理）：一开始 all-in，跟着指数走
    benchmarks["沪深300"] = _buy_and_hold_curve(
        "510300.SS", "CNY", start, end, initial_cash_cny
    )

    # 3. 纯现金（不操作）：永远是 initial
    benchmarks["纯现金"] = [(d, initial_cash_cny) for d in trading_days]

    # 4. 等权组合：4 资产各 25% 起始投入
    # 简化：每个资产单独 buy-and-hold 25k，累加曲线
    eq_assets = [
        ("NDQ.AX", "AUD", initial_cash_cny / 4),
        ("GC=F", "CNY", initial_cash_cny / 4),
        ("AAPL", "USD", initial_cash_cny / 4),
        ("510300.SS", "CNY", initial_cash_cny / 4),
    ]
    eq_curve: Dict[str, float] = {d: 0.0 for d in trading_days}
    for sym, ccy, cash in eq_assets:
        sub = _buy_and_hold_curve(sym, ccy, start, end, cash)
        sub_dict = dict(sub)
        for d in trading_days:
            eq_curve[d] += sub_dict.get(d, cash)
    benchmarks["等权组合"] = [(d, v) for d, v in sorted(eq_curve.items())]

    # 5. buy_hold：实际交易资产的等权 buy-and-hold —— T2 正确的 headline 基准,reward 锚=它
    #    (不是上面含 AAPL 的 legacy 等权组合;ADR-022 T5)。无 assets 时退回三资产默认。
    bh_assets = assets or ["NDQ.AX", "GC=F", "510300.SS"]
    per = initial_cash_cny / len(bh_assets)
    bh: Dict[str, float] = {d: 0.0 for d in trading_days}
    for sym in bh_assets:
        for d, v in _buy_and_hold_curve(sym, get_asset_currency(sym), start, end, per):
            bh[d] += v
    benchmarks["buy_hold"] = [(d, v) for d, v in sorted(bh.items())]

    return benchmarks


def _buy_and_hold_curve(
    symbol: str, ccy: str, start: str, end: str, initial_cash_cny: float,
) -> List[Tuple[str, float]]:
    """在 start 当天 all-in 买入 symbol，到 end 每天 mark-to-market"""
    from utils.exchange_fee import get_history_data
    from utils.fx import get_fx_rate

    trading_days = _trading_days_between(start, end)
    if not trading_days:
        return []

    # start 当天的 price + fx
    start_price = _safe_close(symbol, start)
    start_fx = get_fx_rate(ccy, "CNY", as_of_date=start) or _fallback_fx(ccy)
    if start_price is None or start_price <= 0:
        # 兜底：返回纯现金曲线
        return [(d, initial_cash_cny) for d in trading_days]

    # 换汇 + 买入
    units = (initial_cash_cny / start_fx) / start_price

    curve = []
    for d in trading_days:
        price = _safe_close(symbol, d) or start_price
        fx = get_fx_rate(ccy, "CNY", as_of_date=d) or start_fx
        curve.append((d, round(units * price * fx, 2)))

    return curve


def _safe_close(symbol: str, date_str: str) -> Optional[float]:
    from utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "5d", as_of_date=date_str)
    if df is None or df.empty:
        return None
    try:
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def _fallback_fx(ccy: str) -> float:
    return {"CNY": 1.0, "USD": 7.1, "AUD": 4.7, "HKD": 0.91}.get(ccy.upper(), 1.0)


def run_walk_forward(
    start: str, end: str, step_days: int, assets: List[str],
    initial_cash_cny: float = 100_000.0,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """主入口：跑完整 walk-forward + 算 metrics + 落盘

    必须在 backtest workspace 隔离环境内调用（数据路径已被 backtest_runner monkey-patch）
    """
    from core.paper_trade_simulator import PaperTradeSimulator
    from core.strategy_metrics import evaluate_strategy
    from scripts.backtest_committee import run_one_day

    log.info(f"📅 walk-forward {start} → {end} 每 {step_days} 天 × {len(assets)} 资产")

    decision_dates = _generate_decision_dates(start, end, step_days)
    if not decision_dates:
        raise ValueError("decision_dates 列表为空 — 检查 start/end/step")

    log.info(f"   decision_dates: {len(decision_dates)} 个")

    # 1. 初始化 simulator
    sim = PaperTradeSimulator(start_date=start, initial_cash_cny=initial_cash_cny)

    # 2. 每个 decision_date 跑委员会 + 执行 verdict
    for i, d in enumerate(decision_dates, 1):
        log.info(f"[{i}/{len(decision_dates)}] {d} ...")
        try:
            # resume=False：walk_forward 每次从零重建 simulator，必须拿到每天真实
            # verdict 回放成交；断点续跑会跳过已写日期 → 成交集残缺、指标静默失真。
            day_result = run_one_day(d, assets, resume=False)
        except Exception as e:
            log.error(f"  {d} 失败: {e}")
            continue

        # run_one_day 返回 {"verdicts": {sym: {"verdict": "BUY", "confidence": 0.7, "alloc_cny": 5000}}}
        for sym, asset_data in day_result.get("verdicts", {}).items():
            if "error" in asset_data:
                log.warning(f"    {sym}: error {asset_data['error'][:80]}")
                continue
            if asset_data.get("skipped"):
                continue  # run_one_day 断点续跑跳过的残影，别当成空 HOLD 事务记账
            # asset_data 直接是 verdict dict（不嵌套）
            tx = sim.execute_verdict(d, sym, asset_data)
            log.info(f"    {sym}: {asset_data.get('verdict', '?')} → {tx.action} (alloc={asset_data.get('alloc_cny', 0)})")

    # 3. 沿每个交易日 mark-to-market
    log.info("📊 mark-to-market 沿时间线...")
    trading_days = _trading_days_between(start, end)
    sim.account.daily_values = []
    for d in trading_days:
        v = sim.mark_to_market(d)
        sim.account.daily_values.append((d, v))

    # 4. 算基准曲线（传 assets → buy_hold 用实际交易资产,非含 AAPL 的 legacy 篮）
    log.info("📈 计算基准曲线...")
    benchmark_curves = _build_benchmark_curves(start, end, initial_cash_cny, assets=assets)
    assert "buy_hold" in benchmark_curves, "reward 锚 buy_hold 缺失(R1/ADR-022 T5)"

    # 5. evaluate metrics
    log.info("🧮 evaluate strategy...")
    metrics = evaluate_strategy(
        sim.account.daily_values,
        sim.account.transactions,
        benchmark_curves,
    )

    # 6. 完整结果
    result = {
        "config": {
            "start": start, "end": end, "step_days": step_days,
            "assets": assets, "initial_cash_cny": initial_cash_cny,
        },
        "metrics": metrics,
        "simulator": sim.serialize(),
        "benchmark_curves": {
            name: curve for name, curve in benchmark_curves.items()
        },
    }

    # 7. 落盘
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        log.info(f"💾 结果已写入 {output_path}")

    # 8. 控制台简短总结
    print("\n" + "=" * 60)
    print(f"📊 Walk-forward 完成")
    print(f"   总收益:     {metrics['total_return_pct']:+.2f}%")
    print(f"   年化:       {metrics['annualized_return_pct']:+.2f}%")
    print(f"   最大回撤:   {metrics['max_drawdown_pct']:.2f}%")
    print(f"   Sharpe:     {metrics['sharpe_ratio']:.2f}")
    print(f"   交易次数:   BUY={metrics['n_buys']}  SELL={metrics['n_sells']}  HOLD={metrics['n_holds']}  SKIP={metrics['n_skips']}")
    print(f"\n   vs 基准（alpha = 策略 - 基准）：")
    for name, vs in metrics["vs_benchmarks"].items():
        print(f"   - vs {name}: {vs['alpha_pct']:+.2f}% (赢 {vs['beat_days_pct']:.0f}% 天)")
    print("=" * 60)

    return result


def main():
    """CLI 入口。由 backtest_runner 透传调用（已在隔离 workspace 内）"""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--step", type=int, default=7)
    p.add_argument("--assets", required=True, help="逗号分隔")
    p.add_argument("--initial-cash", type=float, default=100_000.0)
    p.add_argument("--out", help="输出 JSON 路径")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    assets = [s.strip() for s in args.assets.split(",") if s.strip()]
    out = Path(args.out) if args.out else None

    run_walk_forward(
        args.start, args.end, args.step, assets,
        initial_cash_cny=args.initial_cash, output_path=out,
    )


if __name__ == "__main__":
    main()
