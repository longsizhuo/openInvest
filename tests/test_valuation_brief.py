"""单测：估值 brief（utils/valuation.py）

验证按资产类型分流：
1. 权益类（ETF）→ 出 trailing PE + 价格分位
2. 商品/期货（黄金 GC=F, quoteType=FUTURE）→ 返回 ""（走 Macro 货币因素）
3. PE 缺失 / .info 异常 → graceful 退化 ""
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeTicker:
    def __init__(self, info):
        self.info = info


def _patch_yf(monkeypatch, info):
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda sym: _FakeTicker(info))


def test_equity_etf_emits_pe_and_quantile(monkeypatch):
    """ETF（NDQ.AX 类）→ trailing PE + 价格分位都出"""
    _patch_yf(monkeypatch, {"quoteType": "ETF", "trailingPE": 35.8})
    from utils.valuation import build_valuation_brief

    brief = build_valuation_brief("NDQ.AX", price_quantile_2y=0.82)
    assert "trailing_PE=35.8" in brief
    assert "偏贵" in brief                  # 35.8 ≥ 30 阈值
    assert "PRICE_QUANTILE_2Y: 82%" in brief
    assert "forward 盈利增速 v1 不可得" in brief


def test_cheap_pe_labeled(monkeypatch):
    """trailing PE < 18 → 中性偏低"""
    _patch_yf(monkeypatch, {"quoteType": "EQUITY", "trailingPE": 12.0})
    from utils.valuation import build_valuation_brief

    brief = build_valuation_brief("SOME.EQ")
    assert "中性偏低" in brief


def test_gold_future_returns_empty(monkeypatch):
    """黄金 GC=F（quoteType=FUTURE）→ 返回 ""（基本面=货币因素走 Macro）"""
    _patch_yf(monkeypatch, {"quoteType": "FUTURE", "shortName": "Gold"})
    from utils.valuation import build_valuation_brief

    assert build_valuation_brief("GC=F", price_quantile_2y=0.5) == ""


def test_currency_returns_empty(monkeypatch):
    """汇率类 → 返回 ''"""
    _patch_yf(monkeypatch, {"quoteType": "CURRENCY"})
    from utils.valuation import build_valuation_brief

    assert build_valuation_brief("USDCNY=X") == ""


def test_missing_pe_and_no_quantile_returns_empty(monkeypatch):
    """权益类但 PE 缺失 + 无分位 → 不硬凑空 section，返回 ''"""
    _patch_yf(monkeypatch, {"quoteType": "ETF", "trailingPE": None})
    from utils.valuation import build_valuation_brief

    assert build_valuation_brief("NDQ.AX") == ""


def test_equity_pe_missing_but_quantile_present(monkeypatch):
    """PE 缺失但有价格分位 → 仍出分位行（不依赖 PE）"""
    _patch_yf(monkeypatch, {"quoteType": "ETF", "trailingPE": None})
    from utils.valuation import build_valuation_brief

    brief = build_valuation_brief("NDQ.AX", price_quantile_2y=0.6)
    assert "PRICE_QUANTILE_2Y: 60%" in brief
    assert "trailing_PE" not in brief


def test_info_exception_degrades_to_empty(monkeypatch):
    """yfinance .info 抛异常 → graceful 退化 ''（不阻断 committee）"""
    import yfinance as yf

    def boom(sym):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(yf, "Ticker", boom)
    from utils.valuation import build_valuation_brief

    assert build_valuation_brief("NDQ.AX", price_quantile_2y=0.5) == ""


def test_pe_zero_or_negative_ignored(monkeypatch):
    """PE ≤ 0（亏损/脏数据）→ 不输出 VALUATION 行"""
    _patch_yf(monkeypatch, {"quoteType": "ETF", "trailingPE": -5.0})
    from utils.valuation import build_valuation_brief

    brief = build_valuation_brief("NDQ.AX", price_quantile_2y=0.4)
    assert "trailing_PE" not in brief
    assert "PRICE_QUANTILE_2Y: 40%" in brief


def test_pe_thresholds_come_from_config(monkeypatch):
    """PE 分档走 config (valuation 节)：抬高 pe_expensive 后 35.8 不再"偏贵"。"""
    from core.config import reset_config, set_config_override
    from utils.valuation import build_valuation_brief
    import yfinance as yf

    class FakeTicker:
        info = {"quoteType": "ETF", "trailingPE": 35.8}

    monkeypatch.setattr(yf, "Ticker", lambda *_a, **_k: FakeTicker())

    reset_config()
    try:
        brief = build_valuation_brief("NDQ.AX")
        assert "偏贵" in brief  # 默认 30 → 35.8 偏贵

        set_config_override({"valuation": {"pe_expensive": 40.0}})
        brief2 = build_valuation_brief("NDQ.AX")
        assert "偏贵" not in brief2.split("\n")[0].split("阈值")[0]  # 标签变中性
        assert "中性" in brief2
        assert ">40" in brief2  # brief 文案里的阈值数字跟着 config 走
    finally:
        reset_config()
