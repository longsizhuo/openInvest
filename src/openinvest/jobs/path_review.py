"""路径预测后验校准 — "带路径的预测"闭环的最后一环（2026-06，照 verdict_review 模式）。

校验对象：概率表路径化的输出（docs/wiki/15-path-probability.md）——
30/60/90 多窗分布（中位/p_below/P10-P90/悲观分位）+ 90d 四类路径形状 +
回踩深度/见底时点。这些是确定性预测分布，事后用实际路径逐条对账。

两个数据源：
1. **live 快照**：committee_runner 决策时落的 memory/.committee/<date>/<sym>_path.json
   （与 CIO 看到的同源同算，防代码口径漂移污染 ground truth）
2. **recompute（walk-forward 校准）**：路径预测器纯确定性 → 可历史逐日重算
   "当时会预测什么"（get_path_profile(asof=...) 零前视），今天就能出校准基线，
   不用等 90 天攒 live 样本。标 source="recompute"（代码口径=当前版本）

校准口径（确定性）：
- 每窗：实际 fwd 是否落 [P10,P90]（目标覆盖 ~80%）；p_below 作为概率的
  Brier 分（vs 基率 Brier）
- 形状：预测分布给实际形状类的概率（probability score，vs uniform 0.25）+ top1 命中
- 时点：实际见底交易日 vs 预测中位（MAE，仅 dipped 样本）

输出：memory/.dreams/path_review.jsonl + docs/path_calibration.md（gitignored，
含 symbol 真名只供本地）。成熟度过滤：窗口未走完的不评。
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = INVEST_ROOT
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from openinvest.core.memory_store import MemoryStore  # noqa: E402
from openinvest.core.regime_probability import forward_return  # noqa: E402
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

WINDOWS = ("30d", "60d", "90d")
SHAPE_WINDOW = "90d"
SHAPE_CLASSES = ("dip_then_up", "up_no_dip", "pop_then_down", "down_no_pop")


# ---------------------------------------------------------------------------
# 实际路径（与 core/regime_probability.compute_regime_return_frame 同口径：
# 日历日 searchsorted 对齐、窗内最低/最高、到达交易日数）
# ---------------------------------------------------------------------------
_HIST: Dict[str, pd.Series] = {}


def _closes(symbol: str) -> Optional[pd.Series]:
    if symbol not in _HIST:
        from openinvest.db.market_store import MarketStore
        df = MarketStore().get_history_df(symbol, days=100000)
        if df is None or df.empty or "Close" not in df:
            _HIST[symbol] = None
        else:
            s = df["Close"]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            _HIST[symbol] = s[~s.index.duplicated(keep="last")].dropna()
    return _HIST[symbol]


def realized_path(symbol: str, date: str) -> Optional[Dict[str, Any]]:
    """决策日后的实际路径量。窗口未走完（lookahead 不足）→ 对应字段缺省。

    返回 {fwd_30d/60d/90d(%), min_90d(%), tmin_90d, max_90d(%), tmax_90d}；
    连 30d 都不成熟 → None。
    """
    s = _closes(symbol)
    if s is None:
        return None
    idx = s.index
    ts = pd.Timestamp(date)
    i = idx.searchsorted(ts, side="right") - 1
    if i < 0:
        return None
    base = float(s.iloc[i])   # 路径形状段用；标量 fwd 走 forward_return 单一可信源
    out: Dict[str, Any] = {}
    for w in WINDOWS:
        days = int(w.rstrip("d"))
        fr = forward_return(symbol, date, days, closes=s)   # canonical 日历天口径
        if fr is None:
            continue   # 未成熟
        out[f"fwd_{w}"] = round(fr * 100, 3)
        if w == SHAPE_WINDOW:
            j = idx.searchsorted(ts + pd.Timedelta(days=days), side="left")
            seg = s.iloc[i + 1: j + 1].to_numpy()
            if seg.size:
                k = int(np.argmin(seg))
                out["min_90d"] = round(float(seg[k] / base - 1) * 100, 3)
                out["tmin_90d"] = k + 1
                k = int(np.argmax(seg))
                out["max_90d"] = round(float(seg[k] / base - 1) * 100, 3)
                out["tmax_90d"] = k + 1
    return out if "fwd_30d" in out else None


def realized_shape(real: Dict[str, Any], atr_pct: Optional[float]) -> Optional[str]:
    """与预测同口径的实际形状分类（单位 = 决策日 ATR%）。"""
    if "fwd_90d" not in real or "min_90d" not in real or not atr_pct:
        return None
    up = real["fwd_90d"] > 0
    dipped = real["min_90d"] <= -atr_pct
    popped = real["max_90d"] >= atr_pct
    if up:
        return "dip_then_up" if dipped else "up_no_dip"
    return "pop_then_down" if popped else "down_no_pop"


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------
@dataclass
class PathReview:
    date: str
    asset: str
    regime: str
    source: str                       # live / recompute
    current_price: Optional[float]
    windows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 每窗: {fwd_real, in_band(P10-P90), below_current(实际), p_below(预测),
    #        err_vs_median(实际−预测中位, pp)}
    shape_pred: Dict[str, float] = field(default_factory=dict)
    shape_real: Optional[str] = None
    shape_prob_score: Optional[float] = None   # 预测分布给实际类的概率
    shape_top1_hit: Optional[bool] = None
    trough_pred_days: Optional[int] = None
    trough_real_days: Optional[int] = None


def score_one(snap: Dict[str, Any], source: str) -> Optional[PathReview]:
    """单条快照评分。30d 未成熟 → None（跳过）。"""
    profile = snap.get("profile") or {}
    real = realized_path(snap["symbol"], snap["date"])
    if real is None or not profile.get("windows"):
        return None
    rv = PathReview(
        date=snap["date"], asset=snap["symbol"],
        regime=snap.get("regime") or "", source=source,
        current_price=snap.get("current_price"),
    )
    for w in WINDOWS:
        pred = profile["windows"].get(w)
        fr = real.get(f"fwd_{w}")
        if pred is None or fr is None:
            continue
        row = {
            "fwd_real": fr,
            "in_band": bool(pred["p10_pct"] <= fr <= pred["p90_pct"]),
            "below_current": bool(fr < 0),
            "p_below": pred["p_below"],
            "err_vs_median": round(fr - pred["median_pct"], 3),
            # 原始预测统计（校准研究自包含：fit 脚本纯算术做 收缩/带宽 网格）
            "pred": {k: pred[k] for k in
                     ("median_pct", "p10_pct", "p90_pct", "downside_pct",
                      "p_below", "effective_n") if k in pred},
        }
        u = (profile.get("uncond_windows") or {}).get(w)
        if u:
            row["uncond"] = {k: u[k] for k in
                             ("median_pct", "p10_pct", "p90_pct",
                              "downside_pct", "p_below") if k in u}
        rv.windows[w] = row
    shape = profile.get("shape")
    if shape:
        rv.shape_pred = {
            "dip_then_up": shape["pct_dip_then_up"],
            "up_no_dip": shape["pct_up_no_dip"],
            "pop_then_down": shape.get("pct_pop_then_down", 0.0),
            "down_no_pop": shape.get("pct_down_no_pop",
                                     shape.get("pct_down", 0.0)),
        }
        rv.shape_real = realized_shape(real, snap.get("atr_pct"))
        if rv.shape_real:
            rv.shape_prob_score = round(rv.shape_pred.get(rv.shape_real, 0.0), 4)
            top1 = max(rv.shape_pred, key=rv.shape_pred.get)
            rv.shape_top1_hit = (top1 == rv.shape_real)
        rv.trough_pred_days = shape.get("days_to_trough_median")
        rv.trough_real_days = real.get("tmin_90d")
    return rv if rv.windows else None


# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------
def collect_live_snapshots() -> List[Dict[str, Any]]:
    root = MemoryStore().root / ".committee"
    out = []
    if not root.exists():
        return out
    for p in sorted(root.glob("*/*_path.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def recompute_snapshots(
    symbols: List[str], dates: List[str],
) -> List[Dict[str, Any]]:
    """walk-forward 重算"当时会预测什么"（零前视，代码口径=当前版本）。"""
    from openinvest.core.regime_probability import compute_regime_return_frame, get_path_profile
    from openinvest.db.market_store import MarketStore

    out = []
    for sym in symbols:
        df = MarketStore().get_history_df(sym, days=100000)
        if df is None or df.empty:
            continue
        # 逐日 point-in-time regime + atr（frame 的 regime/atr 列本就零前视）
        frame = compute_regime_return_frame(df, sym, windows=("30d",))
        first = frame.index.min()
        for d in dates:
            ts = pd.Timestamp(d)
            # 估计史不足 2 年的日期跳过——当时的分布建立在过薄的数据上，
            # 评它的校准没有意义（结构护栏，非窗口调参）。
            # 量纲是日历天≈近似：停牌/缺口多的资产可能日历满 2 年但有效样本薄；
            # 当前资产池为连续日线，可接受。要精确可换 len(frame.loc[:ts])。
            if (ts - first).days < 730:
                continue
            rows = frame[frame.index <= ts]
            if rows.empty:
                continue
            row = rows.iloc[-1]
            regime = row["regime"]
            if regime == "unknown":
                continue
            try:
                profile = get_path_profile(sym, regime, asof=d)
            except Exception:  # noqa: BLE001
                continue
            if not profile:
                continue
            s = _closes(sym)
            i = s.index.searchsorted(ts, side="right") - 1
            out.append({
                "schema": 1, "date": d, "symbol": sym, "regime": regime,
                "current_price": float(s.iloc[i]) if i >= 0 else None,
                "atr_pct": float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None,
                "profile": profile,
            })
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(reviews: List[PathReview]) -> Dict[str, Any]:
    summ: Dict[str, Any] = {"n": len(reviews), "windows": {}, "shape": {}}
    for w in WINDOWS:
        rows = [r.windows[w] for r in reviews if w in r.windows]
        if not rows:
            continue
        n = len(rows)
        cov = sum(1 for x in rows if x["in_band"]) / n
        # p_below 校准：Brier vs 基率 Brier（越低越好）
        briers = [(x["p_below"] - (1.0 if x["below_current"] else 0.0)) ** 2 for x in rows]
        base = sum(1 for x in rows if x["below_current"]) / n
        base_briers = [(base - (1.0 if x["below_current"] else 0.0)) ** 2 for x in rows]
        errs = [x["err_vs_median"] for x in rows]
        summ["windows"][w] = {
            "n": n,
            "band_coverage": round(cov, 3),          # 目标 ~0.80
            "p_below_brier": round(float(np.mean(briers)), 4),
            "base_rate_brier": round(float(np.mean(base_briers)), 4),
            "median_abs_err_pp": round(float(np.median([abs(e) for e in errs])), 2),
            "median_err_pp": round(float(np.median(errs)), 2),  # 系统性偏差（+=预测偏保守）
        }
    sh = [r for r in reviews if r.shape_real]
    if sh:
        n = len(sh)
        summ["shape"] = {
            "n": n,
            "prob_score_mean": round(float(np.mean([r.shape_prob_score for r in sh])), 4),
            "uniform_baseline": 0.25,
            "top1_hit": round(sum(1 for r in sh if r.shape_top1_hit) / n, 3),
            "trough_mae_days": round(float(np.mean([
                abs(r.trough_real_days - r.trough_pred_days)
                for r in sh
                if r.trough_real_days is not None and r.trough_pred_days is not None
            ])), 1) if any(
                r.trough_real_days is not None and r.trough_pred_days is not None
                for r in sh
            ) else None,
        }
    # 按 regime 分桶（解读"哪个 regime 的路径分布最可信"）
    summ["by_regime"] = {}
    for rg in sorted({r.regime for r in reviews}):
        rows = [r.windows.get("90d") for r in reviews
                if r.regime == rg and "90d" in r.windows]
        if rows:
            summ["by_regime"][rg] = {
                "n": len(rows),
                "band_coverage_90d": round(
                    sum(1 for x in rows if x["in_band"]) / len(rows), 3),
            }
    return summ


def write_outputs(reviews: List[PathReview], summ: Dict[str, Any]) -> Tuple[Path, Path]:
    store = MemoryStore()
    jl = store.root / ".dreams" / "path_review.jsonl"
    jl.parent.mkdir(parents=True, exist_ok=True)
    with jl.open("w", encoding="utf-8") as f:
        for r in reviews:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    md = ROOT / "docs" / "path_calibration.md"
    lines = ["# 路径预测校准报告（gitignored，本地分析用）", "",
             f"样本 {summ['n']} 条", "",
             "| 窗 | n | P10-P90 覆盖(目标0.8) | p_below Brier | 基率 Brier | 中位|误差|pp | 中位偏差pp |",
             "|---|---|---|---|---|---|---|"]
    for w, x in summ["windows"].items():
        lines.append(f"| {w} | {x['n']} | {x['band_coverage']:.0%} | {x['p_below_brier']} "
                     f"| {x['base_rate_brier']} | {x['median_abs_err_pp']} | {x['median_err_pp']:+} |")
    if summ.get("shape"):
        s = summ["shape"]
        lines += ["", f"形状（n={s['n']}）: 概率分 {s['prob_score_mean']}（uniform 0.25）"
                  f" | top1 命中 {s['top1_hit']:.0%} | 见底时点 MAE {s['trough_mae_days']} 交易日"]
    lines += ["", "| regime | n | 90d 带覆盖 |", "|---|---|---|"]
    for rg, x in summ["by_regime"].items():
        lines.append(f"| {rg} | {x['n']} | {x['band_coverage_90d']:.0%} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jl, md


def run(*, recompute_dates: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    snaps = [(s, "live") for s in collect_live_snapshots()]
    if recompute_dates:
        if symbols is None:
            from openinvest.core.portfolio_manager import PortfolioManager
            symbols = [a["symbol"] for a in
                       PortfolioManager().strategy.get("target_assets", [])
                       if a.get("symbol")]
        snaps += [(s, "recompute")
                  for s in recompute_snapshots(symbols, recompute_dates)]
    reviews = []
    for snap, src in snaps:
        try:
            r = score_one(snap, src)
        except Exception as e:  # noqa: BLE001  单条坏数据不阻断
            log.warning(f"path_review 单条失败跳过 {snap.get('date')}|{snap.get('symbol')}: {e}")
            continue
        if r is not None:
            reviews.append(r)
    summ = summarize(reviews)
    jl, md = write_outputs(reviews, summ)
    return {"reviews": len(reviews), "summary": summ,
            "jsonl": str(jl), "report": str(md)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="路径预测后验校准")
    ap.add_argument("--recompute-weekly-since", metavar="YYYY-MM-DD",
                    help="walk-forward 重算：自该日起每周一的预测（校准基线用）")
    ap.add_argument("--symbols", default="", help="逗号分隔；默认 strategy.target_assets")
    args = ap.parse_args()
    dates = None
    if args.recompute_weekly_since:
        start = pd.Timestamp(args.recompute_weekly_since)
        end = pd.Timestamp.now() - pd.Timedelta(days=30)   # 至少 30d 成熟
        dates = [d.strftime("%Y-%m-%d")
                 for d in pd.date_range(start, end, freq="W-MON")]
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    out = run(recompute_dates=dates, symbols=syms)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=1))
    print(f"\n{out['reviews']} 条 → {out['jsonl']}\n报告: {out['report']}")
