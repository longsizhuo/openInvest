"""CIO - 综合 Quant / Macro / Risk Officer 三人输出，给最终客户备忘

CIO 不重做分析，只综合 + 决策 + 输出执行方案。
强制读三方的 SIGNAL/ONE_LINER + 用户上下文，给完整的投行级 memo。

⚠️ Prompt 本体在 `capabilities/committee/cio/cio.md`。

2026-05-18: DSPy v2 few-shot demos 经 sandbox A/B + MIPROv2 train 双重验证后**不接入**
production——zero-shot baseline 已接近天花板 (67% verdict accuracy)，random demos
压制 reasoning quality (REGIME mention 64%→1.8%)，DSPy optimized 也 0pp improvement。
few-shot 注入路线已退役（ADR-007，v1-v4 均输 zero-shot；loader 删于 2026-07-05 #141）。
未来若做校准目标（Brier）的 prompt 优化，是新实验新接线——门槛与判据见 issue #141。
"""
from typing import Any, Dict

from openinvest.capabilities.committee.i18n import (
    bilingual,
    build_field_value_language_directive,
    build_output_language_directive,
    get_invest_lang,
    localize_prompt_output_requirements,
)
from openinvest.capabilities.loader import load_skill


# DeepSeek JSON Output 模式追加段——覆盖 SKILL.md 的 VERDICT: 文本格式，要求吐单个
# JSON 对象。字段名与 cio_parse.parse_cio_memo 的 fields 抽取一一对应；额外 memo 字段
# 承载完整 prose（GUI/transcript 展示用，不丢分析）。必含 "json" 字样（DeepSeek 要求）。
def _json_output_addendum() -> str:
    return "\n\n" + bilingual(
        """=== 输出格式覆盖（本次仅此一种）===
忽略上方 `VERDICT: / CONFIDENCE: ...` 文本格式。只输出**一个 JSON 对象**（不要 markdown 代码围栏、不要任何额外文字），字段如下：
{
  "verdict": "BUY|ACCUMULATE|HOLD|TRIM|SELL",
  "confidence": 0.0~1.0 的数字,
  "dominant_view": "quant|macro|risk",
  "suggested_alloc_cny": 整数金额（SELL/TRIM 用负数表示减仓）,
  "trim_reason": "concentration|stop_loss|bearish"（非 TRIM 用 null）,
  "reentry_price": 数字（非 TRIM 用 null，TRIM 必须低于现价）,
  "reentry_condition": 字符串（非 TRIM 用 null）,
  "expected_path": 字符串（非 TRIM 用 null）,
  "memo": "你完整的投行级中文分析备忘，多段，与平时文本 memo 同等详尽"
}
上方所有规则照旧（sanity / TRIM 约束 / 集中度 / 看到 [WORKER_UNAVAILABLE] 则 verdict=HOLD 且 confidence≤0.4），只是承载在 JSON 字段里。""",
        """=== Output format override (this is the only format for this call) ===
Ignore the `VERDICT: / CONFIDENCE: ...` text format above. Output **a single JSON object only** (no markdown code fences, no extra text), with the following fields:
{
  "verdict": "BUY|ACCUMULATE|HOLD|TRIM|SELL",
  "confidence": a number between 0.0 and 1.0,
  "dominant_view": "quant|macro|risk",
  "suggested_alloc_cny": integer amount (negative for SELL/TRIM to indicate a reduction),
  "trim_reason": "concentration|stop_loss|bearish" (null when not TRIM),
  "reentry_price": number (null when not TRIM; must be below the current price for TRIM),
  "reentry_condition": string (null when not TRIM),
  "expected_path": string (null when not TRIM),
  "memo": "your full investment-bank-grade analysis memo, multi-paragraph, matching the depth of the plain-text memo"
}
All rules above still apply (sanity checks / TRIM constraints / concentration / verdict=HOLD with confidence<=0.4 when [WORKER_UNAVAILABLE] appears) -- they are just carried in the JSON fields.""",
    )


def build_cio_prompt(asset: Dict[str, Any], json_mode: bool = False) -> str:
    """渲染 CIO prompt（含 asset 占位符 + config 阈值条件注入）。

    json_mode=True 追加 JSON 输出段（DeepSeek JSON Output，门控见 utils.llm.supports_json_output）。
    """
    from openinvest.core.config import load_config
    asset_name = asset.get("display_name", asset.get("symbol"))
    verdict_cfg = load_config().verdict

    # TRIM 约束：阈值 > 0 时注入（sweep 出 OOS 验证结果后才启用，遵守 ADR-010 rule 4）
    # 用户显式开启（配 trim_*_loss_pct）= 采用"小额浮亏不 TRIM"纪律，覆盖通用 TRIM 规则。
    trim_constraint = ""
    if verdict_cfg.trim_no_trim_loss_pct > 0 and verdict_cfg.trim_caution_loss_pct > 0:
        trim_constraint = bilingual(
            f"**🔥 小额浮亏 TRIM 约束（用户已启用，强制，覆盖通用 TRIM 规则）**：\n"
            f"- **浮亏 < {verdict_cfg.trim_no_trim_loss_pct}% 不允许 TRIM** — 卖出坐实亏损，长期资金久期下应 HOLD 等修复\n"
            f"- **浮亏 {verdict_cfg.trim_no_trim_loss_pct}-{verdict_cfg.trim_caution_loss_pct}% 且 Macro SIGNAL 非 risk_off：倾向 HOLD** — 短期波动不是卖出理由\n"
            f"- 只有浮亏 > {verdict_cfg.trim_caution_loss_pct}% 或 Macro SIGNAL=risk_off + Risk SIGNAL=high_risk 双触发时，才考虑 TRIM\n"
            f"- 金融逻辑：长期投资账户，小额浮亏不值得交易",
            f"**🔥 Small-unrealized-loss TRIM constraint (enabled by the user, mandatory, overrides the general TRIM rule)**:\n"
            f"- **Unrealized loss < {verdict_cfg.trim_no_trim_loss_pct}%: TRIM is not allowed** -- selling locks in the loss; with a long capital horizon, HOLD and wait for recovery\n"
            f"- **Unrealized loss {verdict_cfg.trim_no_trim_loss_pct}-{verdict_cfg.trim_caution_loss_pct}% and Macro SIGNAL is not risk_off: lean HOLD** -- short-term volatility is not a reason to sell\n"
            f"- Only consider TRIM when unrealized loss > {verdict_cfg.trim_caution_loss_pct}%, or Macro SIGNAL=risk_off together with Risk SIGNAL=high_risk\n"
            f"- Rationale: long-term investment account -- a small unrealized loss is not worth trading over",
        )

    # 集中度 lens 关闭时（单资产/刻意集中策略）压掉 CIO 的超配规则。空串=开启时零改动
    # （str.replace 空串为 no-op）。这是 prompt 软层；硬兜底在 cio_parse.py Sanity 4。
    concentration_directive = ""
    if not verdict_cfg.concentration_lens_enabled:
        concentration_directive = bilingual(
            "**🚫 集中度 lens 已被用户关闭（单资产 / 刻意集中策略）**：忽略上方所有基于 "
            "CONCENTRATION_PCT 的超配规则——`<20% / 20-40% / >40%` 分档与 `>60% 限仓` 均不适用，"
            "**不得以集中度 / 超配为由输出 TRIM**（也不得换标签成 bearish 但实由超配驱动）。"
            "仍须正常评估波动 / 回撤 / 止损 / 宏观 / 估值风险。",
            "**🚫 The concentration lens has been disabled by the user (single-asset / deliberately concentrated strategy)**: ignore all "
            "CONCENTRATION_PCT-based overweight rules above -- the `<20% / 20-40% / >40%` tiers and the `>60% cap` no longer apply. "
            "**Do not output TRIM on grounds of concentration / overweight** (and do not relabel it as bearish while it is actually driven by concentration). "
            "Still evaluate volatility / drawdown / stop-loss / macro / valuation risk normally.",
        )

    # 现金仓位机会成本规则关闭时（默认）压掉"低集中度不许 HOLD、默认至少 ACCUMULATE"。
    # 空串=开启时零改动。纯 prompt 软层（无确定性后处理强制 ACCUMULATE，所以不需要硬兜底）。
    cash_opp_cost_directive = ""
    if not verdict_cfg.cash_opportunity_cost_rule_enabled:
        cash_opp_cost_directive = bilingual(
            "**🚫 现金仓位机会成本规则已被用户关闭**：忽略上方整段「现金仓位机会成本规则」——"
            "`HOLD` 在**任何仓位 / 任何现金比例**都是合法 default，**不得**仅因 CONCENTRATION_PCT 低 / "
            "子弹充足就强制 `ACCUMULATE` 或禁止 `HOLD`。是否加仓纯按 Quant/Macro/Risk 信号 + 估值 / "
            "趋势证据决定。下方 Verdict 选项里「ACCUMULATE=100% 现金时的 default」与「HOLD 只在 20%+ "
            "时合法」同样作废。",
            "**🚫 The cash-opportunity-cost rule has been disabled by the user**: ignore the entire \"cash opportunity cost rule\" section above -- "
            "`HOLD` is a legal default at **any position size / any cash ratio**. **Do not** force `ACCUMULATE` or forbid `HOLD` just because "
            "CONCENTRATION_PCT is low or there is plenty of cash on hand. Whether to add to the position is decided purely by Quant/Macro/Risk signals "
            "plus valuation / trend evidence. Below, the Verdict options' \"ACCUMULATE = default when 100% cash\" and \"HOLD only legal at 20%+\" are likewise void.",
        )

    prompt = load_skill(
        "cio",
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
        TRIM_CONSTRAINT=trim_constraint,
        CONCENTRATION_DIRECTIVE=concentration_directive,
        CASH_OPP_COST_DIRECTIVE=cash_opp_cost_directive,
    )
    prompt = localize_prompt_output_requirements(prompt)
    contract = (
        "Start your first line exactly with `VERDICT:` and fill every required field in order. "
        "Do not write any prose before the structured fields."
        if get_invest_lang() == "en"
        else "第一行必须直接从 `VERDICT:` 开始，并按顺序填写所有必填字段。不要在结构化字段前写任何散文说明。"
    )
    prompt = (
        f"{build_output_language_directive(artifact='analysis memo', keep_verbatim_headers=True)}\n"
        f"{build_field_value_language_directive()}\n"
        f"{contract}\n\n{prompt}"
    )
    if json_mode:
        prompt += _json_output_addendum()
    return prompt


__all__ = ["build_cio_prompt"]
