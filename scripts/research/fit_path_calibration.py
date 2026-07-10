"""路径分布校准参数拟合 — fit/OOS 分割，预注册验收（确定性，纯算术）。

输入：memory/.dreams/path_review.jsonl（walk-forward recompute，行内自带
原始条件/无条件预测统计 pred/uncond）。
变换直接调 core/regime_probability.calibrate_profile（单源，防公式两处漂移）：
  λ = eff_n / (eff_n + k)；stat' = λ·cond + (1−λ)·uncond     （小样本收缩）
  q'' = median' + γ·(q' − median')   对 p10/p90/downside      （带宽扩张）

预注册（跑网格前写死）：
- fit 时代 = date ≤ 2017-12-31；OOS = 2018-01-01 之后
- fit 目标 = Σ_w |coverage_w − 0.80| + mean_w Brier_w（30/60/90 等权）
- 网格 k ∈ {0,5,10,20,40,80}，γ ∈ {1.0,1.1,...,1.6}
- **验收（OOS）**：≥2 个窗的覆盖率落 [0.75, 0.85] 且每窗 Brier ≤ 未校准 Brier
  → 通过才允许把 (k,γ) 写进 defaults.yaml（ADR-010 rule 4）

⚠️ 已知统计局限（issue #179 P2 留痕）：fit/OOS 分窗内的 forward return 样本
窗口重叠，独立有效样本量 << 名义 n，显著性判断按名义 n 会过于乐观；且 (k,γ)
已在生产使用，再用同一历史调参有双重校准（double-dipping）风险。重调参前先做
有效样本量修正（effective_n ≈ n/窗口天数）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore  # noqa: E402
from openinvest.core.regime_probability import calibrate_profile  # noqa: E402

WINDOWS = ("30d", "60d", "90d")
FIT_END = "2017-12-31"
K_GRID = (0, 5, 10, 20, 40, 80)
G_GRID = (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)
TARGET = 0.80


def load_rows():
    path = MemoryStore().root / ".dreams" / "path_review.jsonl"
    out = []
    for line in path.open():
        r = json.loads(line)
        for w, x in r.get("windows", {}).items():
            if "pred" not in x or "uncond" not in x:
                continue
            out.append({
                "date": r["date"], "asset": r["asset"], "window": w,
                "fwd": x["fwd_real"], "below": x["below_current"],
                "pred": x["pred"], "uncond": x["uncond"],
            })
    return out


def apply_calibration(row, k: float, g: float):
    """返回校准后的 (median, p10, p90, p_below)。

    组 mini-profile 直接调 core 的 calibrate_profile —— 与 production 同一份
    实现，公式改动只需改 core 一处（含每步 round(...,4) 的数值口径）。
    """
    w = row["window"]
    prof = calibrate_profile(
        {"windows": {w: row["pred"]}, "uncond_windows": {w: row["uncond"]}},
        shrinkage_k=k, band_gamma=g,
    )
    st = prof["windows"][w]
    return st["median_pct"], st["p10_pct"], st["p90_pct"], st["p_below"]


def evaluate(rows, k: float, g: float):
    """每窗 {coverage, brier}。"""
    out = {}
    for w in WINDOWS:
        sub = [r for r in rows if r["window"] == w]
        if not sub:
            continue
        cov = briers = 0.0
        for r in sub:
            _med, p10, p90, pb = apply_calibration(r, k, g)
            cov += (p10 <= r["fwd"] <= p90)
            briers += (pb - (1.0 if r["below"] else 0.0)) ** 2
        n = len(sub)
        out[w] = {"n": n, "coverage": cov / n, "brier": briers / n}
    return out


def main():
    rows = load_rows()
    fit = [r for r in rows if r["date"] <= FIT_END]
    oos = [r for r in rows if r["date"] > FIT_END]
    print(f"rows: fit={len(fit)//3 if fit else 0}×3窗  oos={len(oos)//3 if oos else 0}×3窗")
    if not fit or not oos:
        print("数据不足，先跑 jobs/path_review.py --recompute-weekly-since 2005-01-01")
        return

    # 网格搜索（fit 时代）
    best, best_loss = None, 1e9
    for k in K_GRID:
        for g in G_GRID:
            ev = evaluate(fit, k, g)
            loss = (sum(abs(x["coverage"] - TARGET) for x in ev.values())
                    + float(np.mean([x["brier"] for x in ev.values()])))
            if loss < best_loss:
                best, best_loss = (k, g), loss
    k, g = best
    print(f"\nfit 最优: shrinkage_k={k}, band_gamma={g}（loss={best_loss:.4f}）")

    def show(tag, rows_, kk, gg):
        ev = evaluate(rows_, kk, gg)
        print(f"\n  [{tag}] k={kk} γ={gg}")
        for w, x in ev.items():
            print(f"    {w}: 覆盖 {x['coverage']:.0%}  Brier {x['brier']:.4f}  (n={x['n']})")
        return ev

    show("fit 未校准", fit, 0, 1.0)
    show("fit 校准后", fit, k, g)
    raw_oos = show("OOS 未校准", oos, 0, 1.0)
    cal_oos = show("OOS 校准后", oos, k, g)

    # 预注册验收
    in_band = sum(1 for w in WINDOWS
                  if w in cal_oos and 0.75 <= cal_oos[w]["coverage"] <= 0.85)
    brier_ok = all(cal_oos[w]["brier"] <= raw_oos[w]["brier"] + 1e-9
                   for w in WINDOWS if w in cal_oos)
    verdict = "PASS" if (in_band >= 2 and brier_ok) else "FAIL"
    print(f"\n预注册验收：OOS 覆盖落 [75%,85%] 的窗数 = {in_band}/3（需≥2），"
          f"Brier 全窗不劣化 = {brier_ok}")
    print(f"=> {verdict}"
          + ("：可写入 defaults.yaml path 节" if verdict == "PASS"
             else "：保持禁用，结论落文档"))


if __name__ == "__main__":
    main()
