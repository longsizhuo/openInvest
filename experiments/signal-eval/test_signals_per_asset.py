"""signals_per_asset 信号生成器已知答案自测(防 false-null:坏信号会把真 edge 也压成 null)。
trend 路径与 evaluate 已由 test_trend_dca 守;这里只守新加的 meanrev/voltarget/breakout。
跑:uv run python -m pytest experiments/signal-eval/test_signals_per_asset.py -q"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from signals_per_asset import sig_breakout, sig_meanrev, sig_voltarget  # noqa: E402


def _series(vals):
    idx = pd.date_range("2000-01-01", periods=len(vals), freq="D")
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


def test_meanrev_reacts_oversold_overbought():
    # V 形:先跌(超卖→做多)后涨(超买→空仓)。meanrev 两个状态都该出现。
    down = np.linspace(100, 80, 60)
    up = np.linspace(80, 120, 60)
    pos = sig_meanrev(_series(np.concatenate([down, up])), window=20, z_enter=1.0)
    assert pos.max() == 1.0     # 超卖时做多(接下跌的刀=均值回归本性)
    assert pos.min() == 0.0     # 超买时空仓


def test_breakout_goes_long_on_new_highs():
    close = _series(100 * (1.001 ** np.arange(400)))   # 单调上行,持续创新高
    pos = sig_breakout(close, window=50)
    assert pos.iloc[80:].eq(1.0).all()


def test_voltarget_cuts_exposure_in_high_vol():
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.2, 300))     # 低波动
    wild = calm[-1] + np.cumsum(rng.normal(0, 3.0, 300))  # 高波动
    pos = sig_voltarget(_series(np.concatenate([calm, wild])), window=20, target_ann=0.15)
    lo = pos.iloc[60:300].mean()    # 低波动段平均敞口
    hi = pos.iloc[330:].mean()      # 高波动段平均敞口
    assert hi < lo                  # 波动率目标:波动越高敞口越低


if __name__ == "__main__":
    test_meanrev_reacts_oversold_overbought()
    test_breakout_goes_long_on_new_highs()
    test_voltarget_cuts_exposure_in_high_vol()
    print("signals_per_asset self-checks passed")
