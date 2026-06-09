"""单测：get_macro_data 货币因素扩展（DXY + 实际利率代理 TIP）

黄金/商品类资产的"基本面"=货币因素，由 Macro 看，不新增角色。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def _df(values):
    return pd.DataFrame(
        {"Close": values}, index=pd.date_range("2024-01-01", periods=len(values)),
    )


def _fake_get_history(monkeypatch, mapping):
    import utils.exchange_fee as ef

    def fake(symbol, period="1mo", as_of_date=None):
        return mapping.get(symbol, pd.DataFrame())
    monkeypatch.setattr(ef, "get_history_data", fake)


def test_macro_data_includes_dxy_and_real_rate(monkeypatch):
    """DXY + TIP 行出现在 macro reference 文本里"""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        "DX-Y.NYB": _df([99.0, 100.0]),   # 美元走强
        "TIP": _df([110.0, 108.9]),       # TIP 跌 → 实际利率上行 → 利空黄金
    })
    from utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "US Dollar Index (DXY" in out
    assert "100.00" in out
    assert "Real-rate proxy (TIPS ETF TIP" in out
    assert "rising (gold headwind)" in out


def test_macro_data_tip_up_is_tailwind(monkeypatch):
    """TIP 涨 → 实际利率下行 → 利好黄金"""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        "DX-Y.NYB": _df([100.0, 99.0]),
        "TIP": _df([108.0, 110.0]),       # TIP 涨
    })
    from utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "falling (gold tailwind)" in out


def test_macro_data_graceful_when_dxy_missing(monkeypatch):
    """DXY/TIP 拉不到（空 df）→ 不出对应行，但 VIX/TNX 仍正常（不抛）"""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        # DX-Y.NYB / TIP 缺失 → 返回空 df
    })
    from utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "10Y Treasury Yield" in out      # 核心仍在
    assert "US Dollar Index" not in out     # 缺数据时不硬凑
    assert "Real-rate proxy" not in out
    assert "Error fetching" not in out      # 不能抛/降级成 error
