"""Q2 — 篮子择时:未调参的 textbook regime 是否预测篮子资产的 forward return?

这是优化计划的"篮子真实域"测试(时序/共有成分,与 Q1 横截面正交)。要点:
- **未调参 regime**:用教科书定义(MA200 趋势 / 252日高点回撤压力),**不用 config 里被
  optuna/atr_defense 调过的阈值**——那些阈值是照着 NDQ/GC 历史崩盘调的,拿它分桶再测"crash→负收益"
  是循环论证(见 tunable.py:129-130)。textbook 阈值预注册、不碰被测数据。
- **0 前视**:regime 标签只用 ≤t 数据(rolling backward);forward return 用日历日对齐,
  t+h 落在数据末尾之外的行 → NaN(不成熟样本不计),口径同 core.regime_probability.forward_return。
- **重叠校正**:30d/90d forward 高度重叠 → 重叠样本的两样本 p 值虚高。显著性只在【非重叠】
  子样本(日期间隔 ≥ h 日历日)上算,effective_n = 非重叠样本数。
- **多重比较**:Holm 覆盖 Q2 这个族(资产 × 窗口 × 对比),分桶 p 不裸用。

跑法:`uv run --with scipy --with statsmodels python experiments/signal-eval/regime_forward_q2.py`
(需 INVEST_HOME / PYTHONPATH 指向 repo)。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402
from eval_config import BASKET_ASSETS, FORWARD_WINDOWS, OUT_DIR  # noqa: E402

_WINDOW_DAYS = {"30d": 30, "90d": 90}


def _forward_return(close: pd.Series, h_days: int) -> np.ndarray:
    """日历日 forward return:每个 t 取 t+h_days 当日或之后第一个收盘 / 当前收盘 − 1;
    t+h 超出数据末尾 → NaN(不成熟,不计)。0 前视。"""
    idx = close.index
    vals = close.values.astype(float)
    target = idx + pd.Timedelta(days=h_days)
    pos = idx.searchsorted(target, side="left")  # 第一个 >= t+h 的位置
    out = np.full(len(idx), np.nan)
    valid = pos < len(idx)
    out[valid] = vals[pos[valid]] / vals[np.flatnonzero(valid)] - 1.0
    return out


def _textbook_regime(close: pd.Series) -> Dict[str, pd.Series]:
    """未调参的教科书 regime 标签(只用 ≤t 数据):
    - trend:收盘 vs 200 日均线(above/below)
    - stress:相对 252 日滚动高点的回撤 ≤ −15% → stress,否则 calm
    返回 {'trend': Series[str], 'stress': Series[str]}(不足窗口的早期 → None)。"""
    ma200 = close.rolling(200, min_periods=200).mean()
    trend = pd.Series(np.where(close >= ma200, "above", "below"), index=close.index)
    trend[ma200.isna()] = None
    roll_max = close.rolling(252, min_periods=252).max()
    dd = close / roll_max - 1.0
    stress = pd.Series(np.where(dd <= -0.15, "stress", "calm"), index=close.index)
    stress[roll_max.isna()] = None
    return {"trend": trend, "stress": stress}


def _nonoverlap_idx(index: pd.DatetimeIndex, h_days: int) -> np.ndarray:
    """贪心取日期间隔 ≥ h_days 日历日的位置 → forward 窗口互不重叠(诚实 effective_n + p)。"""
    picked: List[int] = []
    last = None
    for i, ts in enumerate(index):
        if last is None or (ts - last).days >= h_days:
            picked.append(i)
            last = ts
    return np.asarray(picked, dtype=int)


# 每个对比:(regime 维度, A 标签, B 标签)。A 是"看多/常态",B 是"看空/压力"。
_CONTRASTS = [("trend", "above", "below"), ("stress", "calm", "stress")]


def run_asset(sym: str, df: pd.DataFrame) -> List[dict]:
    close = df["Close"].dropna()
    regimes = _textbook_regime(close)
    rows: List[dict] = []
    for win in FORWARD_WINDOWS:
        h = _WINDOW_DAYS[win]
        fwd = pd.Series(_forward_return(close, h), index=close.index)
        keep = _nonoverlap_idx(close.index, h)  # 非重叠子样本
        for dim, a_lab, b_lab in _CONTRASTS:
            lab = regimes[dim]
            # 非重叠样本上分桶(同时要 forward 成熟 + regime 有标签)
            sub_lab = lab.iloc[keep]
            sub_fwd = fwd.iloc[keep]
            a = sub_fwd[(sub_lab == a_lab) & sub_fwd.notna()].values
            b = sub_fwd[(sub_lab == b_lab) & sub_fwd.notna()].values
            res = mstat.two_sample_diff(list(a), list(b))
            # 描述性(重叠全样本中位数,更稳但不用于显著性)
            raw_a = fwd[(lab == a_lab) & fwd.notna()].values
            raw_b = fwd[(lab == b_lab) & fwd.notna()].values
            rows.append({
                "asset": sym, "window": win, "dim": dim,
                "bucket_a": a_lab, "bucket_b": b_lab,
                "eff_n_a": int(res["n_a"]), "eff_n_b": int(res["n_b"]),
                "raw_n_a": int(len(raw_a)), "raw_n_b": int(len(raw_b)),
                "median_a_noverlap": res["median_a"], "median_b_noverlap": res["median_b"],
                "median_a_raw": float(np.median(raw_a)) if len(raw_a) else float("nan"),
                "median_b_raw": float(np.median(raw_b)) if len(raw_b) else float("nan"),
                "effect_noverlap": res["effect"],
                "p_noverlap": res["p"],
            })
    return rows


def main() -> dict:
    from db.market_store import MarketStore
    ms = MarketStore()
    all_rows: List[dict] = []
    for sym in BASKET_ASSETS:
        df = ms.get_history_df(sym, days=100000)
        if df is None or df.empty:
            print(f"  ⏭ {sym}: 无数据")
            continue
        all_rows.extend(run_asset(sym, df))

    # Holm 覆盖整个 Q2 族(资产 × 窗口 × 对比);跳过 effective_n 不足而 p=nan 的
    fam = [r for r in all_rows if np.isfinite(r["p_noverlap"])]
    if fam:
        adj = mstat.holm([r["p_noverlap"] for r in fam])
        for r, pa in zip(fam, adj):
            r["p_holm"] = pa
    for r in all_rows:
        r.setdefault("p_holm", float("nan"))

    os.makedirs(OUT_DIR, exist_ok=True)
    out = {"test": "Q2_regime_forward", "regime": "textbook (un-tuned: MA200 trend / 252d-drawdown stress)",
           "significance": "non-overlapping subsample, Holm over family", "rows": all_rows}
    with open(os.path.join(OUT_DIR, "q2_regime_forward.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 决策审计行
    n_sig = sum(1 for r in all_rows if np.isfinite(r.get("p_holm", float("nan"))) and r["p_holm"] < 0.05)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"gate": "Q2", "regime": "textbook",
                            "contrasts": len(all_rows), "powered": len(fam),
                            "holm_significant_<0.05": n_sig}, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    res = main()
    print(f"\n=== Q2 篮子 regime-forward(textbook 未调参,非重叠 + Holm)===")
    hdr = f"{'asset':10} {'win':4} {'对比':16} {'effN(a/b)':10} {'中位(a/b)%':16} {'p_holm':8}"
    print(hdr)
    for r in res["rows"]:
        c = f"{r['bucket_a']}>{r['bucket_b']}"
        eff = f"{r['eff_n_a']}/{r['eff_n_b']}"
        med = f"{r['median_a_noverlap']*100:+.2f}/{r['median_b_noverlap']*100:+.2f}" if np.isfinite(r['median_a_noverlap']) and np.isfinite(r['median_b_noverlap']) else "—"
        ph = f"{r['p_holm']:.3f}" if np.isfinite(r.get('p_holm', float('nan'))) else "n/a"
        print(f"{r['asset']:10} {r['window']:4} {c:16} {eff:10} {med:16} {ph:8}")
