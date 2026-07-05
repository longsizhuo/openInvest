"""signals_per_asset — 每资产、多信号族的"有没有能交易的信号"判决(不止黄金、不止趋势)。

承接 trend_dca(趋势已证三资产全失败)。用户要"系统不能只看黄金"+三资产逐个做信号研究。
这里对每个资产(GC=F/510300.SS/NDQ.AX)扫多个信号族:
- trend       MA 趋势 long/flat(迟滞)——已知失败,留作对照
- meanrev     均值回归:z=(close−MA)/std,超卖做多、超买空仓(A股最可能响应,因高波动/散户回归)
- voltarget   波动率目标:敞口 ∝ target_vol/realized_vol(风控族,记忆里 OOS 多失败,验一下)
- breakout    Donchian 突破(另一个趋势变体)

判决口径与 trend_dca 同:每个信号做成仓位序列,对照**匹配平均敞口被动**(隔离 beta)、
两腿扣成本、**DSR 对该资产上所有试过的变体数 deflate**(诚实惩罚"换着花样找信号")。
任一变体 DSR>0.95 才算这个资产上找到可交易信号。预期:多半 null(单资产信号普遍弱)。

跑:uv run --with scipy --with statsmodels python experiments/signal-eval/signals_per_asset.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402
from eval_config import OUT_DIR  # noqa: E402

TRADING_DAYS = 252

# 每资产单边成本:黄金 repo 实测 0.38%/侧;A股 ETF / 纳指 ETF 流动性好,取 0.10%/侧(保守略高)。
COST = {"GC=F": 0.0038, "510300.SS": 0.0010, "NDQ.AX": 0.0010}
GOLD_START = "2000-01-01"


# ---- 信号族:close → 仓位序列(0..1,0 前视:只用 ≤t 数据,迟滞有状态用 O(n) 循环)----

def sig_trend(close: pd.Series, window: int, buf: float) -> pd.Series:
    ma = close.rolling(window, min_periods=window).mean()
    cv, mv = close.values, ma.values
    pos = np.zeros(len(close)); state = 0
    for i in range(len(close)):
        m = mv[i]
        if not np.isfinite(m):
            pos[i] = np.nan; continue   # 暖机 → NaN(evaluate dropna 丢弃,不当作"flat"拉低 avg_exp)
        if cv[i] > m * (1 + buf): state = 1
        elif cv[i] < m * (1 - buf): state = 0
        pos[i] = state
    return pd.Series(pos, index=close.index)


def sig_meanrev(close: pd.Series, window: int, z_enter: float) -> pd.Series:
    """均值回归 long/flat:z<−z_enter(超卖)做多,z>+z_enter(超买)空仓,中间保持。"""
    ma = close.rolling(window, min_periods=window).mean()
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    z = (close - ma) / sd
    zv = z.values
    pos = np.zeros(len(close)); state = 0
    for i in range(len(close)):
        v = zv[i]
        if not np.isfinite(v):
            pos[i] = np.nan; continue   # 暖机 / std=0 → NaN(无信号,不算 flat)
        if v < -z_enter: state = 1
        elif v > z_enter: state = 0
        pos[i] = state
    return pd.Series(pos, index=close.index)


def sig_voltarget(close: pd.Series, window: int, target_ann: float) -> pd.Series:
    """波动率目标:敞口 = clip(target/realized_vol, 0, 1),无杠杆。realized 用 ≤t 收益。"""
    ret = close.pct_change()
    rv = ret.rolling(window, min_periods=window).std(ddof=0) * np.sqrt(TRADING_DAYS)
    pos = (target_ann / rv).clip(lower=0.0, upper=1.0)
    return pos.where(rv > 1e-12)   # 暖机(rv NaN)/ 零波动(div0) → NaN;不留 inf/"零波动满仓"假象


def sig_breakout(close: pd.Series, window: int) -> pd.Series:
    """Donchian:close 创 window 日新高→做多,跌破 window 日新低→空仓。用 shift(1) 排除当日。"""
    hi = close.rolling(window, min_periods=window).max().shift(1)
    lo = close.rolling(window, min_periods=window).min().shift(1)
    cv, hv, lv = close.values, hi.values, lo.values
    pos = np.zeros(len(close)); state = 0
    for i in range(len(close)):
        if not (np.isfinite(hv[i]) and np.isfinite(lv[i])):
            pos[i] = np.nan; continue   # 暖机 → NaN
        if cv[i] > hv[i]: state = 1
        elif cv[i] < lv[i]: state = 0
        pos[i] = state
    return pd.Series(pos, index=close.index)


# 变体网格:(family, fn, kwargs, label)
def _variants() -> List[dict]:
    out = []
    for w in (100, 200, 300):
        for b in (0.0, 0.01):
            out.append({"fam": "trend", "fn": sig_trend, "kw": {"window": w, "buf": b}, "lab": f"trend_ma{w}_b{b}"})
    for w in (20, 50, 100):
        for z in (1.0, 1.5, 2.0):
            out.append({"fam": "meanrev", "fn": sig_meanrev, "kw": {"window": w, "z_enter": z}, "lab": f"meanrev_w{w}_z{z}"})
    for w in (20, 60):
        for t in (0.10, 0.15, 0.20):
            out.append({"fam": "voltarget", "fn": sig_voltarget, "kw": {"window": w, "target_ann": t}, "lab": f"voltgt_w{w}_t{t}"})
    for w in (50, 100, 200):
        out.append({"fam": "breakout", "fn": sig_breakout, "kw": {"window": w}, "lab": f"breakout_w{w}"})
    return out


def _maxdd(ret: pd.Series) -> float:
    eq = (1 + ret).cumprod()
    return float((eq / eq.cummax() - 1.0).min())


def evaluate(close: pd.Series, pos: pd.Series, cost: float) -> Optional[Dict]:
    """信号仓位 vs 匹配平均敞口被动,扣成本。
    暖机由信号 NaN + dropna 处理(不再硬截"首个非零仓位"——那会把信号合法的早期 flat 也砍掉、
    系统性抬高 avg_exp → 偏向 null,review 修)。合法的 flat(pos=0)保留。
    avg_exp 用全样本均值是**有意的**:active=strat−avg_exp·aret 的均值 ≈ Cov(pos,ret)−cost,
    即纯择时技能(avg_exp 只是 demean 常数,不是可交易基线;可交易对照见 trend_dca 的满仓买持)。"""
    df = pd.DataFrame({"close": close, "pos": pos}).dropna()
    if len(df) < 120:
        return None
    df["aret"] = df["close"].pct_change()
    df["held"] = df["pos"].shift(1)
    df["turn"] = (df["pos"] - df["pos"].shift(1)).abs()
    df = df.dropna()
    if len(df) < 60:
        return None
    avg_exp = float(df["pos"].mean())
    strat = df["held"] * df["aret"] - cost * df["turn"]
    passive = avg_exp * df["aret"]
    active = strat - passive
    sd = float(active.std(ddof=1))
    sr = float(active.mean() / sd) if sd > 0 else float("nan")
    skew = float(active.skew())
    kurt_full = float(active.kurt() + 3.0)
    if not (np.isfinite(skew) and np.isfinite(kurt_full)):
        skew, kurt_full = 0.0, 3.0   # 退化样本 → 正态默认(不让 NaN 传进 DSR)
    return {
        "T": len(active), "avg_exposure": avg_exp,
        "trades": int((df["turn"] > 1e-9).sum()), "turnover": float(df["turn"].sum()),
        "sr_active_pp": sr, "sr_active_ann": sr * np.sqrt(TRADING_DAYS) if np.isfinite(sr) else float("nan"),
        "skew": skew, "kurt_full": kurt_full,
        "wealth_strat": float((1 + strat).prod()), "wealth_passive": float((1 + passive).prod()),
        "maxdd_strat": _maxdd(strat), "maxdd_passive": _maxdd(passive),
    }


def run_asset(sym: str, close: pd.Series) -> Dict:
    cost = COST.get(sym, 0.0010)
    rows: List[Dict] = []
    for v in _variants():
        r = evaluate(close, v["fn"](close, **v["kw"]), cost)
        if r is None:
            continue
        r.update({"family": v["fam"], "label": v["lab"]})
        rows.append(r)
    srs = [r["sr_active_pp"] for r in rows if np.isfinite(r["sr_active_pp"])]
    n_trials = len(srs)                                    # 有限 SR 的变体数(与 sr_std 同口径,喂 DSR deflation)
    sr_std = float(np.std(srs, ddof=1)) if len(srs) > 1 else 0.0
    for r in rows:
        sr, T = r["sr_active_pp"], r["T"]
        r["dsr"] = (mstat.deflated_sharpe(sr, T, n_trials, sr_std, skew=r["skew"], kurt=r["kurt_full"])
                    if np.isfinite(sr) else float("nan"))
    rows.sort(key=lambda x: (-(x["dsr"] if np.isfinite(x["dsr"]) else -1)))
    best = rows[0] if rows else None
    any_pass = any(np.isfinite(r["dsr"]) and r["dsr"] > 0.95 for r in rows)
    return {"asset": sym, "cost_per_side": cost, "n_trials": n_trials, "sr_std_trials": sr_std,
            "any_dsr_pass_0.95": any_pass, "best": best, "rows": rows}


def main() -> Dict:
    from openinvest.db.market_store import MarketStore
    ms = MarketStore()
    out = {"test": "signals_per_asset",
           "design": "per-asset × {trend,meanrev,voltarget,breakout} vs matched-exposure passive, net cost, DSR deflated over all variants tried",
           "results": []}
    for sym, start in [("GC=F", GOLD_START), ("510300.SS", None), ("NDQ.AX", None)]:
        df = ms.get_history_df(sym, days=100000)
        if df is None or df.empty:
            continue
        c = df["Close"].dropna(); c.index = pd.to_datetime(c.index)
        if start:
            c = c[c.index >= start]
        out["results"].append(run_asset(sym, c))

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "signals_per_asset.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        for r in out["results"]:
            f.write(json.dumps({"gate": "signals_per_asset", "asset": r["asset"],
                                "n_trials": r["n_trials"], "any_dsr_pass_0.95": r["any_dsr_pass_0.95"],
                                "best_family": r["best"]["family"] if r["best"] else None,
                                "best_dsr": r["best"]["dsr"] if r["best"] else None}, ensure_ascii=False) + "\n")
    return out


def _print(out: Dict) -> None:
    print("\n=== 每资产多信号族判决(匹配敞口、扣成本、DSR deflated over all variants)===")
    for r in out["results"]:
        print(f"\n[{r['asset']}]  n_trials={r['n_trials']}  任一过 DSR>0.95: "
              f"{'✅有' if r['any_dsr_pass_0.95'] else '❌无'}")
        # 每族最优一行
        by_fam: Dict[str, Dict] = {}
        for row in r["rows"]:
            f = row["family"]
            if f not in by_fam or (row["dsr"] or -1) > (by_fam[f]["dsr"] or -1):
                by_fam[f] = row
        print(f"  {'family':10} {'best_var':18} {'SR_ann':>7} {'DSR':>6} {'W_s/W_p':>13} {'DD_s/DD_p':>12}")
        for f in ("trend", "meanrev", "voltarget", "breakout"):
            b = by_fam.get(f)
            if not b:
                continue
            print(f"  {f:10} {b['label']:18} {b['sr_active_ann']:>+7.2f} {b['dsr']:>6.3f} "
                  f"{b['wealth_strat']:>5.2f}/{b['wealth_passive']:<5.2f} "
                  f"{b['maxdd_strat']*100:>4.0f}%/{b['maxdd_passive']*100:<4.0f}%")


if __name__ == "__main__":
    _print(main())
