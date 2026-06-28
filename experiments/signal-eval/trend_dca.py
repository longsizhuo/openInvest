"""trend_dca — 趋势择时 overlay 的判决闸:MA 趋势 long/flat 择时,扣成本后能否赢
"匹配平均敞口的被动持有"?(信号驱动定投技术方案 §5 的上线闸)

为什么需要这个测试(它比 Q2 强在哪):
- Q2 发现"黄金价在 MA200 上方的 90d forward 中位 +3.43% vs 下方 −0.64%"(p=0.016)。
- 但 Q2 忽略了两件让"赢"变假的事:① **交易成本**(每次进出黄金扣 0.38%);
  ② **beta**——"在 MA200 上方"本身就是更牛的日子,光是 in-market 时间更长就会赢,
  这不是择时能力,是暴露差异。
- 本测试同时控制两者:把 MA 趋势做成 **long/flat 择时策略**,对照一个**平均敞口完全相同**
  的被动常仓(constant exposure),两者都扣真实成本。active = 择时 − 匹配被动,
  隔离掉 beta,只留"何时在场"的纯择时贡献。再用 DSR 对 MA 窗口网格的搜索次数做 deflation。

判决:active 的 DSR > 0.95(扣成本、对搜索 deflated)→ 真有择时 edge,值得接进生产。
否则 → 是 beta + 运气、被成本吃掉 → 保持纯被动/纯定投(这是诚实的负结论,不是失败)。

跑法:uv run --with scipy --with statsmodels python experiments/signal-eval/trend_dca.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402
from eval_config import BASKET_ASSETS, OUT_DIR  # noqa: E402

TRADING_DAYS = 252

# MA 窗口 + 缓冲带网格。**MA200 / buf=0 是预注册的 primary**(教科书定义、Q2 显著的那个);
# 其余是刻意的搜索 → 它决定 DSR 的 n_trials(我们到底试了几条腿)。
WINDOWS = [100, 150, 200, 250, 300]
BUFFERS = [0.0, 0.01]            # 迟滞缓冲(防 MA 附近抖动 whipsaw)
PRIMARY: Tuple[int, float] = (200, 0.0)

# 黄金单边成本:repo 对 GC=F 卖出扣 0.38%(paper_trade_simulator.py:244)。
# 按 |Δ敞口| 每侧收;primary 取保守的满 0.38%/侧,另跑敏感性。
PRIMARY_COST = 0.0038
COST_GRID = [0.0010, 0.0019, 0.0038]

# 黄金 GC=F yfinance 2000 前是回填现货,数据存疑且 1970s 金本位崩溃的巨型趋势不可复现
# → primary 用 2000 后(真期货时代);全历史另列作 sensitivity(标注 caveat)。
GOLD_PRIMARY_START = "2000-01-01"


def trend_position(close: pd.Series, window: int, entry_buf: float, exit_buf: float) -> pd.Series:
    """带迟滞的 long/flat 趋势仓位,0 前视。
    pos_t 在 close_t 用 MA_t(只含 ≤t 数据)决定 → 持有 t→t+1;暖机期(MA 未成熟)→ 0。
    ponytail: 迟滞是有状态的 → O(n) 顺序循环;n~14k,可忽略。"""
    ma = close.rolling(window, min_periods=window).mean()
    cv = close.values
    mv = ma.values
    pos = np.zeros(len(close))
    state = 0
    for i in range(len(close)):
        m = mv[i]
        if not np.isfinite(m):
            pos[i] = 0
            continue
        if cv[i] > m * (1 + entry_buf):
            state = 1
        elif cv[i] < m * (1 - exit_buf):
            state = 0
        pos[i] = state
    return pd.Series(pos, index=close.index)


def evaluate_variant(close: pd.Series, window: int, entry_buf: float, exit_buf: float,
                     cost: float) -> Optional[Dict]:
    """单条腿:择时 vs 匹配敞口被动,扣成本。返回 active 收益序列 + 概要。
    暖机行(MA 未成熟)整段剔除,避免 avg_exposure 被前导 0 拖低。"""
    pos = trend_position(close, window, entry_buf, exit_buf)
    ma_ok = close.rolling(window, min_periods=window).mean().notna()
    df = pd.DataFrame({"close": close[ma_ok], "pos": pos[ma_ok]})
    if len(df) < 60:
        return None
    df["aret"] = df["close"].pct_change()
    df["held"] = df["pos"].shift(1)               # 进入第 t 日时持有的仓(t-1 决定)→ 无前视
    df["turn"] = (df["pos"] - df["pos"].shift(1)).abs()
    df = df.dropna()
    # avg_exp 用全样本均值是**有意的**:active=strat−avg_exp·aret 的均值 ≈ Cov(pos,ret)−cost,
    # 即纯择时技能(avg_exp 只是 demean 常数,不是可交易基线)。可交易的零前视对照 = 满仓买持(下方
    # wealth_buyhold/sharpe_buyhold)——策略连它也输,故结论不依赖匹配被动这个基线选择。
    avg_exp = float(df["pos"].mean())
    strat = df["held"] * df["aret"] - cost * df["turn"]   # 成本在换仓当日(close t)结算
    passive = avg_exp * df["aret"]                         # 平均敞口相同的常仓(被动,demean 对照)
    buyhold = df["aret"]                                   # 满仓买入持有(exposure=1,零前视、零择时、零成本)
    active = strat - passive
    T = len(active)
    sd = float(active.std(ddof=1))
    sr = float(active.mean() / sd) if sd > 0 else float("nan")   # active 单周期 SR(非年化,喂 DSR)
    skew = float(active.skew())
    kurt_full = float(active.kurt() + 3.0)                 # mstat 要全峰度(正态=3),pandas.kurt 是 excess
    if not (np.isfinite(skew) and np.isfinite(kurt_full)):
        skew, kurt_full = 0.0, 3.0
    return {
        "window": window, "entry_buf": entry_buf, "exit_buf": exit_buf, "cost": cost,
        "T": T, "avg_exposure": avg_exp,
        "trades": int((df["turn"] > 1e-9).sum()), "turnover": float(df["turn"].sum()),
        "sr_active_perperiod": sr,
        "sr_active_ann": sr * np.sqrt(TRADING_DAYS) if np.isfinite(sr) else float("nan"),
        "mean_active_ann": float(active.mean() * TRADING_DAYS),
        "skew": skew, "kurt_full": kurt_full,
        # 绝对(各自收益序列自身)年化 Sharpe——文档"择时 0.36 vs 被动/买持 0.68"出处
        "sharpe_strat_ann": _abs_sharpe(strat), "sharpe_passive_ann": _abs_sharpe(passive),
        "sharpe_buyhold_ann": _abs_sharpe(buyhold),
        "wealth_strat": float((1 + strat).prod()),
        "wealth_passive": float((1 + passive).prod()),
        "wealth_buyhold": float((1 + buyhold).prod()),   # 文档"满仓买持 15.10"出处
        "maxdd_strat": _maxdd(strat), "maxdd_passive": _maxdd(passive), "maxdd_buyhold": _maxdd(buyhold),
        "_active": active,   # 内部用,出 JSON 前删
    }


def _maxdd(ret: pd.Series) -> float:
    """收益序列的最大回撤(负数,如 -0.42 = -42%)。"""
    eq = (1 + ret).cumprod()
    return float((eq / eq.cummax() - 1.0).min())


def _abs_sharpe(ret: pd.Series) -> float:
    """收益序列自身的年化 Sharpe(均值/标准差×√252)。"""
    sd = float(ret.std(ddof=1))
    return float(ret.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else float("nan")


def dca_tilt_multiples(close: pd.Series, window: int = 200) -> Dict[str, float]:
    """DCA 投入择时:同总额按趋势 tilt 每日投入 vs 平均投入。k>0=MA 上方多投(顺势),
    k<0=下方多投(抄底)。core 永不卖,只调投入额。返回各 k 的 终值/总投入 倍数。0 前视。"""
    ma = close.rolling(window, min_periods=window).mean()
    df = pd.DataFrame({"c": close, "ma": ma}).dropna()
    sig = (df["c"] > df["ma"]).astype(float)
    sbar = float(sig.mean())
    out: Dict[str, float] = {}
    for k in (-1.0, -0.5, 0.0, 0.5, 1.0):
        a = (1.0 + k * (sig - sbar)).clip(lower=0.0)   # 投入权重,Σa=N(总额不变)
        units = float((a / df["c"]).sum())
        out[f"k={k:+.1f}"] = units * float(df["c"].iloc[-1]) / float(a.sum())
    return out


def run_asset(sym: str, close: pd.Series, label: str, cost: float = PRIMARY_COST) -> Dict:
    """对一个资产跑整个 MA 网格,DSR 用网格规模做 deflation,标出 primary(MA200/buf0)。"""
    variants: List[Dict] = []
    for w in WINDOWS:
        for b in BUFFERS:
            v = evaluate_variant(close, w, b, b, cost)
            if v is not None:
                variants.append(v)
    if not variants:
        return {"asset": sym, "label": label, "error": "no_variants"}

    srs = [v["sr_active_perperiod"] for v in variants if np.isfinite(v["sr_active_perperiod"])]
    n_trials = len(srs)                            # 有限 SR 的腿数(与 sr_std_trials 同口径)
    sr_std_trials = float(np.std(srs, ddof=1)) if len(srs) > 1 else 0.0

    for v in variants:
        sr, T = v["sr_active_perperiod"], v["T"]
        if np.isfinite(sr):
            v["psr_vs0"] = mstat.psr(sr, T, skew=v["skew"], kurt=v["kurt_full"])
            v["dsr"] = mstat.deflated_sharpe(sr, T, n_trials, sr_std_trials,
                                             skew=v["skew"], kurt=v["kurt_full"])
            try:
                v["nw_t_active"] = mstat.nw_tstat(v["_active"].values)
            except Exception:  # noqa: BLE001  statsmodels 缺则跳过,不挡 DSR
                v["nw_t_active"] = float("nan")
        else:
            v["psr_vs0"] = v["dsr"] = v["nw_t_active"] = float("nan")
        del v["_active"]

    primary = next((v for v in variants
                    if v["window"] == PRIMARY[0] and v["entry_buf"] == PRIMARY[1]), variants[0])
    return {
        "asset": sym, "label": label, "cost_per_side": cost,
        "n_trials": n_trials, "sr_std_trials": sr_std_trials,
        "primary": primary, "grid": variants,
    }


def main() -> Dict:
    from db.market_store import MarketStore
    ms = MarketStore()
    out: Dict = {"test": "trend_dca_gate",
                 "design": "MA-trend long/flat vs matched-exposure passive, net cost, DSR deflated over MA grid",
                 "gate": "primary DSR > 0.95 → wire; else stay passive", "results": []}

    # 黄金:primary(2000 后)+ 全历史 sensitivity + 成本敏感性
    gdf = ms.get_history_df("GC=F", days=100000)
    if gdf is not None and not gdf.empty:
        gclose = gdf["Close"].dropna()
        gclose.index = pd.to_datetime(gclose.index)
        out["results"].append(run_asset("GC=F", gclose[gclose.index >= GOLD_PRIMARY_START],
                                        label="gold_post2000_primary"))
        out["results"].append(run_asset("GC=F", gclose, label="gold_full_history_sensitivity"))
        for c in COST_GRID:
            r = run_asset("GC=F", gclose[gclose.index >= GOLD_PRIMARY_START],
                          label=f"gold_post2000_cost{c}", cost=c)
            out["results"].append({"asset": "GC=F", "label": r["label"], "cost_per_side": c,
                                   "primary_dsr": r["primary"]["dsr"],
                                   "primary_sr_ann": r["primary"]["sr_active_ann"],
                                   "primary_wealth_strat_vs_passive":
                                       [r["primary"]["wealth_strat"], r["primary"]["wealth_passive"]]})

    # A股 / 纳指:同一套,功效弱(历史短)→ 看 DSR 是否仍过闸
    for sym in [s for s in BASKET_ASSETS if s != "GC=F"]:
        df = ms.get_history_df(sym, days=100000)
        if df is None or df.empty:
            continue
        close = df["Close"].dropna()
        close.index = pd.to_datetime(close.index)
        out["results"].append(run_asset(sym, close, label=f"{sym}_full"))

    # DCA 投入 tilt(同总额,趋势加权 vs 平均)——文档"抄底>平均>顺势"具体数字出处,可复现
    tilt: Dict = {}
    for sym, start in [("GC=F", GOLD_PRIMARY_START), ("510300.SS", None), ("NDQ.AX", None)]:
        df = ms.get_history_df(sym, days=100000)
        if df is None or df.empty:
            continue
        c = df["Close"].dropna(); c.index = pd.to_datetime(c.index)
        if start:
            c = c[c.index >= start]
        tilt[sym] = dca_tilt_multiples(c)
    out["dca_tilt"] = {"design": "same total invested, weight by MA200 trend; k>0=顺势(上方多投), k<0=抄底(下方多投)",
                       "terminal_multiple_by_asset_and_k": tilt}

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "trend_dca.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 决策审计行
    prim = next((r for r in out["results"] if r.get("label") == "gold_post2000_primary"), None)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "gate": "trend_dca", "asset": "GC=F", "window": "post2000",
            "primary_dsr": prim["primary"]["dsr"] if prim else None,
            "primary_passes_0.95": bool(prim and np.isfinite(prim["primary"]["dsr"])
                                        and prim["primary"]["dsr"] > 0.95),
        }, ensure_ascii=False) + "\n")
    return out


def _print(out: Dict) -> None:
    print("\n=== 趋势择时闸:MA-trend long/flat vs 匹配敞口被动(扣成本,DSR deflated)===")
    for r in out["results"]:
        if "primary" not in r:   # 成本敏感性精简行
            print(f"  [{r['label']:28}] DSR={r.get('primary_dsr', float('nan')):.3f}  "
                  f"SR_ann={r.get('primary_sr_ann', float('nan')):+.2f}  "
                  f"终值 strat/passive={r['primary_wealth_strat_vs_passive'][0]:.2f}/"
                  f"{r['primary_wealth_strat_vs_passive'][1]:.2f}")
            continue
        p = r["primary"]
        print(f"\n[{r['label']}]  n_trials={r['n_trials']}  primary=MA{p['window']}/buf{p['entry_buf']}")
        print(f"  T={p['T']}  avg_exp={p['avg_exposure']:.2f}  trades={p['trades']}  "
              f"换手={p['turnover']:.1f}")
        print(f"  SR_active(年化)={p['sr_active_ann']:+.2f}  mean_active(年化)={p['mean_active_ann']*100:+.1f}%  "
              f"NW_t={p['nw_t_active']:+.2f}")
        print(f"  PSR(vs0)={p['psr_vs0']:.3f}   ** DSR={p['dsr']:.3f} **  "
              f"{'✅过闸' if np.isfinite(p['dsr']) and p['dsr'] > 0.95 else '❌不过闸'}")
        print(f"  终值 strat={p['wealth_strat']:.2f} vs passive={p['wealth_passive']:.2f}   "
              f"maxDD strat={p['maxdd_strat']*100:.0f}% vs passive={p['maxdd_passive']*100:.0f}%")


if __name__ == "__main__":
    _print(main())
