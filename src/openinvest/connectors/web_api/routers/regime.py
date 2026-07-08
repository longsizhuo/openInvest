"""regime 路由 — 从 system.py 按域拆分（行为不变）。

市场 regime 端点：实时分类指定 symbol 的 regime + 暴露全部硬规则/提示词给 GUI。
所有 @router.get path 逐字搬运（含 /api/regime/{symbol:path} 的 :path 转换器），
行为零漂移。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from openinvest.utils.exchange_fee import get_history_data

from openinvest.connectors.web_api.models import (
    AgentPromptInfo,
    RegimeResponse,
    RegimeRulesResponse,
)

log = logging.getLogger("web_api")

router = APIRouter()


@router.get("/api/regime/{symbol:path}", response_model=RegimeResponse, tags=["system"])
async def get_regime(symbol: str) -> RegimeResponse:
    """实时算指定 symbol 的市场 regime（牛/熊/震荡）+ 给 LLM 看的 brief"""
    from openinvest.core.regime import classify_regime, regime_strategy_hint, format_regime_brief
    from openinvest.utils.market_metrics import compute_metrics

    try:
        df = get_history_data(symbol, "2y")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"行情拉取失败: {e}")
    if df is None or df.empty:
        raise HTTPException(404, f"无 {symbol} 行情数据")

    metrics = compute_metrics(df)
    info = classify_regime(metrics)
    regime_label = info.get("regime", "unknown")
    reason = info.get("reason", "")
    hint = regime_strategy_hint(regime_label, metrics.get("price_quantile_2y"))
    brief = format_regime_brief(metrics)

    # 把 numpy / pandas 类型转成 JSON-safe
    inputs_safe = {}
    for k, v in metrics.items():
        try:
            if v is None:
                inputs_safe[k] = None
            elif hasattr(v, "item"):
                inputs_safe[k] = v.item()
            else:
                inputs_safe[k] = v
        except Exception:
            inputs_safe[k] = str(v)

    return RegimeResponse(
        symbol=symbol,
        regime=regime_label,
        reason=reason,
        inputs=inputs_safe,
        strategy_hint=hint,
        brief=brief,
    )


@router.get("/api/regime_rules", response_model=RegimeRulesResponse, tags=["system"])
async def get_regime_rules() -> RegimeRulesResponse:
    """暴露 invest 项目所有「硬规则」+「LLM 提示词」给 GUI marketing 页

    包含：
    - core/regime.py 阈值表
    - 4 个 agent 角色的 system prompt 全文
    - CIO sanity check 清单
    - 5 个可被 LLM 调用的 tool
    """
    from openinvest.core.regime import get_thresholds
    from openinvest.capabilities.committee.macro_strategist import build_macro_strategist_prompt
    from openinvest.capabilities.committee.quant import build_quant_prompt
    from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.tools import TOOL_DEFINITIONS

    sample_asset = {
        "symbol": "<SYMBOL>",
        "display_name": "<asset name>",
    }

    agents_info = [
        AgentPromptInfo(
            role="macro",
            label="宏观分析师 (Macro Strategist)",
            description="跨资产共享，每次 daily_report 跑一次。评估全球利率/通胀/地缘风险。",
            prompt_full=build_macro_strategist_prompt(),
            temperature=0.2,
            enable_tools=True,
            notes=[
                "强制输出: SIGNAL (risk_on/risk_off/neutral) + STRENGTH 0-10 + SCORE -5~+5",
                "Hard rule: SCORE<-2 → risk_off / SCORE>2 → risk_on",
            ],
        ),
        AgentPromptInfo(
            role="quant",
            label="量化分析师 (Quant Analyst)",
            description="技术信号（RSI/MA/分位数），受 REGIME 硬约束保护",
            prompt_opening=build_quant_prompt(sample_asset, "opening"),
            prompt_rebuttal=build_quant_prompt(sample_asset, "rebuttal"),
            temperature=0.2,
            enable_tools=False,
            notes=[
                "REGIME=uptrend → 禁 bearish",
                "REGIME=downtrend → 禁 bullish",
                "REGIME=range_bound + price_quantile≤20% → 偏 bullish（底部逢低）",
                "REGIME=range_bound + price_quantile≥80% → 偏 bearish（顶部减仓）",
                "REGIME=crash → 强制 neutral（任何方向都不可执行）",
            ],
        ),
        AgentPromptInfo(
            role="risk",
            label="风险官 (Risk Officer)",
            description="集中度 / dry_powder / 压力测试",
            prompt_opening=build_risk_officer_prompt(sample_asset, "opening"),
            prompt_rebuttal=build_risk_officer_prompt(sample_asset, "rebuttal"),
            temperature=0.2,
            enable_tools=False,
            notes=[
                "Hard rule: 集中度 > 60% → 至少 concerned",
                "Hard rule: dry_powder < 1000 CNY → concerned（无加仓能力）",
                "Hard rule: 7 天内多次买同资产 → high_risk（情绪化追涨）",
            ],
        ),
        AgentPromptInfo(
            role="cio",
            label="CIO 决策者",
            description="综合 Macro + Quant + Risk 三方意见，给出最终 verdict",
            prompt_full=build_cio_prompt(sample_asset),
            temperature=0.1,    # 更保守
            enable_tools=False,
            notes=[
                "5 个 verdict: BUY / ACCUMULATE / HOLD / TRIM / SELL",
                "Sanity: confidence≥0.95+BUY → 自动降级 ACCUMULATE",
                "Sanity: |alloc|>100k → clamp 到 ±100k",
                "Sanity: 输入含 [WORKER_UNAVAILABLE] → 强制 HOLD + confidence≤0.4",
            ],
        ),
    ]

    return RegimeRulesResponse(
        regime_thresholds=get_thresholds(),
        regime_types=["crash", "uptrend", "downtrend", "range_bound", "unknown"],
        regime_priority=[
            "1. crash (ATR% ≥ 5% → 任何方向都强制 neutral)",
            "2. uptrend (MA20 vs MA120 偏离 ≥ +3%)",
            "3. downtrend (MA20 vs MA120 偏离 ≤ -3%)",
            "4. range_bound (其他)",
            "5. unknown (数据不足 → 维持原计划)",
        ],
        verdict_options=["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"],
        sanity_checks=[
            "confidence≥0.95 + verdict=BUY → 自动降级到 ACCUMULATE(0.6)",
            "|SUGGESTED_ALLOC_CNY| > 100,000 → clamp 到 ±100,000",
            "输入含 [WORKER_UNAVAILABLE] → 强制 HOLD + confidence ≤ 0.4",
            "Risk=high_risk → 即便 Quant+Macro bullish 也最多 ACCUMULATE，禁 BUY",
            "CONCENTRATION > 60% → 任何加仓 ≤ dry_powder × 10%",
        ],
        agents=agents_info,
        tools=list(TOOL_DEFINITIONS),
    )
