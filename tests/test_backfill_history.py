"""scripts.backfill_history 单元测试。

只测纯函数 _num（NaN guard）—— backfill()/main() 要 yfinance + MarketStore，
集成成本高，交给 smoke-import + 手动跑覆盖。
"""
from __future__ import annotations

import numpy as np

from scripts.backfill_history import _num


def test_num_passes_real_values_through_as_float():
    assert _num(1234) == 1234.0
    assert isinstance(_num(1234), float)
    assert _num(0) == 0.0  # 0 不是 None，照常通过


def test_num_maps_nan_and_none_to_none():
    # yfinance 停牌/退市日返 NaN OHLC → 必须变 None，不能 float(nan) 混进库
    assert _num(float("nan")) is None
    assert _num(np.nan) is None
    assert _num(None) is None
