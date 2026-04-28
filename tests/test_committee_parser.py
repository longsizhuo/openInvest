"""parse_cio_memo 的 sanity check 测试 — 防 LLM 过度自信 / prompt injection。"""
from __future__ import annotations

from core.committee import (
    AGENT_UNAVAILABLE_MARKER,
    parse_cio_memo,
)


def test_parse_basic():
    text = """
VERDICT: HOLD
CONFIDENCE: 0.7
DOMINANT_VIEW: macro
SUGGESTED_ALLOC_CNY: 0
"""
    r = parse_cio_memo(text)
    assert r["verdict"] == "HOLD"
    assert r["confidence"] == 0.7
    assert r["dominant_view"] == "macro"
    assert r["alloc_cny"] == 0


def test_overconfident_buy_downgraded():
    """audit security M3: confidence>=0.95 + BUY 必降级到 ACCUMULATE"""
    text = "VERDICT: BUY\nCONFIDENCE: 0.99\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 5000"
    r = parse_cio_memo(text)
    assert r["verdict"] == "ACCUMULATE"
    assert r["confidence"] == 0.6
    assert r["_original_verdict"] == "BUY"
    assert r["_original_confidence"] == 0.99


def test_high_confidence_hold_not_downgraded():
    """confidence 高但 verdict 不是 BUY 时不应改"""
    text = "VERDICT: HOLD\nCONFIDENCE: 0.99\nDOMINANT_VIEW: risk\nSUGGESTED_ALLOC_CNY: 0"
    r = parse_cio_memo(text)
    assert r["verdict"] == "HOLD"
    assert r["confidence"] == 0.99


def test_alloc_clamped_when_oversized():
    """单笔超过 ¥100k 大概率 LLM 输出错误，clamp 防误下单"""
    text = "VERDICT: BUY\nCONFIDENCE: 0.7\nSUGGESTED_ALLOC_CNY: 999999"
    r = parse_cio_memo(text)
    assert r["alloc_cny"] == 100000
    assert r["_original_alloc"] == 999999


def test_alloc_negative_clamped():
    text = "VERDICT: SELL\nCONFIDENCE: 0.8\nSUGGESTED_ALLOC_CNY: -500000"
    r = parse_cio_memo(text)
    assert r["alloc_cny"] == -100000


def test_worker_unavailable_forces_hold():
    """audit algo M4: brief 含 [WORKER_UNAVAILABLE] 时强制 HOLD + low confidence"""
    text = f"""
macro: {AGENT_UNAVAILABLE_MARKER} reason=retry_exhausted
quant: bullish strength 8
VERDICT: BUY
CONFIDENCE: 0.85
SUGGESTED_ALLOC_CNY: 8000
"""
    r = parse_cio_memo(text)
    assert r["verdict"] == "HOLD"
    assert r["confidence"] == 0.4


def test_multiple_sanity_checks_can_combine():
    """既 unavailable 又 overconfident BUY → 应该被 unavailable 检查接管"""
    text = f"{AGENT_UNAVAILABLE_MARKER}\nVERDICT: BUY\nCONFIDENCE: 0.99\nSUGGESTED_ALLOC_CNY: 5000"
    r = parse_cio_memo(text)
    assert r["verdict"] == "HOLD"
    assert r["confidence"] == 0.4


def test_unclear_verdict_when_missing():
    text = "随便写点东西没格式\nCONFIDENCE: 0.5"
    r = parse_cio_memo(text)
    assert r["verdict"] == "UNCLEAR"
    assert r["confidence"] == 0.5
