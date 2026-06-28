"""trend_dca 已知答案自测——守住承重逻辑:迟滞信号 / 0 前视 / 成本拖累 / 匹配敞口。
跑:uv run python -m pytest experiments/signal-eval/test_trend_dca.py -q
(纯 pandas/numpy,不需要 scipy;DSR/PSR 由 mstat 自己的测试守。)"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from trend_dca import evaluate_variant, trend_position  # noqa: E402


def _series(vals):
    idx = pd.date_range("2000-01-01", periods=len(vals), freq="D")
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


def test_uptrend_stays_long():
    close = _series(100 * (1.001 ** np.arange(400)))
    pos = trend_position(close, 50, 0.0, 0.0)
    assert pos.iloc[:49].eq(0.0).all()      # 暖机期(MA 未成熟)→ 0
    assert pos.iloc[60:].eq(1.0).all()      # 单调上行 → 持续 long


def test_vshape_enters_and_exits():
    up = 50 + np.arange(120) * 1.0
    down = up[-1] - np.arange(120) * 1.0
    up2 = down[-1] + np.arange(120) * 1.0
    close = _series(np.concatenate([up, down, up2]))
    pos = trend_position(close, 20, 0.0, 0.0)
    turn = (pos - pos.shift(1)).abs().fillna(0.0)
    assert int((turn > 0).sum()) >= 2       # 至少一进一出


def test_no_lookahead_misses_entry_day_jump():
    # 平 100 → 第 60 日跳到 150 → 之后平 150。0 前视下:进场发生在跳空当日 close 之后,
    # held=pos[t-1]=0 → strat 不该吃到那 +50% 跳空 → 终值 ≈ 1(没接住),尽管资产涨了 50%。
    close = _series([100.0] * 60 + [150.0] * 60)
    v = evaluate_variant(close, 20, 0.0, 0.0, cost=0.0)
    assert v is not None
    assert v["wealth_strat"] < 1.05         # 若有前视会接住跳空 → ~1.5


def test_cost_reduces_wealth_and_matched_exposure():
    chop = np.cumprod(1 + np.sin(np.arange(600) / 15.0) * 0.01)   # 来回穿 → 有换手
    close = _series(100 * chop)
    free = evaluate_variant(close, 50, 0.0, 0.0, cost=0.0)
    paid = evaluate_variant(close, 50, 0.0, 0.0, cost=0.005)
    assert free and paid
    assert paid["trades"] >= 1
    assert paid["wealth_strat"] < free["wealth_strat"]            # 成本拖累终值
    assert abs(paid["avg_exposure"] - free["avg_exposure"]) < 1e-9  # 被动敞口=策略平均敞口


if __name__ == "__main__":
    test_uptrend_stays_long()
    test_vshape_enters_and_exits()
    test_no_lookahead_misses_entry_day_jump()
    test_cost_reduces_wealth_and_matched_exposure()
    print("trend_dca self-checks passed")
