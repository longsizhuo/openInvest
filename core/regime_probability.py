"""Regime 概率表 — 基于历史 verdict_review 数据的条件概率分布

设计决策（2026-05-28）：
- 按 (asset, regime) 分组，不按 verdict 分。原因：regime 是信号，verdict 几乎无增量。
  验证见 /tmp/regime_vs_verdict_signal.md。
- 阈值 5% 做成参数，默认 5%。
- n<10 的组返回 low_confidence 标记。
- 数据源：memory/.dreams/verdict_review.jsonl（由 jobs/verdict_review.py 生成）。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 默认阈值：30d return ±5% 算"大涨/大跌"
DEFAULT_THRESHOLD_PCT = 5.0
# 低于此样本量标记 low_confidence
MIN_CONFIDENT_N = 10
# 买回参考用的"悲观但可能"分位（forward return 的低分位 → 卖出后可能触及的低点）
REENTRY_DOWNSIDE_QUANTILE = 0.20


@dataclass
class RegimeProbability:
    """单个 (asset, regime) 的概率分布"""
    asset: str
    regime: str
    n: int
    p_up: float          # P(return > threshold%)
    p_down: float        # P(return < -threshold%)
    p_flat: float        # P(|return| <= threshold%)
    median_return: float # 中位 return (%)
    mean_return: float   # 均值 return (%)
    threshold_pct: float # 使用的阈值
    low_confidence: bool # effective_n < MIN_CONFIDENT_N（重叠窗口源用独立样本判定）
    # 独立样本估计：重叠窗口源（OHLC 日度 forward return）≈ n/窗口天数；
    # 非重叠源（verdict_review，逐决策）= n。默认 0 → summary 回退用 n。
    effective_n: int = 0

    def summary_line(self) -> str:
        """一行中文摘要，给 daily report 用"""
        conf = " ⚠样本不足" if self.low_confidence else ""
        eff = self.effective_n or self.n
        # 重叠窗口源：明确标注独立样本量，避免 n 被误读为独立观测数
        overlap = f"，重叠窗口独立≈{eff}" if eff != self.n else ""
        return (
            f"{self.asset} {self.regime} (n={self.n}{overlap}): "
            f"涨>{self.threshold_pct:.0f}% {self.p_up * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% {self.p_down * 100:.0f}%、"
            f"中位 {self.median_return:+.1f}%"
            f"{conf}"
        )


def _load_reviews(jsonl_path: Path) -> List[Dict[str, Any]]:
    """读 verdict_review.jsonl，返回记录列表。文件不存在返空列表。"""
    if not jsonl_path.exists():
        return []
    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def build_probability_table(
    jsonl_path: Path,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    window: str = "30d",
) -> Dict[Tuple[str, str], RegimeProbability]:
    """从 verdict_review.jsonl 构建 (asset, regime) → 概率分布 表。

    Args:
        jsonl_path: verdict_review.jsonl 路径
        threshold_pct: 涨跌阈值百分比，默认 5%
        window: 使用的 return 窗口，默认 "30d"

    Returns:
        dict[(asset, regime)] → RegimeProbability
    """
    records = _load_reviews(jsonl_path)
    if not records:
        return {}

    # 按 (asset, regime) 分组
    groups: Dict[Tuple[str, str], List[float]] = {}
    for r in records:
        asset = r.get("asset", "")
        regime = r.get("regime_at_decision", "")
        ret = r.get("actual_returns", {}).get(window)
        if not asset or not regime or ret is None:
            continue
        key = (asset, regime)
        groups.setdefault(key, []).append(ret * 100)  # 转为百分比

    # 计算概率
    table: Dict[Tuple[str, str], RegimeProbability] = {}
    for (asset, regime), returns in groups.items():
        n = len(returns)
        p_up = sum(1 for r in returns if r > threshold_pct) / n
        p_down = sum(1 for r in returns if r < -threshold_pct) / n
        p_flat = 1.0 - p_up - p_down
        table[(asset, regime)] = RegimeProbability(
            asset=asset,
            regime=regime,
            n=n,
            p_up=round(p_up, 4),
            p_down=round(p_down, 4),
            p_flat=round(p_flat, 4),
            median_return=round(statistics.median(returns), 2),
            mean_return=round(statistics.mean(returns), 2),
            threshold_pct=threshold_pct,
            low_confidence=n < MIN_CONFIDENT_N,  # verdict_review 非重叠 → effective_n = n
            effective_n=n,
        )

    return table


def get_regime_probability(
    asset: str,
    regime: str,
    table: Optional[Dict[Tuple[str, str], RegimeProbability]] = None,
    jsonl_path: Optional[Path] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> Optional[RegimeProbability]:
    """查询单个 (asset, regime) 的概率分布。

    可以传预构建的 table（避免重复读文件），或传 jsonl_path 现场构建。
    两者都传时 table 优先。

    Returns:
        RegimeProbability 或 None（无数据）
    """
    if table is None:
        if jsonl_path is None:
            # 默认路径
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        table = build_probability_table(jsonl_path, threshold_pct)
    return table.get((asset, regime))


# ============================================================================
# 买回点估计 (reentry) — 给 TRIM 决策用："卖出后历史上会跌到哪、概率多大"
# ============================================================================


def _percentile(values: List[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]）。values 非空。"""
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


@dataclass
class ReentryEstimate:
    """单个 (asset, regime, window) 的卖出后路径 / 买回点估计。

    基于历史 forward return 分布给"悲观但可能"的低点 + 触及概率，
    作为 CIO 出 TRIM 时填 REENTRY_PRICE 的数据参考（不是强制值）。
    """
    asset: str
    regime: str
    window: str
    n: int
    current_price: float
    threshold_pct: float
    p_below_current: float   # P(forward return < 0)，即跌破现价概率
    p_down: float            # P(forward return < -threshold%)
    median_return_pct: float
    downside_pct: float      # forward return 的低分位（REENTRY_DOWNSIDE_QUANTILE）
    downside_price: float    # current_price × (1 + downside_pct/100)
    has_downside: bool       # downside_price < current_price → 存在低于现价的买回参考
    low_confidence: bool     # effective_n < MIN_CONFIDENT_N（重叠窗口源用独立样本判定）
    # 独立样本估计：OHLC 重叠日度 forward return ≈ n/窗口天数；verdict_review = n。
    effective_n: int = 0
    # 报价币种符号（display 用）。GC=F 报价是美元/盎司，硬编码 ¥ 会把 $4116
    # 标成 ¥4116（2026-06-12 用户邮件困惑事故）。channel 币种（如积存金 ¥/克）
    # 是另一回事，由 CIO 的渠道快照负责。
    currency: str = "¥"

    def summary_line(self) -> str:
        conf = " ⚠样本不足" if self.low_confidence else ""
        eff = self.effective_n or self.n
        overlap = f"，重叠窗口独立≈{eff}" if eff != self.n else ""
        if not self.has_downside:
            return (
                f"{self.window} (n={self.n}{overlap}): 该 regime 历史上 {self.window} 内"
                f"跌破现价概率仅 {self.p_below_current * 100:.0f}%，"
                f"{int(REENTRY_DOWNSIDE_QUANTILE * 100)} 分位仍 {self.downside_pct:+.1f}%"
                f"（无明显低于现价的买回点）{conf}"
            )
        return (
            f"{self.window} (n={self.n}{overlap}): 跌破现价概率 {self.p_below_current * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% 概率 {self.p_down * 100:.0f}%；"
            f"悲观情形({int(REENTRY_DOWNSIDE_QUANTILE * 100)}分位) {self.downside_pct:+.1f}% "
            f"→ {self.currency}{self.downside_price:,.2f}；中位 {self.median_return_pct:+.1f}%{conf}"
        )


def get_reentry_estimate(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    window: str = "30d",
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    days: int = 100000,
) -> Optional[ReentryEstimate]:
    """基于历史 forward return 分布给出卖出后买回点参考。

    Args:
        asset / regime: 分组键
        current_price: 现价（cost currency）。None / <=0 → 返回 None（无法折算价格）
        window: forward return 窗口，"30d" / "90d"。该窗口无样本 → 返回 None
        source: "ohlc"（默认，几十年 OHLC 直算，0 token）| "verdict_review"（旧源，保留）
        records / jsonl_path: 仅 source="verdict_review" 时用

    Returns:
        ReentryEstimate 或 None（无现价 / 该 window 无历史样本）
    """
    if current_price is None or current_price <= 0:
        return None

    if source == "ohlc":
        returns = _ohlc_forward_returns(asset, regime, window, days=days)
    else:
        # verdict_review 源（保留：命中率/校准仍用；这里只在显式指定时走）
        if records is None:
            if jsonl_path is None:
                from core.memory_store import MemoryStore
                jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
            records = _load_reviews(jsonl_path)
        returns = []
        for r in records:
            if r.get("asset") != asset or r.get("regime_at_decision") != regime:
                continue
            ret = (r.get("actual_returns") or {}).get(window)
            if ret is None:
                continue
            returns.append(ret * 100)  # 转百分比

    if not returns:
        # 该 window 在历史里无样本（如 90d verdict_review 未回填）→ unavailable
        return None

    n = len(returns)
    # OHLC 源是重叠日度 forward return → 独立样本 ≈ n/窗口天数；verdict_review 非重叠 → n。
    window_days = int(window.rstrip("d")) if source == "ohlc" else 1
    effective_n = max(1, n // window_days)
    downside_pct = round(_percentile(returns, REENTRY_DOWNSIDE_QUANTILE), 2)
    downside_price = round(current_price * (1 + downside_pct / 100), 4)
    return ReentryEstimate(
        asset=asset,
        regime=regime,
        window=window,
        n=n,
        current_price=current_price,
        threshold_pct=threshold_pct,
        p_below_current=round(sum(1 for x in returns if x < 0) / n, 4),
        p_down=round(sum(1 for x in returns if x < -threshold_pct) / n, 4),
        median_return_pct=round(statistics.median(returns), 2),
        downside_pct=downside_pct,
        downside_price=downside_price,
        has_downside=downside_price < current_price,
        low_confidence=effective_n < MIN_CONFIDENT_N,
        effective_n=effective_n,
    )


def get_regime_forward_summary(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    window: str = "30d",
) -> Optional[Dict[str, Any]]:
    """给 regime_brief 的 STRATEGY_HINT 用的中性概率口径。

    返回 {median_pct, p_below, n, effective_n, window} 或 None（无现价 / 该
    regime+window 无 OHLC 样本）。调用方（committee_runner / skill.py）算好后
    传进 format_regime_brief —— core.regime 不能直接 import 本模块（本模块依赖
    core.regime.classify_regime，会构成循环依赖），所以数据由调用方注入。

    2026-05-31: 用于把"人写方向预设"（不抄底/逢高减/谨慎看多…）换成中性的
    "该 regime 历史 30d forward return：中位X%、跌破现价概率Y%、样本n"，让 LLM
    基于数据判断方向。源走 OHLC（0 token，与概率表同源）。
    """
    est = get_reentry_estimate(asset, regime, current_price, window=window, source="ohlc")
    if est is None:
        return None
    return {
        "median_pct": est.median_return_pct,
        "p_below": est.p_below_current,
        "n": est.n,
        "effective_n": est.effective_n,
        "window": window,
    }


def calibrate_profile(
    profile: Dict[str, Any],
    *,
    shrinkage_k: Optional[float] = None,
    band_gamma: Optional[float] = None,
) -> Dict[str, Any]:
    """路径分布校准层（2026-06，walk-forward 时代分桶诊断出两个结构性缺陷的修复）：

    1. **小样本收缩**：条件分布按 effective_n 向同资产无条件分布收缩，
       λ = eff_n / (eff_n + k)。治 downtrend 这类独立样本个位数的桶拿 4 个
       样本装精确（GC 07-09 downtrend 带覆盖实测 7%）。k=0 → λ=1 → 禁用。
    2. **带宽校准**：P10/P90/downside 围绕中位按 γ 扩张（带覆盖 7 个时代里
       6 个 < 80% 目标，结构性偏窄）。γ=1 → 禁用。

    参数默认从 config 读（defaults.yaml `path:` 节 → PathConfig，经 fit/OOS
    验证前默认禁用，ADR-010 rule 4）；显式传参覆盖（fit 脚本网格搜索用）。
    返回新 dict（不改原 profile），uncond_windows 缺失时原样返回。
    """
    if shrinkage_k is None or band_gamma is None:
        from core.config import load_config
        cfg = load_config().path
        if shrinkage_k is None:
            shrinkage_k = cfg.shrinkage_k
        if band_gamma is None:
            band_gamma = cfg.band_gamma
    if (not shrinkage_k and band_gamma == 1.0) or not profile:
        return profile
    uncond = profile.get("uncond_windows") or {}
    out = dict(profile)
    out["windows"] = {}
    out["calibration"] = {"shrinkage_k": shrinkage_k, "band_gamma": band_gamma}
    for w, st in (profile.get("windows") or {}).items():
        st = dict(st)
        u = uncond.get(w)
        if shrinkage_k and u:
            lam = st["effective_n"] / (st["effective_n"] + shrinkage_k)
            # 只校准两边都有的字段（fit 脚本喂的 mini-profile 无 p_down）
            for key in ("median_pct", "p_below", "p_down",
                        "p10_pct", "p90_pct", "downside_pct"):
                if key in st and key in u:
                    st[key] = round(lam * st[key] + (1 - lam) * u[key], 4)
            # 概率字段 clip 护栏：凸组合本身封闭于 [0,1]，这里只防上游脏数据静默传播
            for key in ("p_below", "p_down"):
                if key in st:
                    st[key] = min(1.0, max(0.0, st[key]))
        if band_gamma != 1.0:
            med = st["median_pct"]
            for key in ("p10_pct", "p90_pct", "downside_pct"):
                if key in st:
                    st[key] = round(med + band_gamma * (st[key] - med), 4)
        out["windows"][w] = st
    return out


def quote_currency_prefix(asset: str) -> str:
    """yfinance 报价币种符号（display 用，按 ticker 后缀判定）。

    注意是**报价**币种：GC=F 报美元/盎司、NDQ.AX 报澳元——与用户交易渠道的
    币种/单位（如浙商积存金 ¥/克）无关。未识别后缀返回 "$"（yfinance 无后缀
    ticker 默认美股）；指数（^ 开头）是点位无币种，返回 ""。
    """
    a = asset.upper()
    if a.startswith("^"):
        return ""
    if a.endswith(".AX"):
        return "A$"
    if a.endswith(".HK"):
        return "HK$"
    if a.endswith((".SS", ".SZ")):
        return "¥"
    return "$"


def build_reentry_reference_text(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    windows: Tuple[str, ...] = ("30d", "60d", "90d"),
) -> str:
    """给 CIO brief 用的卖出后路径参考文本（多 window）。无可用数据返回 ""。"""
    text, _profile = build_reentry_reference(
        asset, regime, current_price,
        source=source, jsonl_path=jsonl_path, records=records, windows=windows,
    )
    return text


def build_reentry_reference(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    windows: Tuple[str, ...] = ("30d", "60d", "90d"),
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """路径参考 (text, profile)。profile = get_path_profile 的结构化 dict
    （OHLC 源才有；给 path_review 决策时落快照用——事后校验"当时的预测分布"）。

    source: "ohlc"（默认，几十年 OHLC 直算）| "verdict_review"（旧源，保留）。
    """
    if current_price is None or current_price <= 0:
        return "", None
    cur = quote_currency_prefix(asset)
    if source == "verdict_review" and records is None:
        if jsonl_path is None:
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        records = _load_reviews(jsonl_path)

    # OHLC 源：一次 get_path_profile 算完多窗 + 路径形状（单次行情加载），
    # 再组装成与 verdict_review 源同款的 ReentryEstimate 行。
    profile: Optional[Dict[str, Any]] = None
    if source == "ohlc":
        try:
            profile = get_path_profile(asset, regime, windows=windows)
            if profile:
                # 校准层（config path 节，经 fit/OOS 验证前默认禁用=恒等变换）。
                # CIO 看到的文本与落盘快照都用校准后的分布——所见即所验。
                profile = calibrate_profile(profile)
        except Exception:  # noqa: BLE001  概率表读失败不阻断（外层还有 graceful）
            profile = None

    lines: List[str] = []
    any_data = False
    for w in windows:
        if source == "ohlc":
            st = (profile or {}).get("windows", {}).get(w)
            est = None
            if st is not None:
                downside_price = round(current_price * (1 + st["downside_pct"] / 100), 4)
                est = ReentryEstimate(
                    asset=asset, regime=regime, window=w,
                    n=st["n"], current_price=current_price,
                    threshold_pct=DEFAULT_THRESHOLD_PCT,
                    p_below_current=st["p_below"], p_down=st["p_down"],
                    median_return_pct=st["median_pct"],
                    downside_pct=st["downside_pct"],
                    downside_price=downside_price,
                    has_downside=downside_price < current_price,
                    low_confidence=st["low_confidence"],
                    effective_n=st["effective_n"],
                    currency=cur,
                )
        else:
            est = get_reentry_estimate(
                asset, regime, current_price,
                window=w, source=source, records=records,
            )
            if est is not None:
                est = dataclasses.replace(est, currency=cur)
        if est is None:
            lines.append(f"- {w}: 历史样本不足 / unavailable")
        else:
            any_data = True
            lines.append(f"- {est.summary_line()}")
    if not any_data:
        return "", None

    # 路径形状（概率表路径化，2026-06）：仅 OHLC 源有逐日路径可算。
    # 回答"先跌后涨 vs 持续跌 vs 直接涨"占比 + 回踩深度/时点 → 带路径的预测。
    shape_lines: List[str] = []
    if source == "ohlc":
        try:
            shape = (profile or {}).get("shape")
            if shape:
                dip_med_price = current_price * (1 + shape["dip_median_pct"] / 100)
                dip_p25_price = current_price * (1 + shape["dip_p25_pct"] / 100)
                pop_med_price = current_price * (1 + shape["pop_median_pct"] / 100)
                pop_p75_price = current_price * (1 + shape["pop_p75_pct"] / 100)
                shape_lines = [
                    (
                        f"- {shape['window']} 路径形状（n={shape['n']}，"
                        f"重叠窗口独立≈{shape['effective_n']}）: "
                        f"先跌后涨 {shape['pct_dip_then_up'] * 100:.0f}% / "
                        f"直接涨(无显著回踩) {shape['pct_up_no_dip'] * 100:.0f}% / "
                        f"冲高回落 {shape['pct_pop_then_down'] * 100:.0f}% / "
                        f"一路收跌 {shape['pct_down_no_pop'] * 100:.0f}%"
                        f"（\"显著\"=途中波幅 ≥1×当日ATR；冲高回落=先给更高卖点再收跌）"
                    ),
                    (
                        f"- {shape['window']} 途中最高冲高: 中位 {shape['pop_median_pct']:+.1f}%"
                        f"（→ {cur}{pop_med_price:,.2f}），"
                        f"高四分位 {shape['pop_p75_pct']:+.1f}%（→ {cur}{pop_p75_price:,.2f}）；"
                        f"中位 {shape['days_to_peak_median']} 个交易日见顶"
                    ),
                    (
                        f"- {shape['window']} 途中最深回踩: 中位 {shape['dip_median_pct']:+.1f}%"
                        f"（→ {cur}{dip_med_price:,.2f}），"
                        f"深四分位 {shape['dip_p25_pct']:+.1f}%（→ {cur}{dip_p25_price:,.2f}）；"
                        f"中位 {shape['days_to_trough_median']} 个交易日见谷底"
                    ),
                    (
                        f"- {shape['window']} 窗内 regime 中位持续 "
                        f"{shape['regime_persist_median_days']}/"
                        f"{shape['window_median_days']} 个交易日"
                        f"——持续占比低则形状占比含 regime 切换成分，解读权重打折"
                    ),
                ]
        except Exception:  # noqa: BLE001  路径形状取失败不影响多窗分布主体
            shape_lines = []

    text = (
        f"# 路径参考（regime={regime} 历史 forward 路径分布；TRIM 买回点 + 持有路径预期）：\n"
        f"- 现价: {cur}{current_price:,.2f}\n"
        + "\n".join(lines + shape_lines)
        + "\n（若要 TRIM，REENTRY_PRICE 必须低于现价；历史上跌破现价概率低 = 卖出后大概率买不回更低 = 别 TRIM。"
        "先跌后涨占比高 = 回踩是该 regime 的常态路径，浅回踩别恐慌性止损）"
    )
    return text, (profile if source == "ohlc" else None)


# ============================================================================
# OHLC 直算数据源（纯算术，0 LLM token）—— 几十年历史 regime → forward return 分布
# ============================================================================
#
# 为什么换：概率表核心是 (regime → forward return)，regime 用 MA/ATR/分位算、return
# 用价格算，**全是算术**，不需要 committee 跑过。旧源 verdict_review.jsonl 只有 276 条
# （受 committee 实际跑过的次数限制），把样本量卡死在几十/几百。改用 MarketStore 里
# 几十年 OHLC 对历史每一天直算，样本量 → 数千，且复用 production 的 classify_regime
# 保证 regime 口径一致。

# price_quantile_2y 的滚动窗口（≈2 年交易日），对齐 compute_metrics 传 "2y" 数据的口径
from utils.market_metrics import TRADING_DAYS_2Y as _TRADING_DAYS_2Y  # 单一可信源（504）


def _percentile_rank(window):  # window: np.ndarray
    cur = window[-1]
    return float((window <= cur).sum() / len(window))


def _make_regime_probability(
    asset: str, regime: str, returns_pct: List[float], threshold_pct: float,
    *, window_days: int = 1,
) -> RegimeProbability:
    """从一组 forward return(%) 聚合出 RegimeProbability。

    window_days: forward return 的窗口天数。OHLC 源是**重叠**的日度 forward return
    （相邻样本重叠 window_days-1 天），独立样本 ≈ n/window_days；low_confidence 用这个
    effective_n 而非原始 n 判定，避免重叠样本把置信度撑虚高。verdict_review 源是逐决策
    的非重叠样本 → window_days=1 → effective_n = n。
    """
    n = len(returns_pct)
    effective_n = max(1, n // window_days)
    p_up = sum(1 for r in returns_pct if r > threshold_pct) / n
    p_down = sum(1 for r in returns_pct if r < -threshold_pct) / n
    return RegimeProbability(
        asset=asset, regime=regime, n=n,
        p_up=round(p_up, 4), p_down=round(p_down, 4),
        p_flat=round(1.0 - p_up - p_down, 4),
        median_return=round(statistics.median(returns_pct), 2),
        mean_return=round(statistics.mean(returns_pct), 2),
        threshold_pct=threshold_pct,
        low_confidence=effective_n < MIN_CONFIDENT_N,
        effective_n=effective_n,
    )


def compute_regime_return_frame(
    df, symbol: Optional[str] = None, *, windows: Tuple[str, ...] = ("30d", "90d"),
):
    """对历史每一天算 regime + forward return（纯算术，复用 production classify_regime）。

    Args:
        df: OHLC DataFrame（index=DatetimeIndex，必有 Close，有 High/Low 走真 TR）
        symbol: 传则用 per-asset regime 阈值
        windows: forward return 窗口（"30d"/"90d"，按**日历日**前看）

    Returns:
        DataFrame：index=date，列 =
          - "regime"
          - "fwd_<w>"     窗口期末 forward return（小数）
          - "min_<w>"     窗口内**途中最深回踩**（相对当日收盘的最低点收益，小数）
          - "tmin_<w>"    到达谷底的交易日数（路径时序："什么时候跌"）
          - "max_<w>"     窗口内**途中最高冲高**（镜像 min；卖出时机的核心读数：
                          "先给更高卖点再跌"要靠它才看得见——min 轴只问跌没跌过）
          - "tmax_<w>"    到达顶点的交易日数（"什么时候涨"）
          - "persist_<w>" 当日 regime 在窗内实际持续的交易日数（含当日，cap 到窗末）
                          ——形状占比混着 regime 切换成分，靠它判断解读权重
          - "wdays_<w>"   窗内交易日数（persist 的分母参照）
          - "atr_pct"     当日 ATR%（路径形状的自校准"显著"单位，≥1×ATR 才算数）
        指标 warmup 不足的头部 regime=unknown；lookahead 不足的尾部各前向列=NaN。
        空输入返回空 DataFrame。
    """
    import numpy as np
    import pandas as pd
    from core.regime import classify_regime

    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame()

    close = df["Close"].astype(float)
    n = len(close)

    ma20 = close.rolling(20).mean()
    ma120 = close.rolling(120).mean()
    ret30 = close / close.shift(30) - 1.0
    rebound = close / close.rolling(30).min() - 1.0
    quantile = close.rolling(_TRADING_DAYS_2Y, min_periods=20).apply(
        _percentile_rank, raw=True,
    )

    # ATR%（Wilder RMA，真 TR 含跳空）—— 复刻 utils.market_metrics._calc_atr_pct 口径，
    # 但保留为全序列（原函数只返回末值）
    has_hl = (
        "High" in df.columns and "Low" in df.columns
        and not df["High"].isna().all() and not df["Low"].isna().all()
    )
    if has_hl:
        high, low = df["High"].astype(float), df["Low"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / close * 100.0

    def _v(x):
        return None if pd.isna(x) else float(x)

    regimes = []
    for i in range(n):
        regimes.append(classify_regime({
            "ma20": _v(ma20.iat[i]),
            "ma120": _v(ma120.iat[i]),
            "atr_pct": _v(atr_pct.iat[i]),
            "price_quantile_2y": _v(quantile.iat[i]),
            "return_30d": _v(ret30.iat[i]),
            "rebound_off_30d_low": _v(rebound.iat[i]),
        }, symbol=symbol)["regime"])

    out = pd.DataFrame({"regime": regimes}, index=df.index)

    # regime 前向连续段长（不 cap）：persist_<w> 用。倒序扫一遍 O(n)。
    run = np.ones(n, dtype=int)
    for k in range(n - 2, -1, -1):
        if regimes[k + 1] == regimes[k]:
            run[k] = run[k + 1] + 1

    # forward return（日历日）：找 date + Nd 当天或之后第一个收盘
    idx = df.index
    vals = close.values
    arange = np.arange(n)
    for w in windows:
        days = int(w.rstrip("d"))
        pos = idx.searchsorted(idx + pd.Timedelta(days=days), side="left")
        fwd = np.full(n, np.nan)
        valid = pos < n
        fwd[valid] = vals[pos[valid]] / vals[arange[valid]] - 1.0
        out[f"fwd_{w}"] = fwd
        # 路径化（2026-06）：窗口内途中最深回踩/最高冲高 + 各自到达的交易日数。
        # 只在 lookahead 完整（valid）的行算，与 fwd 同口径。
        mins = np.full(n, np.nan)
        tmins = np.full(n, np.nan)
        maxs = np.full(n, np.nan)
        tmaxs = np.full(n, np.nan)
        persists = np.full(n, np.nan)
        wdays = np.full(n, np.nan)
        for i in np.flatnonzero(valid):
            seg = vals[i + 1: pos[i] + 1]
            if seg.size:
                j = int(np.argmin(seg))
                mins[i] = seg[j] / vals[i] - 1.0
                tmins[i] = j + 1  # 交易日数（t+1 起算）
                j = int(np.argmax(seg))
                maxs[i] = seg[j] / vals[i] - 1.0
                tmaxs[i] = j + 1
            # 窗内交易日数（含当日）与 regime 实际持续（精确逐行 cap，无近似魔数）
            wdays[i] = pos[i] - i + 1
            persists[i] = min(run[i], pos[i] - i + 1)
        out[f"min_{w}"] = mins
        out[f"tmin_{w}"] = tmins
        out[f"max_{w}"] = maxs
        out[f"tmax_{w}"] = tmaxs
        out[f"persist_{w}"] = persists
        out[f"wdays_{w}"] = wdays

    # 当日 ATR%：路径形状的自校准"显著"单位（无绝对百分比 magic number）
    out["atr_pct"] = atr_pct.values

    return out


def get_path_profile(
    asset: str,
    regime: str,
    *,
    windows: Tuple[str, ...] = ("30d", "60d", "90d"),
    shape_window: str = "90d",
    days: int = 100000,
    asof: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """(asset, regime) 的多窗路径画像（纯 OHLC 算术，0 token）。

    概率表路径化（2026-06）：从"30 天单点分布"升级成"30/60/90 多窗分布 +
    路径形状"，直接回答"卖出/持有后什么时候跌、什么时候涨、途中给不给低吸点"。

    Returns:
        {
          "asset", "regime",
          "windows": { "30d": {n, effective_n, median_pct, p_below, p10_pct,
                                p90_pct, low_confidence}, ... },
          "shape": {   # 基于 shape_window（默认 90d）的路径形状分布（四类完备）
            "window", "n", "effective_n",
            "pct_dip_then_up",    # 先跌后涨：途中回踩显著 且 期末收正（等回踩能接回）
            "pct_up_no_dip",      # 直接涨：期末收正且无显著回踩（等回调会踏空）
            "pct_pop_then_down",  # 冲高回落：途中冲高显著 且 期末收跌
                                  # ——卖出时机的核心读数："先给更高卖点再跌"的占比
            "pct_down_no_pop",    # 一路收跌：期末收跌且途中没给过显著高点
            "dip_median_pct",     # 途中最深回踩中位（全样本）
            "dip_p25_pct",        # 深四分位（更悲观的回踩深度）
            "days_to_trough_median",  # 中位多少个交易日见谷底（"什么时候跌"）
            "pop_median_pct",     # 途中最高冲高中位（全样本，镜像 dip）
            "pop_p75_pct",        # 高四分位（更乐观的冲高高度）
            "days_to_peak_median",    # 中位多少个交易日见顶（"什么时候涨"）
            "regime_persist_median_days",  # 窗内 regime 实际持续中位（交易日）
            "window_median_days",          # 窗内交易日数中位（persist 的分母参照）
          } | None,
        }
        或 None（无数据 / 该 regime 无样本）。
    "显著"单位 = 当日 ATR%（自校准，无绝对百分比阈值）；回踩 ≤ −1×ATR 算 dipped，
    冲高 ≥ +1×ATR 算 popped。四类 = up 支按 dipped 拆 / ¬up 支按 popped 拆，互斥完备。
    persist：形状占比混着窗内 regime 切换的成分（"涨"可能不是本 regime 给的）——
    persist 中位 / 窗中位 比值低时，形状解读权重要打折。
    """
    from db.market_store import MarketStore
    df = MarketStore().get_history_df(asset.upper(), days=days)
    if asof is not None and df is not None and not df.empty:
        # point-in-time 模式（walk-forward 校准 / 复算"当时会预测什么"）：
        # 只用 asof 及之前的数据建分布，零前视
        df = df[df.index <= asof]
    all_windows = tuple(dict.fromkeys((*windows, shape_window)))
    frame = compute_regime_return_frame(df, asset.upper(), windows=all_windows)
    if frame.empty:
        return None
    sub = frame[frame["regime"] == regime]
    if sub.empty:
        return None

    def _window_stats(rows, w: str) -> Optional[Dict[str, Any]]:
        col = f"fwd_{w}"
        if col not in rows.columns:
            return None
        rets = (rows[col].dropna() * 100)
        if rets.empty:
            return None
        n = len(rets)
        eff = max(1, n // int(w.rstrip("d")))
        return {
            "n": n,
            "effective_n": eff,
            "median_pct": round(float(rets.median()), 2),
            "p_below": round(float((rets < 0).mean()), 4),
            "p_down": round(float((rets < -DEFAULT_THRESHOLD_PCT).mean()), 4),
            "p10_pct": round(float(rets.quantile(0.10)), 2),
            "p90_pct": round(float(rets.quantile(0.90)), 2),
            # 悲观情形低分位（与 get_reentry_estimate 的 downside 同口径）
            "downside_pct": round(float(rets.quantile(REENTRY_DOWNSIDE_QUANTILE)), 2),
            "low_confidence": eff < MIN_CONFIDENT_N,
        }

    # 无条件分布（同资产全部非 unknown 历史日）：小样本收缩校准的混合对象
    uncond = frame[frame["regime"] != "unknown"]
    out: Dict[str, Any] = {"asset": asset, "regime": regime,
                           "windows": {}, "uncond_windows": {}, "shape": None}
    for w in windows:
        st = _window_stats(sub, w)
        if st is None:
            continue
        out["windows"][w] = st
        ust = _window_stats(uncond, w)
        if ust is not None:
            out["uncond_windows"][w] = ust

    sw = shape_window
    cols = [f"fwd_{sw}", f"min_{sw}", f"tmin_{sw}", f"max_{sw}", f"tmax_{sw}",
            f"persist_{sw}", f"wdays_{sw}", "atr_pct"]
    if all(c in sub.columns for c in cols):
        s = sub.dropna(subset=cols)
        if not s.empty:
            end = s[f"fwd_{sw}"] * 100
            dip = s[f"min_{sw}"] * 100
            pop = s[f"max_{sw}"] * 100
            dipped = dip <= -s["atr_pct"]   # 回踩 ≥1×当日ATR = 给过显著低吸点
            popped = pop >= s["atr_pct"]    # 冲高 ≥1×当日ATR = 给过显著高卖点
            up = end > 0
            n = len(s)
            out["shape"] = {
                "window": sw,
                "n": n,
                "effective_n": max(1, n // int(sw.rstrip("d"))),
                # 四类完备：up 支按 dipped 拆，¬up 支按 popped 拆
                "pct_dip_then_up": round(float((dipped & up).mean()), 4),
                "pct_up_no_dip": round(float((~dipped & up).mean()), 4),
                "pct_pop_then_down": round(float((popped & ~up).mean()), 4),
                "pct_down_no_pop": round(float((~popped & ~up).mean()), 4),
                "dip_median_pct": round(float(dip.median()), 2),
                "dip_p25_pct": round(float(dip.quantile(0.25)), 2),
                "days_to_trough_median": int(s[f"tmin_{sw}"].median()),
                "pop_median_pct": round(float(pop.median()), 2),
                "pop_p75_pct": round(float(pop.quantile(0.75)), 2),
                "days_to_peak_median": int(s[f"tmax_{sw}"].median()),
                "regime_persist_median_days": int(s[f"persist_{sw}"].median()),
                "window_median_days": int(s[f"wdays_{sw}"].median()),
            }
    return out if out["windows"] else None


def _ohlc_forward_returns(
    asset: str, regime: str, window: str, *, days: int = 100000,
) -> List[float]:
    """从几十年 OHLC 取 (asset, regime) 在 window 的 forward return(%) 列表（0 token）。"""
    from db.market_store import MarketStore
    df = MarketStore().get_history_df(asset.upper(), days=days)
    frame = compute_regime_return_frame(df, asset.upper(), windows=(window,))
    col = f"fwd_{window}"
    if frame.empty or col not in frame.columns:
        return []
    sub = frame[frame["regime"] == regime].dropna(subset=[col])
    return (sub[col] * 100).tolist()


def build_probability_table_from_ohlc(
    symbols: List[str],
    *,
    window: str = "30d",
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    days: int = 100000,
) -> Dict[Tuple[str, str], RegimeProbability]:
    """从 MarketStore 几十年 OHLC 直算 (asset, regime) → 概率分布（0 LLM token）。

    与 build_probability_table（verdict_review 源）返回同型 dict，可直接替换。
    """
    from db.market_store import MarketStore
    store = MarketStore()
    window_days = int(window.rstrip("d"))  # 重叠窗口 → effective_n = n/window_days
    table: Dict[Tuple[str, str], RegimeProbability] = {}
    col = f"fwd_{window}"
    for sym in symbols:
        sym = sym.upper()
        df = store.get_history_df(sym, days=days)
        frame = compute_regime_return_frame(df, sym, windows=(window,))
        if frame.empty or col not in frame.columns:
            continue
        frame = frame.dropna(subset=[col])
        for regime in frame["regime"].unique():
            if regime == "unknown":
                continue
            rets = (frame.loc[frame["regime"] == regime, col] * 100).tolist()
            if not rets:
                continue
            table[(sym, regime)] = _make_regime_probability(
                sym, regime, rets, threshold_pct, window_days=window_days,
            )
    return table
