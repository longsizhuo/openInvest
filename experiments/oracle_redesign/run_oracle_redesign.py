"""oracle_redesign — 让 ACCUMULATE / TRIM 数学上可胜出的打标函数重设计（ADR-007 重开前置）

背景（ADR-007 判死刑的根因）
============================
path_c oracle = "标 forward-30d 组合终值最大的 verdict"，终值对仓位是**线性**的：
U(a) = a · fwd。线性 ⇒ 最优解永远在角点（满仓 BUY / 清到底 SELL / 不动 HOLD），
ACCUMULATE(+0.5) 与 TRIM(-0.1) **数学上永远不可能**成为单样本最优——标签空间先天
塌缩成 3 档，与喂什么数据无关。

本实验验证两个候选修复，全程纯机械计算（无 LLM 参与 ⇒ 无 ADR-022 记忆污染问题，
可用全部历史）：

- **path_d（线性回撤惩罚 + 交易成本）**：U(a) = a·(fwd − λ·|mdd|) − c·|Δa|。
  预期**仍然塌缩**（对 a 仍线性，角点解；成本只造出 HOLD 带）——收录进来是为了
  堵住"加个回撤惩罚不就好了"的直觉：线性惩罚救不了，塌缩根因是线性本身。
- **path_r（均值-方差效用 + 交易成本）**：U(a) = a·μ − (γ/2)·a²·σ² − c·|Δa|。
  二次风险项让内点解存在：f* = μ/(γσ²)，μ 相对 σ² 小时半仓（ACCUMULATE）胜过
  满仓；轻度负 μ 时小减仓（TRIM）胜过清仓。理论上五档都能活。

成功判据：某个 sweep 配置下五档标签占比**各 ≥ 3%**，且分档随 (μ, σ, 起始仓位)
的走向符合金融直觉（报告里给切片验证）。

数据口径与 build_dspy_trainset_v2 对齐：同 10 symbol、每 4 个交易日采样、
5 档起始仓位；周期拉长到 2016 起（跨 regime，机械打标不怕污染）。

用法：
    uv run --no-sync python experiments/oracle_redesign/run_oracle_redesign.py
    # 产出 results.json + REPORT.md；yfinance 日线缓存在 cache/（不进 git）
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)

# ---- 口径对齐 build_dspy_trainset_v2 ----
SYMBOLS = [
    "NDQ.AX", "GC=F", "^GSPC", "QQQ", "AAPL",
    "TSLA", "BTC-USD", "TLT", "IWM", "EEM",
]
START = "2016-01-01"
SAMPLE_EVERY_N = 4
ASSET_PCTS = [0.00, 0.25, 0.50, 0.75, 1.00]
FWD_DAYS = 21          # 30 自然日 ≈ 21 交易日
TRAIL_DAYS = 60        # 尾窗算 σ

ALLOC_MAP = {"BUY": 1.00, "ACCUMULATE": 0.50, "HOLD": 0.00, "TRIM": -0.10, "SELL": -0.30}
# 梯子 v2（ADR-007 判决第 3 条的"重新设计 alloc 模型"路径）：拉开减仓档间距——
# TRIM=-0.1 与 SELL=-0.3 太近，f* 落进 TRIM 波段的概率天然小；SELL 改为全退出
# 也更贴 verdict 语义。⚠️ 换梯子=改 verdict 的仓位语义，是产品决策，采纳前须过用户。
ALLOC_MAP_V2 = {"BUY": 1.00, "ACCUMULATE": 0.50, "HOLD": 0.00, "TRIM": -0.30, "SELL": -1.00}
VERDICT_ORDER = ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL")
EPS = 1e-9

# sweep 网格
PATH_R_GAMMAS = [2.0, 5.0, 10.0, 20.0]
PATH_D_LAMBDAS = [0.3, 0.6, 1.0]
COSTS = [0.001, 0.003]   # 单边交易成本（组合权重每换手 1 需付 c）


def _new_asset(asset_pct: float, alloc: float) -> float:
    """与 path_c 完全一致的仓位映射"""
    if alloc > 0:
        return asset_pct + (1.0 - asset_pct) * alloc
    if alloc < 0:
        return asset_pct * (1 + alloc)
    return asset_pct


def _argmax_hold_tiebreak(utils: Dict[str, float]) -> str:
    """HOLD 并列优先（与 path_c tiebreaker 同约定：最少 transaction、最 robust）"""
    max_val = max(utils.values())
    if abs(utils["HOLD"] - max_val) <= EPS:
        return "HOLD"
    return max(utils, key=utils.get)


# ---------- 三个 oracle ----------

def path_c(asset_pct: float, fwd: float) -> str:
    """控制组：终值 argmax（ADR-007 原版，已知塌缩 3 档）。fwd 为小数收益。"""
    utils = {v: _new_asset(asset_pct, a) * fwd for v, a in ALLOC_MAP.items()}
    return _argmax_hold_tiebreak(utils)


def path_d(asset_pct: float, fwd: float, mdd: float, lam: float, cost: float) -> str:
    """线性回撤惩罚 + 交易成本。mdd 为窗口内最大回撤（小数，≤0）。
    预期仍角点解——对 a 线性；放这里是为了实证"线性惩罚救不了塌缩"。"""
    mu_t = fwd - lam * abs(mdd)
    utils = {}
    for v, a in ALLOC_MAP.items():
        na = _new_asset(asset_pct, a)
        utils[v] = na * mu_t - cost * abs(na - asset_pct)
    return _argmax_hold_tiebreak(utils)


def path_r(asset_pct: float, fwd: float, sigma30: float, gamma: float, cost: float,
           alloc_map: Dict[str, float] = ALLOC_MAP) -> str:
    """均值-方差效用 + 交易成本：U(a) = a·μ − (γ/2)·a²·σ² − c·|Δa|。
    二次项造出内点解 f* = μ/(γσ²) ⇒ ACCUMULATE / TRIM 在中等信号强度下可胜出。"""
    var = sigma30 * sigma30
    utils = {}
    for v, a in alloc_map.items():
        na = _new_asset(asset_pct, a)
        utils[v] = na * fwd - 0.5 * gamma * na * na * var - cost * abs(na - asset_pct)
    return _argmax_hold_tiebreak(utils)


def path_v3(asset_pct: float, fwd: float, sigma30: float, gamma: float, cost: float) -> str:
    """计算式仓位（无任何写死百分比）：verdict = 对计算结果的语言描述。

    f* = μ/(γσ²)（均值-方差最优仓位）；交易成本把 f* 向现仓位拉回一个
    盈亏平衡楔子 c/(γσ²)——落在楔子内 = 调仓收益盖不过手续费 = HOLD。
    - 成本修正后仍 ≥1（顶到上限）→ BUY（打满）
    - 成本修正后 ≤0（落到下限）→ SELL（清仓）
    - 区间内上调 → ACCUMULATE / 区间内下调 → TRIM（量 = 计算出的 Δ，非固定档）
    仅剩常数是物理量：γ（风险厌恶，挂用户画像/标定）与 c（真实手续费）。"""
    var = sigma30 * sigma30
    if var <= 0:
        return "HOLD"
    f_star = fwd / (gamma * var)
    wedge = cost / (gamma * var)
    if abs(f_star - asset_pct) <= wedge:
        return "HOLD"
    f_c = f_star - (wedge if f_star > asset_pct else -wedge)
    # 边界语义：目标方向上已无动作空间 = HOLD（空仓无可卖 / 满仓无可买）
    if f_c >= 1.0:
        return "HOLD" if asset_pct >= 1.0 - EPS else "BUY"
    if f_c <= 0.0:
        return "HOLD" if asset_pct <= EPS else "SELL"
    return "ACCUMULATE" if f_c > asset_pct else "TRIM"


def path_k(asset_pct: float, fwd: float, sigma30: float, gamma: float,
           alloc_map: Dict[str, float] = ALLOC_MAP) -> str:
    """Kelly-snap：f* = μ/(γσ²) 截到 [0,1]，标签 = 落点最近的 verdict（Voronoi 分带）。
    与 argmax-U 解耦角点主导；无成本项，并列时 HOLD 优先。"""
    var = sigma30 * sigma30
    if var <= 0:
        return "HOLD"
    f_star = min(1.0, max(0.0, fwd / (gamma * var)))
    best_v, best_d = "HOLD", abs(_new_asset(asset_pct, 0.0) - f_star)
    for v, a in alloc_map.items():
        na = _new_asset(asset_pct, a)
        d = abs(na - f_star)
        if d < best_d - EPS:
            best_v, best_d = v, d
    return best_v


# ---------- 数据 ----------

def load_history(symbol: str) -> pd.DataFrame:
    """yfinance 日线（auto_adjust，与 v2 builder 同口径），csv 缓存避免重复拉取"""
    cache_f = CACHE / f"{symbol.replace('=', '_').replace('^', '_').replace('/', '_')}.csv"
    if cache_f.exists():
        df = pd.read_csv(cache_f, index_col=0, parse_dates=True)
        if len(df) > 200:
            return df
    df = yf.Ticker(symbol).history(start=START, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance 空返回：{symbol}")
    df = df[["Close"]].dropna()
    df.to_csv(cache_f)
    return df


def build_samples(symbol: str) -> List[Dict]:
    """每 4 个交易日一个决策点：fwd 21 交易日收益 / 窗口内 MDD / 尾窗 σ(30d 尺度)"""
    df = load_history(symbol)
    close = df["Close"].to_numpy(dtype=float)
    dates = df.index
    rets = np.diff(np.log(close))
    out: List[Dict] = []
    for i in range(TRAIL_DAYS, len(close) - FWD_DAYS, SAMPLE_EVERY_N):
        window = close[i: i + FWD_DAYS + 1]
        fwd = window[-1] / window[0] - 1.0
        running_max = np.maximum.accumulate(window)
        mdd = float(np.min(window / running_max - 1.0))          # ≤ 0
        sigma30 = float(np.std(rets[i - TRAIL_DAYS: i], ddof=1) * np.sqrt(FWD_DAYS))
        out.append({
            "symbol": symbol,
            "date": str(dates[i].date()),
            "fwd": float(fwd),
            "mdd": mdd,
            "sigma30": sigma30,
        })
    return out


# ---------- 统计 ----------

def dist(labels: List[str]) -> Dict[str, float]:
    n = len(labels)
    c = Counter(labels)
    return {v: round(100.0 * c.get(v, 0) / n, 2) for v in VERDICT_ORDER}


def all_alive(d: Dict[str, float], floor: float = 3.0) -> bool:
    return all(d[v] >= floor for v in VERDICT_ORDER)


def main() -> None:
    print(f"拉取/读取 {len(SYMBOLS)} 个 symbol 日线（cache: {CACHE}）...")
    samples: List[Dict] = []
    for s in SYMBOLS:
        try:
            rows = build_samples(s)
            samples.extend(rows)
            print(f"  {s:8s} {len(rows)} 个决策点")
        except Exception as e:  # noqa: BLE001 单 symbol 失败不连坐（与 news_sources 同约定）
            print(f"  {s:8s} 跳过：{e}")
    n_pts = len(samples)
    grid = [(sm, ap) for sm in samples for ap in ASSET_PCTS]
    print(f"决策点 {n_pts} × 起始仓位 {len(ASSET_PCTS)} = {len(grid)} 样本\n")

    results: Dict = {"n_decision_points": n_pts, "n_samples": len(grid),
                     "symbols": SYMBOLS, "start": START,
                     "configs": []}

    # 控制组 path_c
    labels_c = [path_c(ap, sm["fwd"]) for sm, ap in grid]
    d_c = dist(labels_c)
    results["configs"].append({"oracle": "path_c", "params": {}, "dist": d_c,
                               "all_alive": all_alive(d_c)})
    print(f"path_c(控制组)                    {d_c}")

    # path_d sweep（预期仍塌缩）
    for lam in PATH_D_LAMBDAS:
        for cost in COSTS:
            labels = [path_d(ap, sm["fwd"], sm["mdd"], lam, cost) for sm, ap in grid]
            d = dist(labels)
            results["configs"].append({"oracle": "path_d",
                                       "params": {"lambda": lam, "cost": cost},
                                       "dist": d, "all_alive": all_alive(d)})
            print(f"path_d λ={lam:<4} c={cost:<6}          {d}")

    # path_r / path_r2 / path_k sweep（候选）
    best = None

    def consider(entry: Dict, labels: List[str]) -> None:
        nonlocal best
        if not entry["all_alive"]:
            return
        p = np.array([entry["dist"][v] for v in VERDICT_ORDER]) / 100.0
        p = p[p > 0]
        ent = float(-(p * np.log(p)).sum())
        if best is None or ent > best[1]:
            best = (entry, ent, labels)

    for gamma in PATH_R_GAMMAS:
        for cost in COSTS:
            labels = [path_r(ap, sm["fwd"], sm["sigma30"], gamma, cost) for sm, ap in grid]
            d = dist(labels)
            entry = {"oracle": "path_r", "params": {"gamma": gamma, "cost": cost},
                     "dist": d, "all_alive": all_alive(d)}
            results["configs"].append(entry)
            print(f"path_r  γ={gamma:<4} c={cost:<6}         {d}  {'✅五档全活' if entry['all_alive'] else ''}")
            consider(entry, labels)

    for gamma in PATH_R_GAMMAS:
        for cost in COSTS:
            labels = [path_r(ap, sm["fwd"], sm["sigma30"], gamma, cost, ALLOC_MAP_V2)
                      for sm, ap in grid]
            d = dist(labels)
            entry = {"oracle": "path_r2(ladder_v2)", "params": {"gamma": gamma, "cost": cost},
                     "dist": d, "all_alive": all_alive(d)}
            results["configs"].append(entry)
            print(f"path_r2 γ={gamma:<4} c={cost:<6}         {d}  {'✅五档全活' if entry['all_alive'] else ''}")
            consider(entry, labels)

    for gamma in PATH_R_GAMMAS:
        for cost in COSTS:
            labels = [path_v3(ap, sm["fwd"], sm["sigma30"], gamma, cost) for sm, ap in grid]
            d = dist(labels)
            entry = {"oracle": "path_v3(computed-alloc)", "params": {"gamma": gamma, "cost": cost},
                     "dist": d, "all_alive": all_alive(d)}
            results["configs"].append(entry)
            print(f"path_v3 γ={gamma:<4} c={cost:<6}         {d}  {'✅五档全活' if entry['all_alive'] else ''}")
            consider(entry, labels)

    for gamma in PATH_R_GAMMAS:
        for map_name, amap in [("ladder_v1", ALLOC_MAP), ("ladder_v2", ALLOC_MAP_V2)]:
            labels = [path_k(ap, sm["fwd"], sm["sigma30"], gamma, amap) for sm, ap in grid]
            d = dist(labels)
            entry = {"oracle": f"path_k({map_name})", "params": {"gamma": gamma},
                     "dist": d, "all_alive": all_alive(d)}
            results["configs"].append(entry)
            print(f"path_k  γ={gamma:<4} {map_name}       {d}  {'✅五档全活' if entry['all_alive'] else ''}")
            consider(entry, labels)

    # 推荐配置的直觉切片验证
    if best is not None:
        entry, ent, labels = best
        results["recommended"] = {**entry, "entropy": round(ent, 4)}
        print(f"\n推荐配置：{entry['oracle']} {entry['params']}（熵 {ent:.3f}）")
        # 切片 1：按 σ 三分位 → 高波动时 ACCUMULATE 份额应高于低波动
        sig = np.array([sm["sigma30"] for sm, _ in grid])
        lab = np.array(labels)
        t1, t2 = np.quantile(sig, [1 / 3, 2 / 3])
        slices = {}
        for name, mask in [("low_vol", sig <= t1),
                           ("mid_vol", (sig > t1) & (sig <= t2)),
                           ("high_vol", sig > t2)]:
            slices[name] = dist(list(lab[mask]))
            print(f"  {name:8s} {slices[name]}")
        results["recommended"]["vol_slices"] = slices
        # 切片 2：按起始仓位 → 满仓时 TRIM/SELL 份额应显著高于空仓
        state_slices = {}
        aps = np.array([ap for _, ap in grid])
        for ap in ASSET_PCTS:
            state_slices[str(ap)] = dist(list(lab[aps == ap]))
        results["recommended"]["state_slices"] = state_slices
    else:
        print("\n⚠️ 没有任何 path_r 配置五档全活——需要扩大 sweep 或换目标函数")

    out_f = HERE / "results.json"
    out_f.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写 {out_f}")


if __name__ == "__main__":
    main()
