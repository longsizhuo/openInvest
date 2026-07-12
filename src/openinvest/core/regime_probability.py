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

# 纯计算核已迁 calc 层（ADR-026）——import * 导回保持历史导出面；本文件只剩 IO shell
# （文件读取 / MarketStore / 汇率腿 / MemoryStore 默认路径）。monkeypatch 计算逻辑
# 请钉 openinvest.calc.regime_probability 命名空间。下方 forward_return 为同签名
# IO 包装（closes=None 时拉 MarketStore），定义在 import * 之后故遮蔽 calc 版。
from openinvest.calc.regime_probability import *  # noqa: F401,F403
from openinvest.calc.regime_probability import forward_return as _forward_return_pure
from openinvest.calc.market_metrics import TRADING_DAYS_2Y as _TRADING_DAYS_2Y  # noqa: F401  单一可信源别名（test_market_metrics 守）




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
            from openinvest.core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        table = build_probability_table(jsonl_path, threshold_pct)
    return table.get((asset, regime))


# ============================================================================
# 买回点估计 (reentry) — 给 TRIM 决策用："卖出后 D+window **期末**历史上落在哪、概率多大"
# ============================================================================




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
    """基于历史 forward return 分布给出卖出后买回点参考（期末收益分位口径，非途中最低价）。

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
                from openinvest.core.memory_store import MemoryStore
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
    "该 regime 历史 30d forward return：中位X%、期末低于现价概率Y%、样本n"，让 LLM
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




def _fx_forward_returns(
    base_ccy: str, quote_ccy: str, windows: Tuple[str, ...],
    asof: Optional[str] = None,
):
    """FX (base→quote) 每个 window 的**日历日**远期收益 Series（date-indexed，小数）。
    USD→CNY 读 MarketStore 的 USDCNY=X。返回 (dict[window→pd.Series], fx_symbol)；无数据 ({}, fx_symbol)。

    与本币腿同口径(同一 searchsorted 日历日 + 同 asof 截点)，使两腿可按日期对齐逐点相乘——
    既保证跨度一致(不再 shift(-d) 交易日)，又零前视，又保留金/汇真实共动。"""
    import numpy as np
    import pandas as pd
    from openinvest.db.market_store import MarketStore
    fxsym = f"{base_ccy}{quote_ccy}=X"
    out: Dict[str, Any] = {}
    fdf = MarketStore().get_history_df(fxsym, days=100000)
    if fdf is None or fdf.empty or "Close" not in fdf.columns:
        return out, fxsym
    if asof is not None:
        fdf = fdf[fdf.index <= asof]  # point-in-time：汇率腿也只用 asof 及之前
    fc = fdf["Close"].dropna()
    if fc.empty:
        return out, fxsym
    idx = fc.index
    vals = fc.to_numpy()
    nfx = len(fc)
    arange = np.arange(nfx)
    for w in windows:
        days = int(w.rstrip("d"))
        pos = idx.searchsorted(idx + pd.Timedelta(days=days), side="left")
        valid = pos < nfx
        r = np.full(nfx, np.nan)
        r[valid] = vals[pos[valid]] / vals[arange[valid]] - 1.0
        s = pd.Series(r, index=idx).dropna()
        if not s.empty:
            out[w] = s
    return out, fxsym


def build_reentry_reference_text(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    windows: Tuple[str, ...] = ("30d", "60d", "90d"),
    convert_ccy: Optional[str] = None,
) -> str:
    """给 CIO brief 用的卖出后路径参考文本（多 window）。无可用数据返回 ""。"""
    text, _profile = build_reentry_reference(
        asset, regime, current_price,
        source=source, jsonl_path=jsonl_path, records=records, windows=windows,
        convert_ccy=convert_ccy,
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
    convert_ccy: Optional[str] = None,
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
            from openinvest.core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        records = _load_reviews(jsonl_path)

    # OHLC 源：一次 get_path_profile 算完多窗 + 路径形状（单次行情加载），
    # 再组装成与 verdict_review 源同款的 ReentryEstimate 行。
    profile: Optional[Dict[str, Any]] = None
    if source == "ohlc":
        try:
            # convert_ccy 时 get_path_profile 一次算出本币 windows + 持仓币 currency_overlay
            # （单次行情加载）。base windows 始终报价币种,与委员会其余 brief 同口径。
            profile = get_path_profile(asset, regime, windows=windows, convert_ccy=convert_ccy)
            if profile:
                # 校准层（config path 节，经 fit/OOS 验证前默认禁用=恒等变换）。
                # CIO 看到的文本与落盘快照都用校准后的分布——所见即所验。
                profile = calibrate_profile(profile)
                # overlay 仅在汇率序列真实存在时才有(get_path_profile 保证带 currency 键)——
                # 没真转换就不挂,避免拿未折算的本币分布冒充持仓币口径。同样过一遍校准。
                _ov = profile.get("currency_overlay")
                if _ov and _ov.get("currency"):
                    profile["currency_overlay"] = calibrate_profile(_ov)
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

    # 持仓币种口径下行提示（ADR-021）：仅当 currency_overlay 存在（持仓非报价币种）时附一行。
    ccy_note = ""
    _ov = (profile or {}).get("currency_overlay")
    if _ov and _ov.get("windows"):
        _ccy = _ov.get("currency", "")
        # 报价币种本币口径(可能是 USD/AUD/HKD/CNY,不写死 USD —— NDQ.AX 是 AUD,0700.HK 是 HKD)
        _base = quote_currency_iso(asset) or "报价币"
        _seg = []
        for w in windows:
            c = _ov["windows"].get(w)
            u = (profile or {}).get("windows", {}).get(w)
            if c and u:
                _seg.append(
                    f"{w} 期末低于现价 {c['p_below'] * 100:.0f}% · 20分位 {c['downside_pct']:+.1f}%"
                    f"（{_base} 口径 {u['downside_pct']:+.1f}%）"
                )
        if _seg:
            ccy_note = (
                f"\n- ⚠ 持仓以 {_ccy} 计价：下行按 {_ccy} 口径（含汇率，{_base} 远期分布逐日对齐 {_base}{_ccy} 汇率）"
                f" → " + " | ".join(_seg)
                + f"（{_base} 口径会低估你的下行；上方价位仍为资产报价币种）"
            )

    text = (
        f"# 路径参考（regime={regime} 历史 forward 路径分布；TRIM 买回点 + 持有路径预期）：\n"
        f"- 现价: {cur}{current_price:,.2f}\n"
        + "\n".join(lines + shape_lines)
        + ccy_note
        + "\n（若要 TRIM，REENTRY_PRICE 必须低于现价；以上概率均为 D+window **期末**口径（非途中最低点）——期末仍低于现价的概率低 = 卖出后大概率买不回更低 = 别 TRIM。"
        "先跌后涨占比高 = 回踩是该 regime 的常态路径，浅回踩别恐慌性止损）"
    )
    return text, (profile if source == "ohlc" else None)







def get_path_profile(
    asset: str,
    regime: str,
    *,
    windows: Tuple[str, ...] = ("30d", "60d", "90d"),
    shape_window: str = "90d",
    days: int = 100000,
    asof: Optional[str] = None,
    convert_ccy: Optional[str] = None,
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
    from openinvest.db.market_store import MarketStore
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

    def _window_stats(rows, w: str, fx=None) -> Optional[Dict[str, Any]]:
        col = f"fwd_{w}"
        if col not in rows.columns:
            return None
        base = rows[col].dropna()
        if base.empty:
            return None
        n = len(base)
        # 独立样本由本币(如 USD)腿决定——汇率腿更厚，瓶颈在本币腿，故 effective_n 不被卷积虚抬
        eff = max(1, n // int(w.rstrip("d")))
        extra: Dict[str, Any] = {}
        if fx is not None and len(fx):
            import pandas as pd
            # 币种转换（ADR-021 修订）：原 MC 独立采样有 3 个缺陷——两腿跨度不一致(汇率腿
            # shift(-d) 交易日 vs 本币腿日历日)、无视 asof、且独立配对抹平金汇共动。改为对每个
            # regime 日取**同一日历窗内实际发生**的汇率远期收益(_fx_forward_returns 已同口径),
            # 按日期内连接后逐点 (1+r_base)(1+r_fx)-1 → 跨度一致、零前视、保留"金跌时 CNY 走强
            # 放大 CNY 持有者下行"的真实共动。
            joined = pd.concat(
                [base.rename("b"), fx.rename("f")], axis=1, join="inner"
            ).dropna()
            if joined.empty:
                rets = base * 100  # 无对齐交集 → 退回本币分布
            else:
                rets = ((1.0 + joined["b"]) * (1.0 + joined["f"]) - 1.0) * 100.0
                n = len(rets)
                eff = max(1, n // int(w.rstrip("d")))
                extra["fx_aligned_n"] = n
        else:
            rets = base * 100
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
            **extra,
        }

    # 无条件分布（同资产全部非 unknown 历史日）：小样本收缩校准的混合对象
    uncond = frame[frame["regime"] != "unknown"]
    out: Dict[str, Any] = {"asset": asset, "regime": regime,
                           "windows": {}, "uncond_windows": {}, "shape": None}
    # 币种自适应（ADR-021）：持仓以非报价币种计价时（GC=F 报 USD 但浙商积存金记 CNY），
    # 把报价币分布按汇率折算到持仓币种,挂在 currency_overlay。本币黄金 57 年 ⊗ 汇率几十年,
    # 两腿样本都厚,绕开"长 XAU/CNY 历史不存在"。终端风险按持仓币种；形状(何时见底)仍按本币。
    # 主 windows 始终是报价币种(文本/价位用);转换分布只进 overlay,同一帧算完 → 单次行情加载,
    # base 调用方零影响,也修掉 build_reentry 原先的二次 get_path_profile 双读。
    fx_map: Dict[str, Any] = {}
    fx_symbol = None
    base_ccy = quote_currency_iso(asset)
    if convert_ccy and base_ccy and convert_ccy.upper() != base_ccy:
        try:
            fx_map, fx_symbol = _fx_forward_returns(
                base_ccy, convert_ccy.upper(), all_windows, asof=asof
            )
        except Exception:  # noqa: BLE001  汇率读失败 → 退回本币分布（不阻断）
            fx_map = {}
    for w in windows:
        st = _window_stats(sub, w)  # 主分布:始终报价币种口径
        if st is None:
            continue
        out["windows"][w] = st
        ust = _window_stats(uncond, w)
        if ust is not None:
            out["uncond_windows"][w] = ust
    if fx_map:
        ov_w, ov_u = {}, {}
        for w in windows:
            cst = _window_stats(sub, w, fx=fx_map.get(w))
            if cst is not None:
                ov_w[w] = cst
            cust = _window_stats(uncond, w, fx=fx_map.get(w))
            if cust is not None:
                ov_u[w] = cust
        if ov_w:
            out["currency_overlay"] = {
                "currency": convert_ccy.upper(),
                "currency_method": (
                    f"date-aligned conversion: {asset}({base_ccy}) ⊗ {fx_symbol} "
                    f"逐日对齐实际远期收益; 终端分布按 {convert_ccy.upper()} 口径(含汇率),形状仍按 {base_ccy}"
                ),
                "windows": ov_w,
                "uncond_windows": ov_u,
            }

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
    from openinvest.db.market_store import MarketStore
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
    from openinvest.db.market_store import MarketStore
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


def forward_return(
    symbol: str, asof: str, calendar_days: int,
    *, closes: Optional["pd.Series"] = None,
) -> Optional[float]:
    """同签名 IO 包装：closes 未传时从 MarketStore 拉取，再委托 calc 纯核。

    口径 docstring 见 calc.regime_probability.forward_return（单一可信源）。
    """
    import pandas as pd
    if closes is None:
        from openinvest.db.market_store import MarketStore
        df = MarketStore().get_history_df(symbol, days=100000)
        if df is None or df.empty or "Close" not in df:
            return None
        closes = df["Close"]
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
        closes = closes[~closes.index.duplicated(keep="last")].dropna()
    return _forward_return_pure(symbol, asof, calendar_days, closes=closes)
