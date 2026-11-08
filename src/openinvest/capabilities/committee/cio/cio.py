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
from openinvest.capabilities.loader import apply_lens_blocks, load_skill


# DeepSeek JSON Output 模式追加段——覆盖 SKILL.md 的 VERDICT: 文本格式，要求吐单个
# JSON 对象。字段名与 cio_parse.parse_cio_memo 的 fields 抽取一一对应；额外 memo 字段
# 承载完整 prose（GUI/transcript 展示用，不丢分析）。必含 "json" 字样（DeepSeek 要求）。
def _json_output_addendum() -> str:
    from openinvest.utils.base_currency import base_currency
    # 金额字段名带基准币种（suggested_alloc_cny / _aud …），cio_parse 接受任意三字母币种
    return ("\n\n" + bilingual(
        """=== 输出格式覆盖（本次仅此一种）===
忽略上方 `VERDICT: / CONFIDENCE: ...` 文本格式。只输出**一个 JSON 对象**（不要 markdown 代码围栏、不要任何额外文字），字段如下：
{
  "verdict": "BUY|ACCUMULATE|HOLD|TRIM|SELL",
  "confidence": 0.0~1.0 的数字,
  "dominant_view": "quant|macro|risk",
  "suggested_alloc_cny": 整数金额（SELL/TRIM 用负数表示减仓）,
  "trim_reason": "concentration|stop_loss|bearish"（非 TRIM 用 null）,
  "exit_intent": "risk_reduction|thesis_invalidated|liquidity_need|tactical_reentry" 或 null,
  "hold_reason": "maintain_position|wait_for_edge|user_constraint|data_untrusted" 或 null,
  "market_assessment": {"direction": "bullish|bearish|uncertain|unknown", "horizon": "判断期限或未知", "support": "本次市场支持证据", "counterevidence": "实质反证及局限", "execution_data_gaps": "只列执行资料缺口，不作为市场看空证据"},
  "alternatives": 数组；必须分别列 HOLD_EXISTING、ADD、REDUCE_RISK、TACTICAL_REENTRY，可增加 THESIS_EXIT、LIQUIDITY_EXIT；每项含 kind、applicability（applicable|not_applicable|unknown，不偏好不等于不适用）、market_evaluation（favorable|unfavorable|mixed|unknown）、exit_intent、support、counterevidence、constraint_refs、assumptions、data_gaps；不能合并风险退出与择时买回,
  "review_plan": 按上述结构给出条件、单位、带时区有效期和变更理由的对象，或 null,
  "risk_review_plan": 独立的下行风险复核计划，同 review_plan 结构，或 null；不得只在 memo 中承诺监控,
  "reentry_price": 择时买回型 TRIM 用低于现价的数字，资产报价币种、与现价同单位；stop_loss / concentration 或 risk_reduction / thesis_invalidated / liquidity_need 退出或非 TRIM 可用 null,
  "reentry_condition": 择时买回条件；stop_loss / concentration 或 risk_reduction / thesis_invalidated / liquidity_need 退出或非 TRIM 可用 null,
  "expected_path": 择时买回型 TRIM 的路径依据；风险/需求退出或非 TRIM 可用 null,
  "memo": "你完整的投行级中文分析备忘，多段，与平时文本 memo 同等详尽"
}
上方所有规则照旧（sanity / TRIM 约束 / 集中度 / 看到完整 [WORKER_UNAVAILABLE] reason= 失败标记则 verdict=HOLD 且 confidence≤0.4），只是承载在 JSON 字段里。""",
        """=== Output format override (this is the only format for this call) ===
Ignore the `VERDICT: / CONFIDENCE: ...` text format above. Output **a single JSON object only** (no markdown code fences, no extra text), with the following fields:
{
  "verdict": "BUY|ACCUMULATE|HOLD|TRIM|SELL",
  "confidence": a number between 0.0 and 1.0,
  "dominant_view": "quant|macro|risk",
  "suggested_alloc_cny": integer amount (negative for SELL/TRIM to indicate a reduction),
  "trim_reason": "concentration|stop_loss|bearish" (null when not TRIM),
  "exit_intent": "risk_reduction|thesis_invalidated|liquidity_need|tactical_reentry" or null,
  "hold_reason": "maintain_position|wait_for_edge|user_constraint|data_untrusted" or null,
  "market_assessment": {"direction": "bullish|bearish|uncertain|unknown", "horizon": "horizon or unknown", "support": "market support", "counterevidence": "material counterevidence and limitations", "execution_data_gaps": "execution gaps, not bearish market evidence"},
  "alternatives": an array with separate HOLD_EXISTING, ADD, REDUCE_RISK and TACTICAL_REENTRY entries; optional THESIS_EXIT and LIQUIDITY_EXIT. Each includes kind, applicability (applicable|not_applicable|unknown; not preferred is not inapplicable), market_evaluation (favorable|unfavorable|mixed|unknown), exit_intent, support, counterevidence, constraint_refs, assumptions and data_gaps. Risk reduction and tactical reentry must not be merged,
  "review_plan": conditions, units, timezone-aware expiry and explicit replacement reason using the structure above, or null,
  "risk_review_plan": a separate downside-risk review plan using the same structure, or null; monitoring promised in prose must have structured conditions,
  "reentry_price": number below the current price only for a tactical round-trip TRIM, in the asset's quote currency and unit; null is allowed for stop_loss / concentration or risk_reduction / thesis_invalidated / liquidity_need exits and non-TRIM verdicts,
  "reentry_condition": tactical reentry condition; null is allowed for stop_loss / concentration or risk_reduction / thesis_invalidated / liquidity_need exits and non-TRIM verdicts,
  "expected_path": path evidence for tactical reentry; null is allowed for risk/needs exits and non-TRIM,
  "memo": "your full investment-bank-grade analysis memo, multi-paragraph, matching the depth of the plain-text memo"
}
All rules above still apply (sanity checks / TRIM constraints / concentration / verdict=HOLD with confidence<=0.4 when the complete [WORKER_UNAVAILABLE] reason= failure marker appears) -- they are just carried in the JSON fields.""",
    )).replace("suggested_alloc_cny", f"suggested_alloc_{base_currency().lower()}")


def build_cio_prompt(asset: Dict[str, Any], json_mode: bool = False) -> str:
    """渲染 CIO prompt（含 asset 占位符 + config 阈值条件注入）。

    json_mode=True 追加 JSON 输出段（DeepSeek JSON Output，门控见 utils.llm.supports_json_output）。
    """
    from openinvest.core.config import load_config
    asset_name = asset.get("display_name", asset.get("symbol"))
    verdict_cfg = load_config().verdict

    # Historical P&L bands are retained in configuration/audit snapshots, not
    # injected as investment rules. A configured number is not user authority.
    trim_constraint = ""

    # 集中度 lens 关闭时（单资产/刻意集中策略）压掉 CIO 的超配规则。空串=开启时零改动
    # （str.replace 空串为 no-op）。这是 prompt 软层；硬兜底在 cio_parse.py Sanity 4。
    concentration_directive = ""
    if not verdict_cfg.concentration_lens_enabled:
        concentration_directive = bilingual(
            "**集中度分析当前未启用（配置状态）**：输入可能只有部分资产，缺少完整总资产分母，"
            "不得编造 CONCENTRATION_PCT、据此断言超配或新增持仓比例限制。"
            "这不证明用户选择了单资产或刻意集中策略，也不是用户禁止减仓。"
            "仍须独立评估当前敞口的波动、回撤、投资依据、宏观与流动性风险。",
            "**Concentration analysis is currently disabled (configuration state)**: the input may cover only part of the assets, "
            "so do not invent a total-portfolio denominator, CONCENTRATION_PCT, overweight conclusion or position-share limit. "
            "This does not establish that the user chose a single-asset or deliberately concentrated strategy, and is not a user ban on reducing exposure. "
            "Independently evaluate volatility, drawdown, investment thesis, macro and liquidity risk.",
        )

    # 现金仓位机会成本规则关闭时（默认）压掉"低集中度不许 HOLD、默认至少 ACCUMULATE"。
    # 空串=开启时零改动。纯 prompt 软层（无确定性后处理强制 ACCUMULATE，所以不需要硬兜底）。
    cash_opp_cost_directive = ""
    if not verdict_cfg.cash_opportunity_cost_rule_enabled:
        cash_opp_cost_directive = bilingual(
            "**现金比例强制动作规则已关闭**：不得仅因现金充足而强制加仓，也不得把 HOLD 当成默认正确。"
            "分别比较买入、持有和退出的证据、反证与用户约束，再给出裁决。",
            "**Cash-ratio action forcing is disabled**: sufficient cash does not force accumulation, and HOLD is not presumed correct. "
            "Compare evidence, counterevidence and user constraints for buying, holding and exiting.",
        )

    prompt = load_skill(
        "cio",
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
        TRIM_CONSTRAINT=trim_constraint,
        CONCENTRATION_DIRECTIVE=concentration_directive,
        CASH_OPP_COST_DIRECTIVE=cash_opp_cost_directive,
        ACCUMULATE_DEFINITION=("分批建仓 / 加仓，比较已配置的现金运用偏好，仍须本次证据和真实预算"
                               if verdict_cfg.cash_opportunity_cost_rule_enabled else "分批建仓 / 加仓，须有本次证据和预算依据"),
        HOLD_DEFINITION=("维持现状，说明本次证据与现金运用偏好的权衡；配置不构成强制买入" if verdict_cfg.cash_opportunity_cost_rule_enabled
                         else "维持现状，可在任意仓位选择，但须说明为何优于买入或退出"),
    )
    prompt = apply_lens_blocks(prompt, "concentration", verdict_cfg.concentration_lens_enabled)
    prompt = apply_lens_blocks(prompt, "cash_opportunity", verdict_cfg.cash_opportunity_cost_rule_enabled)
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
