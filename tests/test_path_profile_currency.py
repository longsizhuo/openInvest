"""币种自适应 path-profile（ADR-021）覆盖 —— 原 PR#95 仅测 convert_ccy=None 路径，
FX 转换路径零覆盖。这里覆盖：报价币种识别、是否需转换、currency_overlay 的逐日对齐
转换数学、缺汇率时不挂 overlay 的护栏、base windows 不受影响、FX 腿零前视(asof)、
人话提示用报价币种而非写死 USD。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import openinvest.db.market_store as ms
from openinvest.core.regime_probability import (
    quote_currency_iso,
    convert_ccy_for,
    _fx_forward_returns,
    get_path_profile,
    build_reentry_reference,
)


def _stub_marketstore(monkeypatch, frames):
    """frames: dict[symbol -> DataFrame]。get_history_df 按 symbol 派发。"""
    class _S:
        def get_history_df(self, symbol, days=730):
            return frames.get(symbol)
    monkeypatch.setattr(ms, "MarketStore", lambda: _S())


def _rising(symbol_close, n=400, start="2015-01-01"):
    idx = pd.date_range(start, periods=n, freq="D")
    close = symbol_close(np.arange(n))
    return pd.DataFrame(
        {"Close": close, "High": close * 1.01, "Low": close * 0.99}, index=idx
    )


# ---------- 报价币种识别 ----------


def test_quote_currency_iso_covers_major_suffixes():
    cases = {
        "GC=F": "USD", "AAPL": "USD", "NDQ.AX": "AUD", "0700.HK": "HKD",
        "510300.SS": "CNY", "SHEL.L": "GBP", "7203.T": "JPY", "SHOP.TO": "CAD",
        "BTC-USD": "USD", "ETH-EUR": "EUR",
    }
    for sym, exp in cases.items():
        assert quote_currency_iso(sym) == exp, sym
    assert quote_currency_iso("^GSPC") is None  # 指数无币种


def test_convert_ccy_for_no_false_mismatch_on_correctly_labeled_quote():
    # 回归 PR#95 bug: .L 曾被当 USD → 对 cost=GBP 的 GBP 资产捏造一条假转换
    assert convert_ccy_for("SHEL.L", "GBP") is None       # 报价=成本=GBP,无错配
    assert convert_ccy_for("GC=F", "CNY") == "CNY"        # USD 资产记 CNY → 需转
    assert convert_ccy_for("GC=F", "USD") is None         # 同币种
    assert convert_ccy_for("GC=F", None) is None          # 无 cost_currency


# ---------- currency_overlay：逐日对齐转换数学 ----------


def test_currency_overlay_is_date_aligned_product(monkeypatch):
    # FX 几何增长 → 每个 w 的远期收益是常数 k_w；逐日对齐转换后中位 = (1+base_med)(1+k)-1
    g = 0.0002  # 0.02%/日
    _stub_marketstore(monkeypatch, {
        "GC=F": _rising(lambda a: np.linspace(100, 260, len(a))),       # 上行 → uptrend
        "USDCNY=X": _rising(lambda a: 6.0 * (1.0 + g) ** a),            # 几何增长
    })
    base = get_path_profile("GC=F", "uptrend", windows=("30d",))
    conv = get_path_profile("GC=F", "uptrend", windows=("30d",), convert_ccy="CNY")
    assert base and conv
    ov = conv["currency_overlay"]
    assert ov["currency"] == "CNY"
    assert "date-aligned" in ov["currency_method"]

    bw = base["windows"]["30d"]
    ow = ov["windows"]["30d"]
    k = (1.0 + g) ** 30 - 1.0  # 30 日历日 FX 远期收益（freq=D 无 gap → searchsorted=i+30）
    expected_med = ((1.0 + bw["median_pct"] / 100.0) * (1.0 + k) - 1.0) * 100.0
    assert abs(ow["median_pct"] - round(expected_med, 2)) < 0.05
    # 转换样本数 = 对齐交集（FX 覆盖全程 → 等于 base 样本）
    assert ow["fx_aligned_n"] == bw["n"]


def test_base_windows_unchanged_by_convert(monkeypatch):
    # 主 windows 始终报价币种：带不带 convert_ccy，base windows 必须逐字一致
    _stub_marketstore(monkeypatch, {
        "GC=F": _rising(lambda a: np.linspace(100, 260, len(a))),
        "USDCNY=X": _rising(lambda a: 6.0 + a * 0.001),
    })
    base = get_path_profile("GC=F", "uptrend", windows=("30d", "60d"))
    conv = get_path_profile("GC=F", "uptrend", windows=("30d", "60d"), convert_ccy="CNY")
    assert base["windows"] == conv["windows"]
    assert "currency_overlay" in conv and "currency_overlay" not in base


def test_no_overlay_when_fx_missing(monkeypatch):
    # 护栏（CR#2）：USDCNY=X 无数据时不能挂一条未折算的本币分布冒充 CNY 口径
    _stub_marketstore(monkeypatch, {
        "GC=F": _rising(lambda a: np.linspace(100, 260, len(a))),
        # 故意不提供 USDCNY=X → get_history_df 返回 None
    })
    conv = get_path_profile("GC=F", "uptrend", windows=("30d",), convert_ccy="CNY")
    assert conv is not None
    assert "currency_overlay" not in conv


# ---------- FX 腿口径：日历日 + 零前视 ----------


def test_fx_forward_returns_calendar_day_and_asof(monkeypatch):
    _stub_marketstore(monkeypatch, {
        "USDCNY=X": _rising(lambda a: 6.0 * (1.0 + 0.0002) ** a, n=200),
    })
    out, sym = _fx_forward_returns("USD", "CNY", ("30d",))
    assert sym == "USDCNY=X"
    s = out["30d"]
    assert isinstance(s, pd.Series)
    # 日历日远期收益（freq=D）应 ≈ 常数 (1.0002**30 - 1)
    k = (1.0002) ** 30 - 1.0
    assert abs(float(s.iloc[0]) - k) < 1e-6
    # asof 截点：返回 Series 不含 asof 之后的日期（零前视）
    out2, _ = _fx_forward_returns("USD", "CNY", ("30d",), asof="2015-03-01")
    assert out2["30d"].index.max() <= pd.Timestamp("2015-03-01")


# ---------- 人话提示用报价币种，不写死 USD（CR#4）----------


def test_reentry_note_uses_quote_ccy_not_hardcoded_usd(monkeypatch):
    # NDQ.AX 报价 AUD、持仓 CNY → 提示该说 "AUD 口径" 而非 "USD 口径"
    _stub_marketstore(monkeypatch, {
        "NDQ.AX": _rising(lambda a: np.linspace(20, 52, len(a))),
        "AUDCNY=X": _rising(lambda a: 4.7 * (1.0 + 0.0002) ** a),
    })
    text, profile = build_reentry_reference(
        "NDQ.AX", "uptrend", 50.0, convert_ccy="CNY",
    )
    assert profile and profile.get("currency_overlay")
    assert "持仓以 CNY 计价" in text
    assert "AUD 口径" in text
    assert "USD 口径" not in text  # 不再写死 USD
