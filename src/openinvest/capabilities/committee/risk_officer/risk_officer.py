"""Risk Officer - 只看用户上下文 + 风险预算 + 压力测试

不分析市场技术面也不分析宏观环境（那是 Quant 和 Macro 的事）。
专注"用户当前的财务画像和这次操作的风险预算"。

⚠️ Prompt 本体在 `capabilities/committee/risk_officer/risk_officer.md` + `SKILL_rebuttal.md`。

这是当前 invest 系统最缺的视角——所有 BUY 建议都在真空里给，
没人盯"用户已经 70% 重仓"或"子弹只剩 ¥290" 这种关键约束。
"""
from typing import Any, Dict

from openinvest.capabilities.committee.i18n import (
    bilingual,
    build_field_value_language_directive,
    build_output_language_directive,
    localize_prompt_output_requirements,
)
from openinvest.capabilities.loader import load_skill


def build_risk_officer_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    """渲染 Risk Officer prompt（含 asset 占位符替换）

    round_label="opening" → SKILL.md (Round 1 独立陈述)
    round_label="rebuttal" → SKILL_rebuttal.md (Round 2 cross-challenge)
    """
    from openinvest.core.config import load_config
    asset_name = asset.get("display_name", asset.get("symbol"))
    prompt = load_skill(
        "risk_officer",
        round_label=round_label,
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
    )
    prompt = localize_prompt_output_requirements(prompt)
    prompt = (
        f"{build_output_language_directive(artifact='analysis')}\n"
        f"{build_field_value_language_directive()}\n\n{prompt}"
    )
    # 集中度 lens 关闭时（ADR-020 默认）集中度只作背景：portfolio_summary 仍给真值（藏数字会逼
    # LLM 自算——2026-10-10 编出 1.7%、真值 16.7%），这里令模型照抄、禁自算，且不得据此升级 /
    # 建议减仓。前置注入而非占位符：一次覆盖 opening + rebuttal 两个 SKILL 文件，不会漏某一轮。
    if not load_config().verdict.concentration_lens_enabled:
        directive = bilingual(
            "**🚫 集中度 lens 已关闭（默认；用户录入的可能只是部分资产）**：用户上下文里的「集中度 X%」"
            "是系统算好的真值，**仅作背景与压力测试用**——CONCENTRATION_PCT 照抄该数字，"
            "WORST_CASE_LOSS_PCT_AT_-20 = 该集中度 × 20%，禁止自算或估算占比；"
            "**不得以集中度 / 仓位占比 / 超配为由升级 SIGNAL、建议减仓或压低加仓上限**"
            "（跳过下方模板「核心关注 1」的 PWM 25-35% / >50% 超配标准与 `>60% 至少 concerned` 规则；ONE_LINER 的建仓上限按子弹 DRY_POWDER 表述，不按占总资产 %）。"
            "其余风险维度（波动 / 回撤 / 止损 / 现金流动性 / 追涨）照常评估。\n\n",
            "**🚫 The concentration lens is disabled (default; the user may have recorded only part of their assets)**: the \"集中度 X%\" "
            "figure in the user context is a system-computed true value, **for background and stress testing only** -- copy it verbatim into "
            "CONCENTRATION_PCT and WORST_CASE_LOSS_PCT_AT_-20 = that figure × 20%; never compute or estimate the share yourself. "
            "**Do not escalate SIGNAL, recommend trimming, or lower the add-on cap on grounds of concentration / position share / overweight** "
            "(skip the template's \"core focus 1\" PWM 25-35% / >50% overweight standard and the `>60% at least concerned` rule below; state the ONE_LINER position cap against DRY_POWDER, not as a % of total assets). "
            "Evaluate the other risk dimensions (volatility / drawdown / stop-loss / cash liquidity / chasing rallies) as usual.\n\n",
        )
        prompt = directive + prompt
    return prompt


__all__ = ["build_risk_officer_prompt"]
