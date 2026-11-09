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
from openinvest.capabilities.loader import apply_lens_blocks, load_skill


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
    lens_on = bool(load_config().verdict.concentration_lens_enabled)
    # 模板里集中度专属的字段 / 标准 / 升级规则用 lens 标记包住，lens 关时整块删掉
    prompt = apply_lens_blocks(prompt, "concentration", lens_on)
    prompt = localize_prompt_output_requirements(prompt)
    prompt = (
        f"{build_output_language_directive(artifact='analysis')}\n"
        f"{build_field_value_language_directive()}\n\n{prompt}"
    )
    # 集中度 lens 关闭时（ADR-020 默认）不给、也不许估算占比——用户录入的常只是部分资产。
    # 前置注入而非占位符：一次覆盖 opening + rebuttal 两个 SKILL 文件，不会漏某一轮。
    # 压力测试改用 portfolio_summary 给的绝对损失金额（2026-10-11：缺这个数时模型自编占比）。
    if not lens_on:
        from openinvest.utils.base_currency import base_currency
        base = base_currency()
        directive = bilingual(
            "**🚫 集中度 lens 已关闭（默认；用户录入的可能只是部分资产）**：用户上下文【不含】集中度数字。"
            "**不要输出 CONCENTRATION_PCT 字段，也不要提及、估算或换算该资产占总资产的比例**（压力测试同样只用金额："
            f"WORST_CASE_LOSS_{base}_AT_-20 仅照抄已给的情景损失，否则 N/A）。不得从 DRY_POWDER_{base} 推出未授权的新建仓上限。"
            "其余风险维度（波动 / 回撤 / 止损 / 现金流动性 / 追涨）照常评估。\n\n",
            "**🚫 The concentration lens is disabled (default; the user may have recorded only part of their assets)**: the user context contains no concentration figures. "
            "**Do not output the CONCENTRATION_PCT field, and do not mention, estimate, or convert this asset's share of total assets** (stress tests use amounts only: "
            f"copy only a supplied scenario loss into WORST_CASE_LOSS_{base}_AT_-20, otherwise N/A). Do not derive an unauthorized position cap from DRY_POWDER_{base}. "
            "Evaluate the other risk dimensions (volatility / drawdown / stop-loss / cash liquidity / chasing rallies) as usual.\n\n",
        )
        prompt = directive + prompt
    return prompt


__all__ = ["build_risk_officer_prompt"]
