"""市场 Regime 分类 — Single Source of Truth (Layer 2b)

市场状态判别的唯一来源。所有需要"现在是上涨/下跌/震荡/崩盘"判断的代码
都必须 import 这个模块，**禁止重复实现阈值或分类逻辑**。

为什么需要这个模块（设计动机）:
- 老 committee 的 Quant Round 1 prompt 让 LLM 自己判断 SIGNAL，缺少"市场状态"上下文，
  常常在区间震荡市底部输出 bearish（看到价格刚跌就喊弱），错过逢低买点。
- 老 dreaming.py 自己定义了 vix/tnx 二维 regime，跟 committee 完全脱节，
  长期模式跟短期决策互不通气。
- 本模块统一两边的 regime 语言，让 dreaming 提炼的 "uptrend 时买金赚钱率 80%"
  能被 committee 直接消费。

接入方:
- agents/quant.py（committee Round 1）：把 regime 当事实塞进 prompt
- jobs/dreaming.py（REM sleep）：聚合时按 regime 分桶
- 任何下游想加新阈值 → 改这里的 THRESHOLDS，不要在自己代码里硬编码
"""
from __future__ import annotations

from typing import Any, Dict, Literal

# Regime 类型枚举（任何下游引用必须用这个）
RegimeType = Literal["uptrend", "downtrend", "range_bound", "crash", "unknown"]
ALL_REGIMES = ("uptrend", "downtrend", "range_bound", "crash", "unknown")

# 阈值常量 — 唯一权威，改这里要同步改 tests/test_regime.py
THRESHOLDS: Dict[str, float] = {
    # MA20 vs MA120 偏离 ≥ 3% 视为有趋势（MA120 比 MA60 慢，所以阈值高一点）
    "trend_ma_spread_pct": 3.0,
    # 14 日 ATR 占当前价 ≥ 5% 视为崩盘期（异常高波动）
    "crash_atr_pct_min": 5.0,
    # 价格 2 年分位 ≤ 20% 视为"低位"，配合 range_bound 触发"震荡市底部"提示
    "low_quantile_threshold": 0.20,
    # 价格 2 年分位 ≥ 80% 视为"高位"
    "high_quantile_threshold": 0.80,
}


def classify_regime(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """根据 market_metrics.compute_metrics 的输出判定 regime。

    Args:
        metrics: utils.market_metrics.compute_metrics 返回的 dict
                 必需字段: ma20, ma120, atr_pct, price_quantile_2y

    Returns:
        {
          "regime":      RegimeType,
          "reason":      str — 人话解释（给 LLM prompt 用）
          "inputs_used": dict — 实际用到的字段值，方便 audit 时回溯
        }

    判定优先级:
        1. crash      — atr_pct ≥ THRESHOLDS["crash_atr_pct_min"]（最高优先级）
        2. uptrend    — (ma20 - ma120) / ma120 ≥ +trend_ma_spread_pct%
        3. downtrend  — (ma20 - ma120) / ma120 ≤ -trend_ma_spread_pct%
        4. range_bound — 默认（MA 纠缠 + 波动正常）
        5. unknown    — 缺关键数据
    """
    ma20 = metrics.get("ma20")
    ma120 = metrics.get("ma120")
    atr_pct = metrics.get("atr_pct")
    quantile = metrics.get("price_quantile_2y")

    # 透明 audit：把判断用的输入回写
    inputs_used = {
        "ma20": ma20,
        "ma120": ma120,
        "atr_pct": atr_pct,
        "price_quantile_2y": quantile,
    }

    # 缺数据 → unknown，让下游决定怎么处理（保守起见 = 不动）
    if any(v is None for v in (ma20, ma120, atr_pct)):
        return {
            "regime": "unknown",
            "reason": "缺少必要指标（ma20/ma120/atr_pct 至少一项为 None）",
            "inputs_used": inputs_used,
        }

    # 1. Crash 优先：异常高波动期间不管趋势先标 crash
    if atr_pct >= THRESHOLDS["crash_atr_pct_min"]:
        return {
            "regime": "crash",
            "reason": f"ATR {atr_pct:.2f}% ≥ {THRESHOLDS['crash_atr_pct_min']}% 极高波动",
            "inputs_used": inputs_used,
        }

    # 2 + 3. 趋势判断：MA20 vs MA120 偏离度
    if ma120 == 0:
        # 防御：理论上不会发生（价格 = 0 不可能）
        return {
            "regime": "unknown",
            "reason": "ma120 = 0，无法计算 spread",
            "inputs_used": inputs_used,
        }
    ma_spread_pct = (ma20 - ma120) / ma120 * 100

    if ma_spread_pct >= THRESHOLDS["trend_ma_spread_pct"]:
        return {
            "regime": "uptrend",
            "reason": f"MA20 高于 MA120 {ma_spread_pct:+.2f}%（≥ {THRESHOLDS['trend_ma_spread_pct']}%）",
            "inputs_used": inputs_used,
        }
    if ma_spread_pct <= -THRESHOLDS["trend_ma_spread_pct"]:
        return {
            "regime": "downtrend",
            "reason": f"MA20 低于 MA120 {ma_spread_pct:+.2f}%（≤ -{THRESHOLDS['trend_ma_spread_pct']}%）",
            "inputs_used": inputs_used,
        }

    # 4. 默认震荡
    return {
        "regime": "range_bound",
        "reason": f"MA 纠缠 ({ma_spread_pct:+.2f}%) 且 ATR {atr_pct:.2f}% 正常，无明确趋势",
        "inputs_used": inputs_used,
    }


def regime_strategy_hint(regime: RegimeType, price_quantile_2y: float | None) -> str:
    """把 regime 翻译成给 Quant prompt 用的策略提示。

    Quant 角色看到这个提示后，必须遵循对应的方向偏好。
    特别是 range_bound + 低分位的情况，硬规则要求 Quant 不允许在 Round 2
    被 Risk Officer 逼着改 SIGNAL（这是老系统的核心 bug）。
    """
    if regime == "uptrend":
        return "顺势 — 回调可加，止损放宽到趋势线下方，不要被短期回调吓出去"
    if regime == "downtrend":
        return "止损 — 不抄底，等趋势确认反转（MA20 重新站上 MA120）后再考虑"
    if regime == "range_bound":
        if price_quantile_2y is None:
            return "震荡市 — 高抛低吸，避免追涨杀跌"
        if price_quantile_2y <= THRESHOLDS["low_quantile_threshold"]:
            return (
                "震荡市底部 — 当前 2 年分位 "
                f"{price_quantile_2y * 100:.0f}% (≤ {THRESHOLDS['low_quantile_threshold'] * 100:.0f}%)，"
                "逢低分批是首选；禁止因 Risk 集中度警告就在底部止损卖飞"
            )
        if price_quantile_2y >= THRESHOLDS["high_quantile_threshold"]:
            return (
                "震荡市顶部 — 当前 2 年分位 "
                f"{price_quantile_2y * 100:.0f}% (≥ {THRESHOLDS['high_quantile_threshold'] * 100:.0f}%)，"
                "逢高减仓，落袋为安"
            )
        return f"震荡市中段 — 2 年分位 {price_quantile_2y * 100:.0f}%，无明显边缘信号，建议不动"
    if regime == "crash":
        return "崩盘 — 离场观望，等波动率回归正常 (ATR < 3%) 再说"
    return "状态未知 — 数据不足，维持原计划"


def format_regime_brief(metrics: Dict[str, Any]) -> str:
    """从 metrics dict 生成给 Quant prompt 用的 REGIME 上下文片段。

    返回形如：
      ```
      REGIME: range_bound
      REASON: MA 纠缠 (+0.45%) 且 ATR 1.20% 正常，无明确趋势
      INPUTS: ma20=4625.30, ma120=4604.50, atr_pct=1.20, price_quantile_2y=0.05
      STRATEGY_HINT: 震荡市底部 — 当前 2 年分位 5% (≤ 20%)，逢低分批是首选...
      ```

    skill.py / daily_report.py 都用这个函数生成 brief，然后塞进 quant_input 字符串。
    任何想自己拼 regime 上下文的代码 → 改这个函数，别在外头另拼一份。
    """
    classification = classify_regime(metrics)
    regime = classification["regime"]
    reason = classification["reason"]
    inputs = classification["inputs_used"]
    hint = regime_strategy_hint(regime, metrics.get("price_quantile_2y"))

    inputs_str = ", ".join(
        f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
        for k, v in inputs.items()
    )

    return (
        f"REGIME: {regime}\n"
        f"REASON: {reason}\n"
        f"INPUTS: {inputs_str}\n"
        f"STRATEGY_HINT: {hint}"
    )


__all__ = [
    "RegimeType",
    "ALL_REGIMES",
    "THRESHOLDS",
    "classify_regime",
    "regime_strategy_hint",
    "format_regime_brief",
]
