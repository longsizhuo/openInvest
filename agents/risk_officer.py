"""Risk Officer - 只看用户上下文 + 风险预算 + 压力测试

不分析市场技术面也不分析宏观环境（那是 Quant 和 Macro 的事）。
专注"用户当前的财务画像和这次操作的风险预算"。

⚠️ Prompt 本体在 `agents/skills/risk_officer/SKILL.md` + `SKILL_rebuttal.md`。

这是当前 invest 系统最缺的视角——所有 BUY 建议都在真空里给，
没人盯"用户已经 70% 重仓"或"子弹只剩 ¥290" 这种关键约束。
"""
from typing import Any, Dict

from agents.skills_loader import load_skill


def build_risk_officer_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    """渲染 Risk Officer prompt（含 asset 占位符替换）

    round_label="opening" → SKILL.md (Round 1 独立陈述)
    round_label="rebuttal" → SKILL_rebuttal.md (Round 2 cross-challenge)
    """
    from core.config import load_config
    asset_name = asset.get("display_name", asset.get("symbol"))
    prompt = load_skill(
        "risk_officer",
        round_label=round_label,
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
    )
    # 集中度 lens 关闭时（单资产/刻意集中策略）压掉超配升级。前置注入而非占位符：一次覆盖
    # opening + rebuttal 两个 SKILL 文件，不会漏某一轮。CONCENTRATION_PCT 字段仍如实输出。
    if not load_config().verdict.concentration_lens_enabled:
        directive = (
            "**🚫 集中度 lens 已关闭（单资产 / 刻意集中策略）**：下方 CONCENTRATION_PCT 字段仍如实"
            "输出该资产占比，但**不得据此升级 SIGNAL 或建议减仓**（跳过 `>60% 至少 concerned` 规则）。"
            "其余风险维度（波动 / 回撤 / 止损 / 现金流动性 / 追涨）照常评估。\n\n"
        )
        prompt = directive + prompt
    return prompt


__all__ = ["build_risk_officer_prompt"]
