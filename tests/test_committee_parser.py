"""parse_cio_memo 的 sanity check 测试 — 防 LLM 过度自信 / prompt injection。"""
from __future__ import annotations

import pytest

from core.config import reset_config, set_config_override
from core.committee import (
    AGENT_UNAVAILABLE_MARKER,
    parse_cio_memo,
)


@pytest.fixture(autouse=True)
def _reset_config():
    """每个 test 重置 config 隔离状态。"""
    reset_config()
    yield
    reset_config()


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
    assert r["alloc_cny"] == 0  # force-HOLD 同时归零方向性信号（不留 SUGGESTED_ALLOC 8000）


def test_multiple_sanity_checks_can_combine():
    """既 unavailable 又 overconfident BUY → 应该被 unavailable 检查接管"""
    text = f"{AGENT_UNAVAILABLE_MARKER}\nVERDICT: BUY\nCONFIDENCE: 0.99\nSUGGESTED_ALLOC_CNY: 5000"
    r = parse_cio_memo(text)
    assert r["verdict"] == "HOLD"
    assert r["confidence"] == 0.4
    assert r["alloc_cny"] == 0


def test_unclear_verdict_when_missing():
    text = "随便写点东西没格式\nCONFIDENCE: 0.5"
    r = parse_cio_memo(text)
    assert r["verdict"] == "UNCLEAR"
    assert r["confidence"] == 0.5


# ---------- config override 实时生效 ----------

def test_config_override_changes_overdrive_threshold():
    """set_config_override() 改变 buy_confidence_overdrive 阈值"""
    # 默认阈值 0.95，confidence=0.93 不触发降级
    text = "VERDICT: BUY\nCONFIDENCE: 0.93\nSUGGESTED_ALLOC_CNY: 5000"
    r1 = parse_cio_memo(text)
    assert r1["verdict"] == "BUY"

    # 把阈值降到 0.90，0.93 现在触发降级
    set_config_override({"verdict": {"buy_confidence_overdrive": 0.90}})
    r2 = parse_cio_memo(text)
    assert r2["verdict"] == "ACCUMULATE"
    assert r2["confidence"] == 0.6


def test_config_override_changes_alloc_ceiling():
    """set_config_override() 改变 alloc_cny_ceiling"""
    # 默认 ceiling ¥100k，¥50k 不 clamp
    text = "VERDICT: BUY\nCONFIDENCE: 0.7\nSUGGESTED_ALLOC_CNY: 50000"
    r1 = parse_cio_memo(text)
    assert r1["alloc_cny"] == 50000

    # 把 ceiling 降到 ¥20k，¥50k 被 clamp
    set_config_override({"verdict": {"alloc_cny_ceiling": 20000}})
    r2 = parse_cio_memo(text)
    assert r2["alloc_cny"] == 20000


# ---------- CIO prompt TRIM 约束条件注入 ----------

def test_cio_prompt_trim_constraint_disabled_by_default():
    """默认阈值 0 → TRIM 约束段不出现在 prompt 里"""
    from agents.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "TRIM 约束" not in prompt
    assert "不允许 TRIM" not in prompt


def test_cio_prompt_trim_constraint_enabled_via_override():
    """set_config_override 注入阈值 > 0 → TRIM 约束段出现"""
    set_config_override({"verdict": {
        "trim_no_trim_loss_pct": 5.0,
        "trim_caution_loss_pct": 10.0,
    }})
    from agents.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "TRIM 约束" in prompt
    assert "5.0%" in prompt
    assert "10.0%" in prompt


# ---------- Sanity check 4: SOLVENCY=strong + TRIM(concentration) → HOLD ----------

def _trim_text(reason: str = "concentration") -> str:
    return (
        f"VERDICT: TRIM\n"
        f"CONFIDENCE: 0.7\n"
        f"DOMINANT_VIEW: risk\n"
        f"SUGGESTED_ALLOC_CNY: -5000\n"
        f"TRIM_REASON: {reason}\n"
    )


def test_sanity4_solvency_strong_trim_concentration_forces_hold():
    """SOLVENCY=strong + TRIM + concentration → 强制 HOLD"""
    r = parse_cio_memo(_trim_text("concentration"), solvency_strong=True)
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "TRIM"
    assert r["trim_reason"] is None
    assert r["confidence"] <= 0.4
    assert r["alloc_cny"] == 0


def test_sanity4_solvency_strong_trim_stop_loss_not_overridden():
    """SOLVENCY=strong + TRIM + stop_loss → 不覆盖（stop_loss 是真实风险）"""
    r = parse_cio_memo(_trim_text("stop_loss"), solvency_strong=True)
    assert r["verdict"] == "TRIM"
    assert r["trim_reason"] == "stop_loss"
    # 未触发 Sanity 4 → confidence / alloc 不应被 force-HOLD 副作用改动
    assert r["confidence"] == 0.7
    assert r["alloc_cny"] == -5000


def test_sanity4_solvency_strong_trim_bearish_not_overridden():
    """SOLVENCY=strong + TRIM + bearish → 不覆盖"""
    r = parse_cio_memo(_trim_text("bearish"), solvency_strong=True)
    assert r["verdict"] == "TRIM"
    assert r["trim_reason"] == "bearish"
    assert r["confidence"] == 0.7
    assert r["alloc_cny"] == -5000


def test_sanity4_solvency_weak_trim_concentration_not_overridden():
    """SOLVENCY≠strong + TRIM + concentration → 不覆盖"""
    r = parse_cio_memo(_trim_text("concentration"), solvency_strong=False)
    assert r["verdict"] == "TRIM"
    assert r["trim_reason"] == "concentration"
    assert r["confidence"] == 0.7
    assert r["alloc_cny"] == -5000


def test_sanity4_trim_reason_extraction():
    """TRIM_REASON 正确提取"""
    r = parse_cio_memo(_trim_text("bearish"), solvency_strong=False)
    assert r["trim_reason"] == "bearish"
    r2 = parse_cio_memo(
        "VERDICT: HOLD\nCONFIDENCE: 0.5\nTRIM_REASON: N/A\n",
        solvency_strong=True,
    )
    assert r2["trim_reason"] is None
    assert r2["verdict"] == "HOLD"


# ---------- Sanity check 5: TRIM 必须给低于现价的买回点，否则降级 HOLD ----------

def _trim_reentry_text(reentry_price="950", reason="bearish") -> str:
    rp = f"REENTRY_PRICE: {reentry_price}\n" if reentry_price is not None else ""
    return (
        "VERDICT: TRIM\n"
        "CONFIDENCE: 0.8\n"
        "DOMINANT_VIEW: quant\n"
        "SUGGESTED_ALLOC_CNY: -5000\n"
        f"TRIM_REASON: {reason}\n"
        f"{rp}"
        "REENTRY_CONDITION: 价格跌至 ¥950 且 RSI<40\n"
        "EXPECTED_PATH: range_bound 顶部，30d 内 55% 概率跌破现价\n"
    )


def test_sanity5_reentry_below_current_keeps_trim():
    """买回点 ¥950 < 现价 ¥1000 → TRIM 成立，保留"""
    r = parse_cio_memo(_trim_reentry_text("950"), current_price=1000.0)
    assert r["verdict"] == "TRIM"
    assert r["reentry_price"] == 950.0
    assert r["reentry_condition"] and r["expected_path"]


def test_sanity5_reentry_at_or_above_current_forces_hold():
    """买回点 ≥ 现价 → 卖了高价接回 = 纯亏 → 降级 HOLD"""
    r = parse_cio_memo(_trim_reentry_text("1050"), current_price=1000.0)
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "TRIM"
    assert r["_sanity5_reason"] == "reentry_not_below_current"


def test_sanity5_reentry_missing_forces_hold():
    """TRIM 但没给 REENTRY_PRICE → 降级 HOLD"""
    r = parse_cio_memo(_trim_reentry_text(None), current_price=1000.0)
    assert r["verdict"] == "HOLD"
    assert r["_sanity5_reason"] == "reentry_missing"


def test_sanity5_skipped_without_current_price():
    """current_price 未知（如存档 re-parse）→ Sanity5 不强制，保留原 verdict"""
    r = parse_cio_memo(_trim_reentry_text("1050"), current_price=None)
    assert r["verdict"] == "TRIM"


def test_sanity5_reentry_price_parses_currency_and_commas():
    """REENTRY_PRICE 支持 ¥ 和千分位"""
    txt = _trim_reentry_text("¥1,234.56")
    r = parse_cio_memo(txt, current_price=2000.0)
    assert r["reentry_price"] == 1234.56
    assert r["verdict"] == "TRIM"
