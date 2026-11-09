"""单测：宏观入口保留 DXY 与 TIP ETF 的原始观测类型。

黄金/商品类资产的"基本面"=货币因素，由 Macro 看，不新增角色。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def _df(values):
    return pd.DataFrame(
        {"Close": values}, index=pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(values)),
    )


def _fake_get_history(monkeypatch, mapping):
    import openinvest.utils.exchange_fee as ef

    def fake(symbol, period="1mo", as_of_date=None):
        return mapping.get(symbol, pd.DataFrame())
    monkeypatch.setattr(ef, "get_history_data", fake)


def test_macro_data_includes_dxy_and_raw_tip_etf_change(monkeypatch):
    """DXY + TIP 行出现在 macro reference 文本里"""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        "DX-Y.NYB": _df([99.0, 100.0]),   # 美元走强
        "TIP": _df([110.0, 108.9]),       # ETF 价格变化，不等于收益率变化。
    })
    from openinvest.utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "US Dollar Index (DXY" in out
    assert "100.00" in out
    assert "TIPS bond ETF price (TIP" in out
    assert "does not establish" in out and "rising (gold headwind)" not in out


def test_macro_data_tip_up_does_not_assert_a_real_yield_or_gold_direction(monkeypatch):
    """ETF price returns cannot establish a real-yield or gold direction."""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        "DX-Y.NYB": _df([100.0, 99.0]),
        "TIP": _df([108.0, 110.0]),       # TIP 涨
    })
    from openinvest.utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "TIPS bond ETF price (TIP" in out and "+1.85%" in out
    assert "falling (gold tailwind)" not in out


def test_macro_data_graceful_when_dxy_missing(monkeypatch):
    """DXY/TIP 拉不到（空 df）→ 不出对应行，但 VIX/TNX 仍正常（不抛）"""
    _fake_get_history(monkeypatch, {
        "^TNX": _df([4.0, 4.5]),
        "^VIX": _df([15.0, 18.0]),
        # DX-Y.NYB / TIP 缺失 → 返回空 df
    })
    from openinvest.utils.exchange_fee import get_macro_data

    out = get_macro_data()
    assert "10Y Treasury Yield" in out      # 核心仍在
    assert "US Dollar Index" not in out     # 缺数据时不硬凑
    assert "Real-rate proxy" not in out
    assert "Error fetching" not in out      # 不能抛/降级成 error


def test_macro_tool_keeps_etf_measurement_and_original_time(monkeypatch):
    _fake_get_history(monkeypatch, {"^VIX": _df([15, 18]), "TIP": _df([100, 102])})
    from openinvest.capabilities.tools import TOOL_DEFINITIONS, _impl_get_macro_snapshot
    snapshot = _impl_get_macro_snapshot()
    assert snapshot['vix'] == 18 and snapshot['tip_1mo_pct'] == 2
    assert snapshot['real_yield_level'] is None and snapshot['real_yield_direction'] is None
    assert snapshot['as_of_kind'] == 'snapshot_collected_at_not_market_observation'
    source = snapshot['observations']['vix']
    assert source['last_bar'] and source['source_observation']['quote_asof'] is None
    assert source['source_observation']['quote_fresh'] is False
    description = next(x['function']['description'] for x in TOOL_DEFINITIONS
                       if x['function']['name'] == 'get_macro_snapshot')
    assert 'TIP 涨=' not in description and '不能确定' in description


def test_macro_tool_does_not_freshen_stale_failed_or_mixed_data(monkeypatch):
    stale = _df([10, 20]); stale.index = stale.index - pd.Timedelta(days=30)
    failed = _df([4, 5]); failed.attrs['yf_fetch_failed'] = True
    mixed = _df([100, 200]); mixed.attrs['source_observation'] = {'history_basis_qualified': False}
    _fake_get_history(monkeypatch, {'^VIX': stale, '^TNX': failed, 'TIP': mixed})
    from openinvest.capabilities.tools import _impl_get_macro_snapshot
    snapshot = _impl_get_macro_snapshot()
    assert snapshot['vix'] is None and snapshot['tnx'] is None and snapshot['tip_1mo_pct'] is None
    assert not snapshot['observations']['vix']['available']
    assert not snapshot['observations']['tnx']['available']
