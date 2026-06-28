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

参数来源（Step 2 config 注入）:
- 阈值从 core.config.load_config().regime 读取
- Per-asset 覆盖从 core.config.load_config().regime_per_asset 读取
- THRESHOLDS / ASSET_OVERRIDES 模块级 dict 保留向后兼容（从 config 构建）
- sweep runner 用 set_config_override() 注入新参数，函数实时生效
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

# Regime 类型枚举（任何下游引用必须用这个）
RegimeType = Literal["uptrend", "downtrend", "range_bound", "crash", "recovery", "unknown"]
ALL_REGIMES = ("uptrend", "downtrend", "range_bound", "crash", "recovery", "unknown")


def _build_thresholds_from_config() -> Dict[str, float]:
    """从 config 构建 THRESHOLDS dict（排除 per_asset 子键）。"""
    from core.config import load_config
    cfg = load_config().regime
    return {
        "trend_ma_spread_pct": cfg.trend_ma_spread_pct,
        "crash_atr_pct_min": cfg.crash_atr_pct_min,
        "crash_drawdown_30d_pct": cfg.crash_drawdown_30d_pct,
        "crash_deep_drawdown_30d_pct": cfg.crash_deep_drawdown_30d_pct,
        "recovery_rebound_pct": cfg.recovery_rebound_pct,
        "recovery_quantile_max": cfg.recovery_quantile_max,
        "low_quantile_threshold": cfg.low_quantile_threshold,
        "high_quantile_threshold": cfg.high_quantile_threshold,
    }


def _build_asset_overrides_from_config() -> Dict[str, Dict[str, float]]:
    """从 config 构建 ASSET_OVERRIDES dict。"""
    from core.config import load_config
    per_asset = load_config().regime_per_asset
    result = {}
    for symbol, pa_cfg in per_asset.items():
        override = {}
        if pa_cfg.trend_ma_spread_pct is not None:
            override["trend_ma_spread_pct"] = pa_cfg.trend_ma_spread_pct
        if pa_cfg.crash_atr_pct_min is not None:
            override["crash_atr_pct_min"] = pa_cfg.crash_atr_pct_min
        if override:
            result[symbol] = override
    return result


# 向后兼容：旧代码 import THRESHOLDS / ASSET_OVERRIDES 仍可用
# 但推荐用 get_thresholds() / get_asset_overrides() 实时读取（set_config_override 后生效）
THRESHOLDS: Dict[str, float] = _build_thresholds_from_config()
ASSET_OVERRIDES: Dict[str, Dict[str, float]] = _build_asset_overrides_from_config()


def get_thresholds() -> Dict[str, float]:
    """实时从 config 读取 regime 阈值（set_config_override 后立即生效）。"""
    return _build_thresholds_from_config()


def get_asset_overrides() -> Dict[str, Dict[str, float]]:
    """实时从 config 读取 per-asset 覆盖（set_config_override 后立即生效）。"""
    return _build_asset_overrides_from_config()


def _per_asset_thresholds(symbol: Optional[str]) -> Dict[str, float]:
    """把默认阈值与该资产的 override 合并。

    每次调用从 config 实时读取，set_config_override() 注入后立即生效。
    没传 symbol 或 symbol 无 override → 完全用默认值。
    传了 → 用 override 字段覆盖默认值。
    """
    from core.config import load_config
    cfg = load_config()
    base = {
        "trend_ma_spread_pct": cfg.regime.trend_ma_spread_pct,
        "crash_atr_pct_min": cfg.regime.crash_atr_pct_min,
        "crash_drawdown_30d_pct": cfg.regime.crash_drawdown_30d_pct,
        "crash_deep_drawdown_30d_pct": cfg.regime.crash_deep_drawdown_30d_pct,
        "recovery_rebound_pct": cfg.regime.recovery_rebound_pct,
        "recovery_quantile_max": cfg.regime.recovery_quantile_max,
        "low_quantile_threshold": cfg.regime.low_quantile_threshold,
        "high_quantile_threshold": cfg.regime.high_quantile_threshold,
    }
    if not symbol:
        return base
    pa = cfg.regime_per_asset.get(symbol)
    if pa is None:
        return base
    if pa.trend_ma_spread_pct is not None:
        base["trend_ma_spread_pct"] = pa.trend_ma_spread_pct
    if pa.crash_atr_pct_min is not None:
        base["crash_atr_pct_min"] = pa.crash_atr_pct_min
    return base


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
    # atr_spike_ratio 不参与分类，但回写进 inputs_used → format_regime_brief 的
    # INPUTS 行 → coordinator transcript，让 atr_defense_from_text 两路径同源
    inputs_used = {
        # 未归一化绝对 MA → format_regime_brief INPUTS 行 → 记忆穿越指纹(ADR-022)。其余分位/spread 已相对,唯独这俩漏绝对量。
        "ma20": ma20,
        "ma120": ma120,
        "atr_pct": atr_pct,
        "atr_spike_ratio": metrics.get("atr_spike_ratio"),
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


def _regime_data_hint(
    price_quantile_2y: float | None,
    prob_hint: Optional[Dict[str, Any]],
) -> str:
    """uptrend / downtrend / range_bound / recovery 共用的中性提示：引用该 regime 的
    OHLC 概率口径 + 当前分位，方向判断交回 LLM + 数据，不给方向预设。"""
    q = (
        f"当前 2 年分位 {price_quantile_2y * 100:.0f}%；"
        if price_quantile_2y is not None else ""
    )
    if not prob_hint:
        # 调用方没传概率口径（OHLC 读失败 / 非 committee caller）→ 退化无数字版，
        # 仍中性、仍指向数据，不预设方向。
        return (
            f"{q}该 regime 的历史 30d forward return 分布（中位 / 跌破现价概率 / 样本数）"
            "见 brief 概率表口径。结合分位 / RSI / 浮亏自行判断方向，不预设方向"
        )
    med = prob_hint.get("median_pct")
    pb = prob_hint.get("p_below")
    n = prob_hint.get("n")
    eff = prob_hint.get("effective_n") or n
    overlap = f"（重叠窗口独立≈{eff}）" if (eff and n and eff != n) else ""
    return (
        f"该 regime 历史 30d forward return：中位 {med:+.1f}%、"
        f"跌破现价概率 {pb * 100:.0f}%、样本 n={n}{overlap}。"
        f"{q}结合分位 / RSI / 浮亏自行判断方向，不预设方向"
    )


def regime_strategy_hint(
    regime: RegimeType,
    price_quantile_2y: float | None,
    symbol: Optional[str] = None,
    *,
    prob_hint: Optional[Dict[str, Any]] = None,
) -> str:
    """把 regime 翻译成给 Quant prompt 用的 REGIME 上下文提示。

    2026-05-31（REGIME→方向链第一刀）：去掉人手写、无数据背书的方向预设
    （顺势可加 / 不抄底 / 高抛低吸 / 逢高减 / 谨慎看多）——它们会和系统自己的
    OHLC 概率表打架、干扰 LLM。uptrend / downtrend / range_bound / recovery 改成
    中性引用该 (symbol, regime) 的 30d forward return 概率口径（中位 / 跌破现价
    概率 / 样本数），方向判断交回 LLM + 数据。

    crash / unknown 保留（不是方向预测）：
    - crash「离场观望」是可执行性语义（崩盘期波动极高，任何方向都难理性执行）；
    - unknown「维持原计划」是缺数据时的保守默认。

    prob_hint: 调用方用 core.regime_probability.get_regime_forward_summary 算好的
    概率口径 dict {median_pct, p_below, n, effective_n}；None → 退化无数字中性版。
    （本模块不能 import regime_probability —— 后者依赖本模块 classify_regime，循环依赖，
    所以概率数据由调用方注入。）symbol 保留作签名兼容（本函数已不再用）。
    """
    if regime == "crash":
        return "崩盘 — 离场观望，等波动率回归正常 (ATR < 3%) 再说"
    if regime == "unknown":
        return "状态未知 — 数据不足，维持原计划"
    # uptrend / downtrend / range_bound / recovery：中性引用概率口径，不预设方向
    return _regime_data_hint(price_quantile_2y, prob_hint)


def format_regime_brief(
    metrics: Dict[str, Any],
    symbol: Optional[str] = None,
    *,
    prob_hint: Optional[Dict[str, Any]] = None,
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
        regime, metrics.get("price_quantile_2y"), symbol=symbol, prob_hint=prob_hint,
    )

    inputs_str = ", ".join(
        f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
        for k, v in inputs.items()
    )

    # 标记是否用了 per-asset 覆盖，让 LLM 看到"这个阈值不是默认值"
    from core.config import load_config
    has_override = bool(symbol and load_config().regime_per_asset.get(symbol))
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
