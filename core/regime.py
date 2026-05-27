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

Per-asset 阈值（P1-2）:
- 不同资产波动结构不同：黄金 ATR ~1%、纳指 ~1.8%、加密 ~3%。
  统一用 5% 当 crash 触发线对黄金太松（永远不触发）；用 3% 当
  trend spread 阈值对黄金偏紧（黄金趋势更连贯）。
- ASSET_OVERRIDES 按 symbol 局部覆盖 THRESHOLDS。无 override 的资产
  fallback 到 default。改 default 影响所有资产；改 override 仅影响该资产。
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

# Regime 类型枚举（任何下游引用必须用这个）
RegimeType = Literal["uptrend", "downtrend", "range_bound", "crash", "recovery", "unknown"]
ALL_REGIMES = ("uptrend", "downtrend", "range_bound", "crash", "recovery", "unknown")

# 默认阈值 — 应用在所有未在 ASSET_OVERRIDES 中显式声明的资产上
THRESHOLDS: Dict[str, float] = {
    # MA20 vs MA120 偏离 ≥ 3% 视为有趋势（MA120 比 MA60 慢，所以阈值高一点）
    "trend_ma_spread_pct": 3.0,
    # crash 波动腿：14 日 ATR 占当前价 ≥ 5% 视为异常高波动
    "crash_atr_pct_min": 5.0,
    # crash 路径一（急跌）跌幅腿：过去 30 交易日累计跌幅 ≥ 20%（return_30d ≤ -0.20），
    # 与波动腿 AND 组合。
    "crash_drawdown_30d_pct": 20.0,
    # crash 路径二（深跌）：过去 30 交易日累计跌幅 ≥ 30%（return_30d ≤ -0.30），
    # **不看波动**单独触发。2026-05-26 双触发器：解决 AND 逻辑挡掉"慢阴跌"
    # （跌够但波动不剧烈）的问题 —— 深跌本身就是 crash，无需高 ATR 佐证。
    "crash_deep_drawdown_30d_pct": 30.0,
    # recovery：从近 30 日低点反弹 ≥ 10%（rebound_off_30d_low ≥ 0.10）
    "recovery_rebound_pct": 10.0,
    # recovery：且价格仍在低位（2y 真百分位 < 50%）
    "recovery_quantile_max": 0.50,
    # 价格 2 年分位 ≤ 20% 视为"低位"，配合 range_bound 触发"震荡市底部"提示
    "low_quantile_threshold": 0.20,
    # 价格 2 年分位 ≥ 80% 视为"高位"
    "high_quantile_threshold": 0.80,
}

# 单资产覆盖（per-asset overrides）—— P1-2
# 仅声明跟默认不同的 key；其余 key fallback 到 THRESHOLDS。
#
# 来源依据：
# - GC=F (黄金): 长期 ATR_pct 均值 ~1.0%，5% 永远不触发。改 3.5%
#               让真正的剧烈日内波动（俄乌、美联储意外）能被识别。
#               MA spread 放宽到 5% — 黄金趋势更连贯，3% 太敏感。
# - NDQ.AX (纳指 ETF): ATR_pct 均值 ~1.8%，5% crash 阈值合理保留。
#               MA spread 放宽到 4% — ETF 比单股更平滑。
# - BTC-USD (加密): ATR_pct 均值 ~3-5%，crash 阈值上调到 8% 防误报。
#
# 加新资产覆盖：直接往 dict 里加。schema 见 _per_asset_thresholds()。
ASSET_OVERRIDES: Dict[str, Dict[str, float]] = {
    "GC=F": {
        "trend_ma_spread_pct": 5.0,
        "crash_atr_pct_min": 3.5,
    },
    "NDQ.AX": {
        "trend_ma_spread_pct": 4.0,
    },
    "BTC-USD": {
        "trend_ma_spread_pct": 8.0,
        "crash_atr_pct_min": 8.0,
    },
    "ETH-USD": {
        "trend_ma_spread_pct": 8.0,
        "crash_atr_pct_min": 8.0,
    },
}


def _per_asset_thresholds(symbol: Optional[str]) -> Dict[str, float]:
    """把默认 THRESHOLDS 与该资产的 override 合并。

    没传 symbol 或 symbol 不在 ASSET_OVERRIDES → 完全用默认值。
    传了 → 用 override 字段覆盖默认值。
    """
    if not symbol:
        return dict(THRESHOLDS)
    override = ASSET_OVERRIDES.get(symbol, {})
    if not override:
        return dict(THRESHOLDS)
    merged = dict(THRESHOLDS)
    merged.update(override)
    return merged


def classify_regime(
    metrics: Dict[str, Any],
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """根据 market_metrics.compute_metrics 的输出判定 regime。

    Args:
        metrics: utils.market_metrics.compute_metrics 返回的 dict
                 必需字段: ma20, ma120, atr_pct, price_quantile_2y
        symbol: 资产 yfinance ticker（如 "GC=F"），传了则用 ASSET_OVERRIDES
                 里该 symbol 的阈值覆盖默认；不传 = 用默认 THRESHOLDS。

    Returns:
        {
          "regime":      RegimeType,
          "reason":      str — 人话解释（给 LLM prompt 用）
          "inputs_used": dict — 实际用到的字段值，方便 audit 时回溯
          "thresholds_used": dict — 本次判断用的阈值（per-asset 或默认）
        }

    判定优先级:
        1. crash      — 双触发器（满足任一即 crash，最高优先级）：
                        · 路径一急跌：atr_pct ≥ crash_atr_pct_min **且** return_30d ≤ -crash_drawdown_30d_pct%
                        · 路径二深跌：return_30d ≤ -crash_deep_drawdown_30d_pct%（不看波动）
        2. recovery   — crash 未触发 + 从近 30 日低点反弹 ≥ recovery_rebound_pct%
                        + 价格仍在低位（分位 < recovery_quantile_max）
        3. uptrend    — (ma20 - ma120) / ma120 ≥ +trend_ma_spread_pct%
        4. downtrend  — (ma20 - ma120) / ma120 ≤ -trend_ma_spread_pct%
        5. range_bound — 默认（MA 纠缠 + 波动正常）
        6. unknown    — 缺关键数据
    """
    thresholds = _per_asset_thresholds(symbol)
    ma20 = metrics.get("ma20")
    ma120 = metrics.get("ma120")
    atr_pct = metrics.get("atr_pct")
    quantile = metrics.get("price_quantile_2y")
    return_30d = metrics.get("return_30d")
    rebound = metrics.get("rebound_off_30d_low")

    # 透明 audit：把判断用的输入回写
    inputs_used = {
        "ma20": ma20,
        "ma120": ma120,
        "atr_pct": atr_pct,
        "price_quantile_2y": quantile,
        "return_30d": return_30d,
        "rebound_off_30d_low": rebound,
    }

    # 缺数据 → unknown，让下游决定怎么处理（保守起见 = 不动）
    if any(v is None for v in (ma20, ma120, atr_pct)):
        return {
            "regime": "unknown",
            "reason": "缺少必要指标（ma20/ma120/atr_pct 至少一项为 None）",
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }

    # 1. Crash 优先（双触发器，满足任一路径即 crash）：
    #    路径一（急跌）：波动腿 AND 跌幅腿（高 ATR + 30 日跌 ≥ 20%）
    #    路径二（深跌）：return_30d ≤ -30%，**不看波动**（慢阴跌也算 crash）
    #    跌幅相关腿都需要 return_30d；数据不足（None）时该腿视为不满足（保守）。
    vol_leg = atr_pct >= thresholds["crash_atr_pct_min"]
    drawdown_threshold = thresholds["crash_drawdown_30d_pct"] / 100.0
    deep_threshold = thresholds["crash_deep_drawdown_30d_pct"] / 100.0
    has_return = return_30d is not None
    drawdown_leg = has_return and return_30d <= -drawdown_threshold
    deep_leg = has_return and return_30d <= -deep_threshold
    if deep_leg:
        # 路径二优先描述（深跌本身已足够，无论波动）
        return {
            "regime": "crash",
            "reason": (
                f"深跌：30 日跌 {return_30d * 100:.1f}% ≤ "
                f"-{thresholds['crash_deep_drawdown_30d_pct']:.0f}%（不看波动单独触发）"
            ),
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }
    if vol_leg and drawdown_leg:
        return {
            "regime": "crash",
            "reason": (
                f"急跌：ATR {atr_pct:.2f}% ≥ {thresholds['crash_atr_pct_min']}% 高波动 "
                f"且 30 日跌 {return_30d * 100:.1f}% ≤ -{thresholds['crash_drawdown_30d_pct']:.0f}%"
            ),
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }

    # 2. Recovery：crash 已解除（上面双条件未同时满足）后，从低点反弹 + 仍在低位。
    #    无状态实现：用"已从近 30 日低点反弹 ≥ 10%"近似"刚从 crash 走出"，
    #    并要求分位 < 0.5 确保仍处低位（避免在健康高位牛市里误判 recovery）。
    rebound_threshold = thresholds["recovery_rebound_pct"] / 100.0
    quantile_max = thresholds["recovery_quantile_max"]
    if (rebound is not None and quantile is not None
            and rebound >= rebound_threshold
            and quantile < quantile_max):
        return {
            "regime": "recovery",
            "reason": (
                f"从近 30 日低点反弹 {rebound * 100:.1f}% "
                f"(≥ {thresholds['recovery_rebound_pct']:.0f}%) 且分位 {quantile * 100:.0f}% "
                f"(< {quantile_max * 100:.0f}%) 仍在低位"
            ),
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }

    # 3 + 4. 趋势判断：MA20 vs MA120 偏离度
    if ma120 == 0:
        # 防御：理论上不会发生（价格 = 0 不可能）
        return {
            "regime": "unknown",
            "reason": "ma120 = 0，无法计算 spread",
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }
    ma_spread_pct = (ma20 - ma120) / ma120 * 100

    if ma_spread_pct >= thresholds["trend_ma_spread_pct"]:
        return {
            "regime": "uptrend",
            "reason": (
                f"MA20 高于 MA120 {ma_spread_pct:+.2f}% "
                f"(≥ {thresholds['trend_ma_spread_pct']}%)"
            ),
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }
    if ma_spread_pct <= -thresholds["trend_ma_spread_pct"]:
        return {
            "regime": "downtrend",
            "reason": (
                f"MA20 低于 MA120 {ma_spread_pct:+.2f}% "
                f"(≤ -{thresholds['trend_ma_spread_pct']}%)"
            ),
            "inputs_used": inputs_used,
            "thresholds_used": thresholds,
        }

    # 5. 默认震荡
    return {
        "regime": "range_bound",
        "reason": f"MA 纠缠 ({ma_spread_pct:+.2f}%) 且 ATR {atr_pct:.2f}% 正常，无明确趋势",
        "inputs_used": inputs_used,
        "thresholds_used": thresholds,
    }


def regime_strategy_hint(
    regime: RegimeType,
    price_quantile_2y: float | None,
    symbol: Optional[str] = None,
) -> str:
    """把 regime 翻译成给 Quant prompt 用的策略提示。

    Quant 角色看到这个提示后，必须遵循对应的方向偏好。
    特别是 range_bound + 低分位的情况，硬规则要求 Quant 不允许在 Round 2
    被 Risk Officer 逼着改 SIGNAL（这是老系统的核心 bug）。

    symbol 只用来取 per-asset 的 quantile 阈值（高低位线）。
    """
    thresholds = _per_asset_thresholds(symbol)
    if regime == "uptrend":
        return "顺势 — 回调可加，止损放宽到趋势线下方，不要被短期回调吓出去"
    if regime == "downtrend":
        return "止损 — 不抄底，等趋势确认反转（MA20 重新站上 MA120）后再考虑"
    if regime == "range_bound":
        if price_quantile_2y is None:
            return "震荡市 — 高抛低吸，避免追涨杀跌"
        if price_quantile_2y <= thresholds["low_quantile_threshold"]:
            return (
                "震荡市底部 — 当前 2 年分位 "
                f"{price_quantile_2y * 100:.0f}% (≤ {thresholds['low_quantile_threshold'] * 100:.0f}%)，"
                "逢低分批是首选；禁止因 Risk 集中度警告就在底部止损卖飞"
            )
        if price_quantile_2y >= thresholds["high_quantile_threshold"]:
            return (
                "震荡市顶部 — 当前 2 年分位 "
                f"{price_quantile_2y * 100:.0f}% (≥ {thresholds['high_quantile_threshold'] * 100:.0f}%)，"
                "逢高减仓，落袋为安"
            )
        return f"震荡市中段 — 2 年分位 {price_quantile_2y * 100:.0f}%，无明显边缘信号，建议不动"
    if regime == "crash":
        return "崩盘 — 离场观望，等波动率回归正常 (ATR < 3%) 再说"
    if regime == "recovery":
        return (
            "复苏 — 从低位反弹，允许谨慎看多 (cautious bullish)：小仓试探、确认"
            "站稳再加，止损放在反弹起点下方；不要一把梭，反弹失败可能二次探底"
        )
    return "状态未知 — 数据不足，维持原计划"


def format_regime_brief(
    metrics: Dict[str, Any],
    symbol: Optional[str] = None,
) -> str:
    """从 metrics dict 生成给 Quant prompt 用的 REGIME 上下文片段。

    传 symbol 时使用 per-asset 阈值；不传 → 使用默认 THRESHOLDS。

    返回形如：
      ```
      REGIME: range_bound
      REASON: MA 纠缠 (+0.45%) 且 ATR 1.20% 正常，无明确趋势
      INPUTS: ma20=4625.30, ma120=4604.50, atr_pct=1.20, price_quantile_2y=0.05
      THRESHOLDS: trend_ma_spread_pct=5.00 (per-asset GC=F), crash_atr_pct_min=3.50
      STRATEGY_HINT: 震荡市底部 — 当前 2 年分位 5% (≤ 20%)，逢低分批是首选...
      ```

    skill.py / daily_report.py 都用这个函数生成 brief，然后塞进 quant_input 字符串。
    任何想自己拼 regime 上下文的代码 → 改这个函数，别在外头另拼一份。
    """
    classification = classify_regime(metrics, symbol=symbol)
    regime = classification["regime"]
    reason = classification["reason"]
    inputs = classification["inputs_used"]
    thresholds_used = classification["thresholds_used"]
    hint = regime_strategy_hint(
        regime, metrics.get("price_quantile_2y"), symbol=symbol,
    )

    inputs_str = ", ".join(
        f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
        for k, v in inputs.items()
    )

    # 标记是否用了 per-asset 覆盖，让 LLM 看到"这个阈值不是默认值"
    has_override = bool(symbol and ASSET_OVERRIDES.get(symbol))
    threshold_label = f" (per-asset {symbol})" if has_override else ""
    threshold_str = ", ".join(
        f"{k}={v:.2f}" for k, v in thresholds_used.items()
    )

    return (
        f"REGIME: {regime}\n"
        f"REASON: {reason}\n"
        f"INPUTS: {inputs_str}\n"
        f"THRESHOLDS{threshold_label}: {threshold_str}\n"
        f"STRATEGY_HINT: {hint}"
    )


__all__ = [
    "RegimeType",
    "ALL_REGIMES",
    "THRESHOLDS",
    "ASSET_OVERRIDES",
    "classify_regime",
    "regime_strategy_hint",
    "format_regime_brief",
]
