"""口径修正的确定性差异回算（2026-06-13，零前视、纯价格、可复现）。

证明"全量历史分位 → 近2年(504)分位"这个生产口径修正的影响面,产出**人话差异报告**:
过去每个交易日,regime 标签 / 防御触发 在旧口径 vs 新口径下分别是什么,哪几天会变、为什么。

口径定义(都只用 ≤D 的收盘价,无前视,合法"覆盖过去"):
- 旧(production bug): 分位 = price_D 在**全部 ≤D 收盘价**里的百分位(扩张窗;
  get_history_data 返回全量 DB → 实际就是这个)
- 新(修正): 分位 = price_D 在**近 TRADING_DAYS_2Y 交易日**里的百分位(滚动窗;
  与回测 compute_regime_return_frame / validate_gold_defense 同口径)

只有 price_quantile_2y(regime 用)和 VIX 分位(防御 flag 用)受影响,其余指标
(ma/atr/ret30/rebound)两口径完全相同。

判据**跑前写死、不许看结果改**:regime 阈值走 config(low=0.20/high=0.80),
防御线走 config sentiment.vix_defense_quantile。本脚本不引入任何新自由度。

用法: uv run python scripts/audit_convention_diff.py [--recent N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from core.regime import classify_regime  # noqa: E402
from db.market_store import MarketStore  # noqa: E402
from utils.market_metrics import TRADING_DAYS_2Y  # noqa: E402

ASSETS = ("GC=F", "NDQ.AX")


def _expanding_quantile(close: pd.Series) -> pd.Series:
    """扩张窗分位(旧口径):每天 = price_D 在全部 ≤D 收盘里的百分位。"""
    vals = close.to_numpy()
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        seg = vals[: i + 1]
        out[i] = (seg <= vals[i]).sum() / len(seg)
    return pd.Series(out, index=close.index)


def _rolling_quantile(close: pd.Series, window: int) -> pd.Series:
    """滚动窗分位(新口径):每天 = price_D 在近 window 交易日里的百分位。"""
    return close.rolling(window, min_periods=20).apply(
        lambda w: (w <= w[-1]).sum() / len(w), raw=True)


def _regime_inputs(df: pd.DataFrame):
    """复刻 compute_regime_return_frame 的 regime 输入(除分位外两口径相同)。"""
    close = df["Close"].astype(float)
    ma20 = close.rolling(20).mean()
    ma120 = close.rolling(120).mean()
    ret30 = close / close.shift(30) - 1.0
    rebound = close / close.rolling(30).min() - 1.0
    high, low = df.get("High"), df.get("Low")
    prev = close.shift(1)
    if high is not None and low is not None and not high.isna().all():
        tr = pd.concat([(high - low).abs(), (high - prev).abs(),
                        (low - prev).abs()], axis=1).max(axis=1)
    else:
        tr = (close - prev).abs()
    atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / close * 100.0
    return close, ma20, ma120, atr_pct, ret30, rebound


def _regime_series(df, quantile: pd.Series, symbol: str) -> pd.Series:
    close, ma20, ma120, atr_pct, ret30, rebound = _regime_inputs(df)

    def _v(x):
        return None if pd.isna(x) else float(x)
    out = []
    for i in range(len(close)):
        r = classify_regime({
            "ma20": _v(ma20.iat[i]), "ma120": _v(ma120.iat[i]),
            "atr_pct": _v(atr_pct.iat[i]),
            "price_quantile_2y": _v(quantile.iat[i]),
            "return_30d": _v(ret30.iat[i]),
            "rebound_off_30d_low": _v(rebound.iat[i]),
        }, symbol=symbol)
        out.append(r["regime"])
    return pd.Series(out, index=close.index)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=180,
                    help="只报告最近 N 个交易日的翻转(默认 180)")
    args = ap.parse_args()
    ms = MarketStore()
    cfg = load_config()
    defense_q = cfg.sentiment.vix_defense_quantile

    print(f"口径差异回算(零前视) · 窗口={TRADING_DAYS_2Y}交易日 · 防御线={defense_q:.0%}分位")
    print("旧=全量历史扩张窗(production bug) / 新=近2年滚动窗(修正)\n")

    # ===== regime 标签翻转(per-asset, price_quantile)=====
    for sym in ASSETS:
        df = ms.get_history_df(sym, days=100000)
        if df is None or df.empty:
            continue
        q_old = _expanding_quantile(df["Close"].astype(float))
        q_new = _rolling_quantile(df["Close"].astype(float), TRADING_DAYS_2Y)
        rg_old = _regime_series(df, q_old, sym)
        rg_new = _regime_series(df, q_new, sym)
        recent = df.index[-args.recent:]
        flips = [(d, rg_old[d], rg_new[d], q_old[d], q_new[d])
                 for d in recent if rg_old[d] != rg_new[d]]
        print(f"【{sym}】近 {args.recent} 交易日 regime 翻转: {len(flips)} 天")
        for d, ro, rn, qo, qn in flips:
            print(f"  {d.date()}: regime {ro} → {rn}"
                  f"  (price分位 {qo:.0%}→{qn:.0%})")
        if not flips:
            print("  (无翻转——该资产近期分位两口径同侧)")
        print()

    # ===== 防御 flag 翻转(market-level, VIX)=====
    vix = ms.get_history_df("^VIX", days=100000)
    if vix is not None and not vix.empty:
        v = vix["Close"].astype(float)
        vq_old = _expanding_quantile(v)
        vq_new = _rolling_quantile(v, TRADING_DAYS_2Y)
        flag_old = vq_old >= defense_q
        flag_new = vq_new >= defense_q
        recent = vix.index[-args.recent:]
        flips = [(d, bool(flag_old[d]), bool(flag_new[d]), vq_old[d], vq_new[d])
                 for d in recent if bool(flag_old[d]) != bool(flag_new[d])]
        print(f"【INDEP_DEFENSE_FLAG(VIX 哨兵)】近 {args.recent} 交易日翻转: {len(flips)} 天")
        for d, fo, fn, qo, qn in flips:
            arrow = "on→off" if fo else "off→on"
            eff = ("买侧不再被降级(原会拦的现放行)" if fo
                   else "买侧将被降级(原放行的现拦)")
            print(f"  {d.date()}: 防御 {arrow}  (VIX分位 {qo:.0%}→{qn:.0%}) → {eff}")
        if not flips:
            print("  (无翻转)")


if __name__ == "__main__":
    main()
