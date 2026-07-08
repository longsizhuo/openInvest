"""parse_cio_memo 的 sanity check 测试 — 防 LLM 过度自信 / prompt injection。"""
from __future__ import annotations

import pytest

from openinvest.core.config import reset_config, set_config_override
from openinvest.core.committee import (
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
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "TRIM 约束" not in prompt
    assert "不允许 TRIM" not in prompt


def test_cio_prompt_trim_constraint_enabled_via_override():
    """set_config_override 注入阈值 > 0 → TRIM 约束段出现"""
    set_config_override({"verdict": {
        "trim_no_trim_loss_pct": 5.0,
        "trim_caution_loss_pct": 10.0,
    }})
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "TRIM 约束" in prompt
    assert "5.0%" in prompt
    assert "10.0%" in prompt


# ---------- 现金仓位机会成本规则开关（ADR-024）----------

def test_cio_prompt_cash_opp_cost_directive_when_off_by_default():
    """默认（rule OFF）→ 注入"机会成本规则已被用户关闭"directive，作废低集中度强制加仓"""
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "现金仓位机会成本规则已被用户关闭" in prompt
    assert "任何仓位 / 任何现金比例" in prompt


def test_cio_prompt_no_cash_opp_cost_directive_when_enabled():
    """显式开启 → directive 不出现，原硬编码规则照常生效"""
    set_config_override({"verdict": {"cash_opportunity_cost_rule_enabled": True}})
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "现金仓位机会成本规则已被用户关闭" not in prompt
    # 原规则文本仍在（占位符为空串，规则段保留）
    assert "现金仓位机会成本规则" in prompt


# ---------- Sanity check 4: 集中度 lens 关 → concentration-TRIM 强制 HOLD ----------
# 2026-06-23：solvency 自动兜底（"兜底充足 ⇒ 账户内集中度高不算风险"）已移除——它在
# 事后悄悄反转 CIO 的减仓、掩盖真实集中度风险，且只在 parse 层动手、prompt 层不知情，
# 导致"CIO 据理力争减仓 vs 裁决 HOLD"的自相矛盾。集中度是否构成约束，只由显式 lens
# 开关说了算（关 lens 时同时在 Risk/CIO prompt 软抑制 + 这里硬兜底，两层一致）。

def _trim_text(reason: str = "concentration") -> str:
    return (
        f"VERDICT: TRIM\n"
        f"CONFIDENCE: 0.7\n"
        f"DOMINANT_VIEW: risk\n"
        f"SUGGESTED_ALLOC_CNY: -5000\n"
        f"TRIM_REASON: {reason}\n"
    )


def test_parse_cio_memo_rejects_solvency_strong_kwarg():
    """solvency 自动兜底已移除 → parse_cio_memo 不再接受 solvency_strong（防回潮）"""
    with pytest.raises(TypeError):
        parse_cio_memo(_trim_text("concentration"), solvency_strong=True)


def test_concentration_trim_visible_when_lens_on():
    """lens 显式开 → concentration-TRIM 如实保留，不被任何写死兜底掩盖"""
    set_config_override({"verdict": {"concentration_lens_enabled": True}})
    r = parse_cio_memo(_trim_text("concentration"))
    assert r["verdict"] == "TRIM"
    assert r["trim_reason"] == "concentration"
    assert r["confidence"] == 0.7
    assert r["alloc_cny"] == -5000
    assert "_concentration_lens" not in r


def test_non_concentration_trim_visible_when_lens_on():
    """lens 开 → stop_loss / bearish 等真实风险 TRIM 一律如实保留"""
    for reason in ("stop_loss", "bearish"):
        r = parse_cio_memo(_trim_text(reason))
        assert r["verdict"] == "TRIM"
        assert r["trim_reason"] == reason
        assert r["confidence"] == 0.7
        assert r["alloc_cny"] == -5000


def test_trim_reason_extraction():
    """TRIM_REASON 正确提取；HOLD + N/A → None"""
    assert parse_cio_memo(_trim_text("bearish"))["trim_reason"] == "bearish"
    r2 = parse_cio_memo("VERDICT: HOLD\nCONFIDENCE: 0.5\nTRIM_REASON: N/A\n")
    assert r2["trim_reason"] is None
    assert r2["verdict"] == "HOLD"


# ---------- 集中度 lens 开关 (concentration_lens_enabled) ----------

def test_concentration_lens_off_forces_hold():
    """lens 关 → concentration-TRIM 强制 HOLD（移除 solvency 后唯一的集中度兜底路径）"""
    set_config_override({"verdict": {"concentration_lens_enabled": False}})
    r = parse_cio_memo(_trim_text("concentration"))
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "TRIM"
    assert r["trim_reason"] is None
    assert r["alloc_cny"] == 0
    assert r["confidence"] <= 0.4
    assert r["_concentration_lens"] == "disabled"


def test_concentration_lens_off_by_default_forces_hold():
    """默认 lens 关（ADR-020，2026-06-25）→ concentration-TRIM 默认即被 force-HOLD，无需显式 override"""
    r = parse_cio_memo(_trim_text("concentration"))
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "TRIM"
    assert r["_concentration_lens"] == "disabled"


def test_concentration_lens_off_keeps_stop_loss_trim():
    """lens 关只压"超配"，真实风险（stop_loss）TRIM 不受影响"""
    set_config_override({"verdict": {"concentration_lens_enabled": False}})
    r = parse_cio_memo(_trim_text("stop_loss"))
    assert r["verdict"] == "TRIM"
    assert r["trim_reason"] == "stop_loss"
    assert r["alloc_cny"] == -5000


def test_cio_prompt_no_concentration_directive_when_lens_on():
    """lens 显式开 → CIO prompt 不含关闭指令"""
    set_config_override({"verdict": {"concentration_lens_enabled": True}})
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "集中度 lens 已被用户关闭" not in prompt


def test_cio_prompt_concentration_directive_on_when_lens_disabled():
    """lens 关 → CIO prompt 注入关闭指令"""
    set_config_override({"verdict": {"concentration_lens_enabled": False}})
    from openinvest.capabilities.committee.cio import build_cio_prompt
    prompt = build_cio_prompt({"symbol": "GC=F", "display_name": "黄金"})
    assert "集中度 lens 已被用户关闭" in prompt


def test_risk_officer_prompt_concentration_directive_both_rounds():
    """lens 关 → Risk Officer opening + rebuttal 两轮 prompt 都注入关闭指令"""
    from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt
    asset = {"symbol": "GC=F", "display_name": "黄金"}
    set_config_override({"verdict": {"concentration_lens_enabled": True}})
    assert "集中度 lens 已关闭" not in build_risk_officer_prompt(asset)
    set_config_override({"verdict": {"concentration_lens_enabled": False}})
    assert "集中度 lens 已关闭" in build_risk_officer_prompt(asset, round_label="opening")
    assert "集中度 lens 已关闭" in build_risk_officer_prompt(asset, round_label="rebuttal")


def test_committee_prompts_follow_english_mode_and_keep_parser_markers():
    """INVEST_LANG=en 时自然语言切英文，但结构化解析锚点仍要求保持英文。"""
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.macro_strategist import build_macro_strategist_prompt
    from openinvest.capabilities.committee.quant import build_quant_prompt
    from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt

    set_config_override({"language": {"invest_lang": "en"}})
    asset = {"symbol": "GC=F", "display_name": "Gold"}

    quant_prompt = build_quant_prompt(asset)
    risk_prompt = build_risk_officer_prompt(asset)
    cio_prompt = build_cio_prompt(asset)
    macro_prompt = build_macro_strategist_prompt()

    assert "Produce your analysis in English." in quant_prompt
    assert "Produce your analysis in English." in risk_prompt
    assert "Produce your analysis in English." in macro_prompt
    assert "Produce your analysis memo in English." in cio_prompt
    assert "Keep all required section headers" in cio_prompt
    assert "VERDICT, CONFIDENCE, DOMINANT_VIEW" in cio_prompt
    assert "All free-text field values after the fixed English field names must also be in English." in quant_prompt
    assert "All free-text field values after the fixed English field names must also be in English." in risk_prompt
    assert "All free-text field values after the fixed English field names must also be in English." in macro_prompt
    assert "All free-text field values after the fixed English field names must also be in English." in cio_prompt


def test_committee_prompts_default_to_chinese_mode():
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.quant import build_quant_prompt

    asset = {"symbol": "GC=F", "display_name": "黄金"}
    assert "请使用中文输出你的分析。" in build_quant_prompt(asset)
    assert "请使用中文输出你的分析备忘。" in build_cio_prompt(asset)


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
