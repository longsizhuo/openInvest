"""收益率回测评估 —— 把已落盘的 committee verdict 当交易信号，跑真实净值曲线，
按 TradingAgents 口径出 CR / AR / Sharpe / MaxDD，对比 Buy-and-Hold / 纯 regime / 现金。

为什么：单期 _is_hit 方向命中率在牛市里只是"漂移计"（always-buy 自动赢）。改成
风险调整 + 相对基准，才看得见委员会真正的价值（仓位 sizing / 回撤规避）。本脚本
**只读已落盘 verdict + OHLC，不跑委员会**，所以秒级、确定性、可反复跑。

用法：
  uv run python -m scripts.research.backtest_eval \
      --verdicts preset=/tmp/full_validation_results.jsonl \
      --verdicts naked=/tmp/fv_after.jsonl \
      [--mapping relative|absolute] [--init-exposure 0.5]

verdict→仓位是路径依赖的状态机（verdict 是仓位*变化*信号）。映射是可调假设，
默认跑 relative；--mapping absolute 做稳健性检查。不 import core.committee。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.db.market_store import MarketStore  # noqa: E402

TRADING_DAYS = 252
LAST_HOLD_DAYS = 30  # 最后一个决策持有多久（让其持有期能 realized）

# regime → 仓位（纯机械 regime 策略基线，不用 LLM）
REGIME_EXPOSURE = {
    "uptrend": 1.0, "recovery": 1.0, "range_bound": 0.5,
    "downtrend": 0.0, "crash": 0.0, "unknown": 0.5, None: 0.5,
}


def _relative(cur: float, v: str) -> float:
    if v == "BUY": return 1.0
    if v == "ACCUMULATE": return min(1.0, cur + 0.25)
    if v == "TRIM": return cur * 0.5
    if v == "SELL": return 0.0
    return cur  # HOLD / UNCLEAR / 未知 → 维持


def _absolute(cur: float, v: str) -> float:
    return {"BUY": 1.0, "ACCUMULATE": 0.75, "TRIM": 0.25, "SELL": 0.0}.get(v, cur)


def _conservative(cur: float, v: str) -> float:
    # 风险厌恶读法：永不满仓、加仓慢、减仓狠。验证结论不靠"激进解读 verdict"。
    if v == "BUY": return 0.6
    if v == "ACCUMULATE": return min(0.6, cur + 0.15)
    if v == "TRIM": return cur * 0.4
    if v == "SELL": return 0.0
    return cur  # HOLD / UNCLEAR / 未知 → 维持


MAPPINGS = {"relative": _relative, "absolute": _absolute, "conservative": _conservative}


def load_verdicts(path: str) -> Dict[str, List[Tuple[str, str, str]]]:
    """→ {asset: [(date, verdict, regime), ...]} 按日期升序。"""
    out: Dict[str, List[Tuple[str, str, str]]] = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") != "ok":
            continue
        a = r.get("asset")
        out.setdefault(a, []).append(
            (r["date"], str(r.get("verdict") or "HOLD"), r.get("regime_at_decision"))
        )
    for a in out:
        out[a].sort()
    return out


def close_series(asset: str) -> pd.Series:
    df = MarketStore().get_history_df(asset, days=100000)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    s = df["Close"].copy()
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def exposure_path(decisions, close_idx, mapfn, init):
    """把决策序列展开成对齐到 close_idx 每日的仓位（决策当日起向前生效）。"""
    exp_by_date = {}
    cur = init
    for d, v, _ in decisions:
        cur = mapfn(cur, v)
        exp_by_date[pd.Timestamp(d)] = cur
    # 对齐到每个交易日：取 <= t 的最近一次决策仓位；决策前用 init
    dec_dates = sorted(exp_by_date)
    series = pd.Series(index=close_idx, dtype=float)
    for t in close_idx:
        prior = [dd for dd in dec_dates if dd <= t]
        series[t] = exp_by_date[prior[-1]] if prior else init
    return series


def sim_daily_returns(exposure: pd.Series, close: pd.Series) -> pd.Series:
    """组合日收益 = 昨日仓位 × 标的当日收益（signal at close → 持有到次日，无未来泄漏）。"""
    ret = close.pct_change().fillna(0.0)
    return (exposure.shift(1).fillna(exposure.iloc[0]) * ret)


def metrics(dr: pd.Series) -> dict:
    eq = (1 + dr).cumprod()
    n = len(dr)
    cr = float(eq.iloc[-1] - 1) if n else 0.0
    ar = float((1 + cr) ** (TRADING_DAYS / n) - 1) if n > 5 else float("nan")
    sd = dr.std()
    sharpe = float(dr.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 1e-12 else float("nan")
    dd = float((eq / eq.cummax() - 1).min()) if n else 0.0
    return {"CR": cr, "AR": ar, "Sharpe": sharpe, "MaxDD": dd}


def run_asset(asset, decisions, mapfn, init):
    """返回该资产各策略的日收益序列 dict。"""
    close = close_series(asset)
    if close.empty or len(decisions) < 2:
        return None
    d0 = pd.Timestamp(decisions[0][0])
    dN = pd.Timestamp(decisions[-1][0]) + pd.Timedelta(days=LAST_HOLD_DAYS)
    close = close[(close.index >= d0) & (close.index <= dN)]
    if len(close) < 6:
        return None
    idx = close.index
    out = {}
    # committee
    out["committee"] = sim_daily_returns(exposure_path(decisions, idx, mapfn, init), close)
    # baselines
    out["buy_hold"] = sim_daily_returns(pd.Series(1.0, index=idx), close)
    out["cash"] = pd.Series(0.0, index=idx)
    reg_dec = [(d, REGIME_EXPOSURE.get(rg, 0.5), rg) for d, v, rg in decisions]
    reg_exp = pd.Series(index=idx, dtype=float)
    dd_sorted = [(pd.Timestamp(d), e) for d, e, _ in reg_dec]
    for t in idx:
        prior = [e for dt, e in dd_sorted if dt <= t]
        reg_exp[t] = prior[-1] if prior else 0.5
    out["regime"] = sim_daily_returns(reg_exp, close)
    out["_avg_exp_committee"] = float(exposure_path(decisions, idx, mapfn, init).mean())
    return out


def main():
    p = argparse.ArgumentParser(description="收益率回测评估（读已落盘 verdict）")
    p.add_argument("--verdicts", action="append", required=True,
                   help="label=path，可多次（如 preset=... naked=...）")
    p.add_argument("--mapping", choices=list(MAPPINGS), default="relative")
    p.add_argument("--init-exposure", type=float, default=0.5)
    args = p.parse_args()
    mapfn = MAPPINGS[args.mapping]
    init = args.init_exposure

    labeled = []
    for spec in args.verdicts:
        label, path = spec.split("=", 1)
        labeled.append((label, load_verdicts(path)))

    assets = sorted({a for _, vd in labeled for a in vd})
    print(f"映射={args.mapping}  init_exposure={init}  资产={assets}\n")

    # 收集每个 (label/baseline, asset) 的日收益，算组合（等权）
    rows = []  # (policy_label, asset, metrics, avg_exp)
    combined: Dict[str, List[pd.Series]] = {}

    baseline_done = False
    for label, vd in labeled:
        for asset in assets:
            res = run_asset(asset, vd.get(asset, []), mapfn, init)
            if res is None:
                continue
            rows.append((f"{label}", asset, metrics(res["committee"]), res["_avg_exp_committee"]))
            combined.setdefault(label, []).append(res["committee"])
            if not baseline_done:
                for bl in ("buy_hold", "regime", "cash"):
                    rows.append((bl, asset, metrics(res[bl]), None))
                    combined.setdefault(bl, []).append(res[bl])
        baseline_done = True  # baseline 与 label 无关，只收一次（用第一个 label 的价格/regime）

    def combine(series_list):
        df = pd.concat(series_list, axis=1).fillna(0.0)
        return df.mean(axis=1)  # 等权组合日收益

    print(f"{'policy':<22}{'asset':<10}{'CR':>9}{'AR':>9}{'Sharpe':>9}{'MaxDD':>9}{'avgExp':>8}")
    print("-" * 76)
    for pol, asset, m, ae in rows:
        ae_s = f"{ae:.2f}" if ae is not None else "-"
        print(f"{pol:<22}{asset:<10}{m['CR']*100:>8.1f}%{m['AR']*100:>8.1f}%"
              f"{m['Sharpe']:>9.2f}{m['MaxDD']*100:>8.1f}%{ae_s:>8}")
    print("-" * 76)
    print(f"{'[COMBINED 等权]':<32}{'CR':>9}{'AR':>9}{'Sharpe':>9}{'MaxDD':>9}")
    for pol, series_list in combined.items():
        m = metrics(combine(series_list))
        print(f"{pol:<32}{m['CR']*100:>8.1f}%{m['AR']*100:>8.1f}%{m['Sharpe']:>9.2f}{m['MaxDD']*100:>8.1f}%")


if __name__ == "__main__":
    main()
