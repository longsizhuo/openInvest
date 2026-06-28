"""Q1 — 横截面选股脊柱(优化计划 v5 研究域,与 Q2 篮子时序正交)。

问题:committee 读的【确定性特征】在几千股票横截面上,有没有预测 forward return 的信号?
无 LLM、无 cutoff 问题(纯特征→收益的统计关系,不涉及模型 memorization)。

宇宙:当前 S&P500(~503)。⚠ survivorship-biased(Stooq 含退市被 block 返回 noindex HTML;
CRSP 付费)。**不声明无偏**——量残余偏差(当前成分=幸存者,覆盖起止)并排报。
解读规则:survivorship 只会【抬高】IC → 不显著 = 稳健负结论;显著 = 需更干净宇宙确认。

显著性:每个 rebalance 日一个横截面 rank-IC(用 compute_metrics 的真特征);IC 序列因 forward(30d)>
rebalance step(周)而重叠/自相关 → 用 Newey-West t(auto 带宽)on IC 序列;Holm 覆盖特征族。

跑法:
  拉宇宙(慢,缓存):uv run --with scipy --with statsmodels python q1_cross_sectional.py pull
  算:                uv run --with scipy --with statsmodels python q1_cross_sectional.py compute
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402
from eval_config import OUT_DIR  # noqa: E402

# pickle 仅用于【本脚本自己 pull() 写、compute() 读】的本地价格缓存(dict[str,DataFrame]),
# 非外部/不可信来源,无 RCE 面;DataFrame 字典用 pickle 比 parquet 省一个 pyarrow 依赖。
PRICES_CACHE = os.path.join(OUT_DIR, "q1_prices.pkl")
PULL_START = "2021-01-01"
TEST_START = "2024-01-01"      # 横截面 IC 测试窗起点(前面留 ~2y 给 compute_metrics lookback)
REBALANCE_FREQ = "W-FRI"       # 周频 rebalance
FWD_DAYS = 30                  # forward return 日历日
# 测 committee 真读的这些确定性特征(compute_metrics 输出)
FEATURES = ["return_30d", "rsi14", "price_quantile_2y", "volatility_annualized",
            "max_drawdown", "atr_pct"]


def get_universe() -> list:
    """当前 S&P500 成分(Wikipedia)。survivorship-biased,measured 后并排报。"""
    req = urllib.request.Request("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                                 headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode()
    syms = re.findall(r'<td><a [^>]*rel="nofollow"[^>]*>([A-Z\.]{1,6})</a>', html)
    return sorted(set(s.replace(".", "-") for s in syms))  # BRK.B → BRK-B (yfinance 口径)


def pull() -> None:
    import yfinance as yf
    syms = get_universe()
    print(f"宇宙 {len(syms)} 个(当前 S&P500,survivorship-biased)")
    frames = {}
    CHUNK = 80
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        try:
            data = yf.download(chunk, start=PULL_START, auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
            for s in chunk:
                try:
                    sub = data[s] if len(chunk) > 1 else data
                    df = sub[["Close", "High", "Low", "Volume"]].dropna(how="all")
                    if len(df) > 300:
                        frames[s] = df
                except Exception:
                    pass
            print(f"  chunk {i//CHUNK+1}: 累计 {len(frames)} 个有数据")
        except Exception as e:
            print(f"  chunk {i//CHUNK+1} 失败: {type(e).__name__} {str(e)[:60]}")
    os.makedirs(OUT_DIR, exist_ok=True)
    pd.to_pickle({"frames": frames, "universe_n": len(syms),
                  "pulled_n": len(frames), "pull_start": PULL_START}, PRICES_CACHE)
    print(f"→ 缓存 {len(frames)}/{len(syms)} 个 ticker 到 {PRICES_CACHE}")


def compute() -> dict:
    from utils.market_metrics import compute_metrics
    blob = pd.read_pickle(PRICES_CACHE)
    frames = blob["frames"]
    dates = pd.date_range(TEST_START, pd.Timestamp.today(), freq=REBALANCE_FREQ)

    # 每个 rebalance 日:横截面收集每只股票的特征 + forward return,算 rank-IC
    per_date_ic = {f: [] for f in FEATURES}
    n_names_per_date = []
    for t in dates:
        feat_vals = {f: [] for f in FEATURES}
        fwds = []
        for s, df in frames.items():
            hist = df.loc[:t]
            if len(hist) < 260:
                continue
            # forward return t → t+FWD_DAYS(日历日,第一个 >= 的收盘)
            future = df.loc[t + pd.Timedelta(days=FWD_DAYS):]
            if future.empty:
                continue
            p_now = hist["Close"].iloc[-1]
            p_fut = future["Close"].iloc[0]
            if not (p_now > 0):
                continue
            m = compute_metrics(hist.tail(520))
            row = [m.get(f) for f in FEATURES]
            if any(v is None or not np.isfinite(v) for v in row):
                continue
            for f, v in zip(FEATURES, row):
                feat_vals[f].append(v)
            fwds.append(p_fut / p_now - 1.0)
        if len(fwds) < 30:   # 横截面太薄,跳过该日
            continue
        n_names_per_date.append(len(fwds))
        for f in FEATURES:
            per_date_ic[f].append(mstat.rank_ic(feat_vals[f], fwds))

    # 每个特征:IC 序列 → mean IC / ICIR / NW-t(auto lag,处理重叠自相关)
    rows = []
    pvals = []
    from scipy.stats import norm
    for f in FEATURES:
        ics = [x for x in per_date_ic[f] if np.isfinite(x)]
        if len(ics) < 10:
            rows.append({"feature": f, "n_dates": len(ics), "mean_ic": float("nan"),
                         "icir": float("nan"), "nw_t": float("nan"), "p_nw": float("nan")})
            continue
        t_nw = mstat.nw_tstat(ics)
        p_nw = float(2 * (1 - norm.cdf(abs(t_nw))))   # 双侧
        rows.append({"feature": f, "n_dates": len(ics), "mean_ic": float(np.mean(ics)),
                     "icir": mstat.icir(ics), "nw_t": t_nw, "p_nw": p_nw})
        pvals.append((f, p_nw))

    if pvals:
        adj = mstat.holm([p for _, p in pvals])
        pmap = {f: a for (f, _), a in zip(pvals, adj)}
        for r in rows:
            r["p_holm"] = pmap.get(r["feature"], float("nan"))

    out = {
        "test": "Q1_cross_sectional_rank_IC",
        "universe": "current S&P500 (survivorship-biased)",
        "survivorship_caveat": {"universe_n": blob["universe_n"], "pulled_n": blob["pulled_n"],
                                "note": "current constituents = survivors; IC 偏高。不显著=稳健负;显著需更干净宇宙确认。"},
        "rebalance": REBALANCE_FREQ, "fwd_days": FWD_DAYS,
        "median_names_per_date": float(np.median(n_names_per_date)) if n_names_per_date else 0,
        "n_rebalance_dates": len(n_names_per_date),
        "features": rows,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "q1_cross_sectional.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    n_sig = sum(1 for r in rows if np.isfinite(r.get("p_holm", float("nan"))) and r["p_holm"] < 0.05)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"gate": "Q1", "universe": "SP500_survivorship",
                            "features_tested": len(rows), "holm_significant_<0.05": n_sig,
                            "median_breadth": out["median_names_per_date"]}, ensure_ascii=False) + "\n")
    print(f"\n=== Q1 横截面 rank-IC(S&P500 survivorship,{out['n_rebalance_dates']} 个 rebalance 日,"
          f"横截面中位 {out['median_names_per_date']:.0f} 只)===")
    print(f"{'feature':22} {'mean_IC':9} {'ICIR':7} {'NW_t':7} {'p_holm':8}")
    for r in rows:
        ph = f"{r['p_holm']:.3f}" if np.isfinite(r.get('p_holm', float('nan'))) else "n/a"
        print(f"{r['feature']:22} {r['mean_ic']:+.4f}  {r['icir']:+.3f}  {r['nw_t']:+.2f}   {ph}")
    return out


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "compute"
    if mode == "pull":
        pull()
    else:
        compute()
