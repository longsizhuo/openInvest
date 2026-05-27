"""CIO - 综合 Quant / Macro / Risk Officer 三人输出，给最终客户备忘

CIO 不重做分析，只综合 + 决策 + 输出执行方案。
强制读三方的 SIGNAL/ONE_LINER + 用户上下文，给完整的投行级 memo。

⚠️ Prompt 本体在 `agents/skills/cio/SKILL.md`。

2026-05-18: DSPy v2 few-shot demos 经 sandbox A/B + MIPROv2 train 双重验证后**不接入**
production——zero-shot baseline 已接近天花板 (67% verdict accuracy)，random demos
压制 reasoning quality (REGIME mention 64%→1.8%)，DSPy optimized 也 0pp improvement。
helper 函数 `agents/dspy_few_shot_loader.py` 保留作未来 path c 重训后的接入路径。
"""
from typing import Any, Dict

from agents.skills_loader import load_skill


def build_cio_prompt(asset: Dict[str, Any]) -> str:
    """渲染 CIO prompt（含 asset 占位符 + config 阈值条件注入）"""
    from core.config import load_config
    asset_name = asset.get("display_name", asset.get("symbol"))
    verdict_cfg = load_config().verdict

    # TRIM 约束：阈值 > 0 时注入（sweep 出 OOS 验证结果后才启用，遵守 ADR-010 rule 4）
    trim_constraint = ""
    if verdict_cfg.trim_no_trim_loss_pct > 0 and verdict_cfg.trim_caution_loss_pct > 0:
        trim_constraint = (
            f"**🔥 零花钱账户 + 强破产兜底时的 TRIM 约束（强制）**：\n"
            f"当 Wealth Context 显示 SOLVENCY_BUFFER=strong 且 account_purpose 含\"零花钱\"或类似表述时：\n"
            f"- **浮亏 < {verdict_cfg.trim_no_trim_loss_pct}% 不允许 TRIM** — 卖出坐实亏损，而用户无流动性压力，应 HOLD 等修复\n"
            f"- **浮亏 {verdict_cfg.trim_no_trim_loss_pct}-{verdict_cfg.trim_caution_loss_pct}% 且 Macro 非强 risk_off：倾向 HOLD** — 零花钱账户的资金久期长，短期波动不是卖出理由\n"
            f"- 只有浮亏 > {verdict_cfg.trim_caution_loss_pct}% 或 Macro 强 risk_off + Risk high_risk 双触发时，才考虑 TRIM\n"
            f"- 金融逻辑：零花钱账户 + 强破产兜底，小额浮亏不值得交易"
        )

    return load_skill(
        "cio",
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
        TRIM_CONSTRAINT=trim_constraint,
    )


__all__ = ["build_cio_prompt"]
