"""Q2 关键逻辑单测:forward-return(0 前视 + 尾部 NaN)、非重叠间隔、textbook regime。
跑法同 mstat:uv run --with scipy --with statsmodels python -m pytest experiments/signal-eval/ -q"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy")
pytest.importorskip("statsmodels")

sys.path.insert(0, os.path.dirname(__file__))
import regime_forward_q2 as q2  # noqa: E402


def test_forward_return_calendar_and_tail_nan():
    idx = pd.date_range("2020-01-01", periods=10, freq="D")
    close = pd.Series(np.arange(1, 11), index=idx, dtype=float)  # 1..10
    fwd = q2._forward_return(close, 3)  # t + 3 日历日
    assert fwd[0] == pytest.approx(4 / 1 - 1)   # t0 价1 → 第一个 ≥ 01-04 = idx3 价4
    assert fwd[1] == pytest.approx(5 / 2 - 1)
    assert np.isnan(fwd[-1]) and np.isnan(fwd[-2]) and np.isnan(fwd[-3])  # 尾部无 t+3 → NaN


def test_nonoverlap_spacing():
    idx = pd.date_range("2020-01-01", periods=100, freq="D")
    picked = idx[q2._nonoverlap_idx(idx, 10)]
    diffs = [(picked[i + 1] - picked[i]).days for i in range(len(picked) - 1)]
    assert all(d >= 10 for d in diffs)        # 互不重叠
    assert len(picked) == 10                  # 100 日 / 10 = 10 个


def test_textbook_regime_trend_and_warmup():
    idx = pd.date_range("2020-01-01", periods=300, freq="D")
    close = pd.Series(np.linspace(1.0, 2.0, 300), index=idx)  # 单调上行
    reg = q2._textbook_regime(close)
    assert reg["trend"].iloc[-1] == "above"   # 末端在 MA200 之上
    assert reg["trend"].iloc[0] is None       # 不足 200 → None
    assert reg["stress"].iloc[0] is None      # 不足 252 → None


def test_textbook_regime_stress():
    idx = pd.date_range("2020-01-01", periods=300, freq="D")
    base = np.linspace(1.0, 2.0, 260)
    crash = np.linspace(2.0, 1.4, 40)         # 末端 −30% 回撤
    close = pd.Series(np.concatenate([base, crash]), index=idx)
    reg = q2._textbook_regime(close)
    assert reg["stress"].iloc[-1] == "stress"  # 深回撤 → stress


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
