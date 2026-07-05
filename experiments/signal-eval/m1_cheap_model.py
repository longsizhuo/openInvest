"""M1 — 便宜模型基线(优化计划 v5)。

Q1 测单变量横截面 IC 全不显著。M1 问:committee 特征的【多变量组合】(GBM,walk-forward)
能不能拿到 OOS 横截面选股信号?这是判"无选股信号"前的最后一检(组合是否优于单条),
也是 M3 委员会增量的对照面("便宜模型能到多少")。

walk-forward 防前视:date t 的训练集只含 forward【已完全实现】的历史行(d + FWD_DAYS ≤ t),
预测当日 t 的横截面,算 OOS 预测 vs 实现的 rank-IC;NW-t(auto 带宽)on OOS IC 序列。

跑法:uv run --with scipy --with statsmodels --with scikit-learn python m1_cheap_model.py
(复用 Q1 的价格缓存 out/q1_prices.pkl;panel 自带缓存。)
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402
from eval_config import OUT_DIR  # noqa: E402
from q1_cross_sectional import FEATURES, FWD_DAYS, PRICES_CACHE, REBALANCE_FREQ, TEST_START  # noqa: E402

PANEL_CACHE = os.path.join(OUT_DIR, "q1_panel.pkl")  # 自写本地缓存(非外部),pickle 安全


def build_panel() -> pd.DataFrame:
    if os.path.exists(PANEL_CACHE):
        return pd.read_pickle(PANEL_CACHE)
    from openinvest.utils.market_metrics import compute_metrics
    blob = pd.read_pickle(PRICES_CACHE)
    frames = blob["frames"]
    dates = pd.date_range(TEST_START, pd.Timestamp.today(), freq=REBALANCE_FREQ)
    recs = []
    for t in dates:
        for s, df in frames.items():
            hist = df.loc[:t]
            if len(hist) < 260:
                continue
            future = df.loc[t + pd.Timedelta(days=FWD_DAYS):]
            if future.empty:
                continue
            p_now = hist["Close"].iloc[-1]
            if not (p_now > 0):
                continue
            m = compute_metrics(hist.tail(520))
            row = {f: m.get(f) for f in FEATURES}
            if any(v is None or not np.isfinite(v) for v in row.values()):
                continue
            row.update({"date": t, "name": s, "fwd": future["Close"].iloc[0] / p_now - 1.0})
            recs.append(row)
    panel = pd.DataFrame(recs)
    os.makedirs(OUT_DIR, exist_ok=True)
    panel.to_pickle(PANEL_CACHE)
    return panel


def walk_forward(panel: pd.DataFrame) -> dict:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from scipy.stats import norm

    dates = sorted(panel["date"].unique())
    oos_ic = []
    min_train = 5000  # 训练行下限,前期跳过
    for t in dates:
        cutoff = pd.Timestamp(t) - pd.Timedelta(days=FWD_DAYS)
        train = panel[panel["date"] <= cutoff]            # forward 已实现,无前视
        test = panel[panel["date"] == t]
        if len(train) < min_train or len(test) < 30:
            continue
        model = HistGradientBoostingRegressor(max_depth=3, max_iter=150,
                                              learning_rate=0.05, min_samples_leaf=50)
        model.fit(train[FEATURES].values, train["fwd"].values)
        pred = model.predict(test[FEATURES].values)
        oos_ic.append(mstat.rank_ic(list(pred), list(test["fwd"].values)))

    ics = [x for x in oos_ic if np.isfinite(x)]
    t_nw = mstat.nw_tstat(ics) if len(ics) >= 10 else float("nan")
    p_nw = float(2 * (1 - norm.cdf(abs(t_nw)))) if np.isfinite(t_nw) else float("nan")
    out = {
        "test": "M1_cheap_model_GBM_walkforward",
        "universe": "current S&P500 (survivorship-biased)",
        "n_oos_dates": len(ics),
        "mean_oos_ic": float(np.mean(ics)) if ics else float("nan"),
        "icir": mstat.icir(ics),
        "nw_t": t_nw, "p_nw": p_nw,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "m1_cheap_model.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"gate": "M1_GBM", "mean_oos_ic": out["mean_oos_ic"],
                            "nw_t": out["nw_t"], "p_nw": out["p_nw"]}, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    panel = build_panel()
    print(f"panel: {len(panel)} 行, {panel['date'].nunique()} 日, {panel['name'].nunique()} 名")
    res = walk_forward(panel)
    print(f"\n=== M1 GBM walk-forward(OOS 横截面 rank-IC)===")
    print(f"OOS dates {res['n_oos_dates']} | mean OOS IC {res['mean_oos_ic']:+.4f} | "
          f"ICIR {res['icir']:+.3f} | NW_t {res['nw_t']:+.2f} | p={res['p_nw']:.3f}")
    print("对照 Q1 最佳单变量 IC ≈ +0.067(atr/vol),NW-t≈1.8(不显著)。")
