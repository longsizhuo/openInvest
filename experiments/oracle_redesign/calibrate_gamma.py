"""calibrate_gamma — path_v3 定稿前的两个收尾实验(REPORT.md"下一步"1、2)

Part A 不确定性楔子(path_v4 = path_v3 + 显著性闸):
    hindsight 的 μ 让 HOLD 带偏窄(教材过度活跃)。live 的 μ 是噪声估计——理性
    交易者要求信号先过显著性:|μ| ≤ κ·σ30 视为"无视图",无视图不交易(HOLD,
    留在原仓位;不是清仓——无证据支持任何调仓方向)。κ 是显著性水平(z 分数),
    与 γ 同类的风险政策量,不是新的魔法梯子数。sweep κ 看 HOLD 占比拨盘。

Part B γ 标定(walk-forward 组合回测):
    对每个 γ×κ:每 4 交易日按 path_v4 的**计算目标仓位**调仓(计算式语义,直接
    用 f_c 连续值),日频复利,扣交易成本,对比同资产 buy-and-hold(ADR-022 §7:
    只评择时,锚定同资产)。⚠️ 标签含 hindsight ⇒ 结果是**上界/一致性检验**,
    不是可报业绩——用途:验证按标签行动跨期组合是连贯的(v3 审计同性质),并在
    风险调整指标(Sharpe/MDD)上挑 γ。

用法:
    uv run --no-sync python experiments/oracle_redesign/calibrate_gamma.py
    # 复用 run_oracle_redesign 的取数与缓存;产出 calibration.json + stdout 表
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_oracle_redesign import (  # noqa: E402
    ASSET_PCTS, EPS, FWD_DAYS, SAMPLE_EVERY_N, SYMBOLS, TRAIL_DAYS,
    VERDICT_ORDER, all_alive, build_samples, dist, load_history,
)

COST = 0.003
GAMMAS = [2.0, 5.0, 10.0, 20.0]
KAPPAS = [0.0, 0.25, 0.5, 1.0]     # κ=0 即退化回 path_v3


def path_v4(a0: float, fwd: float, sigma30: float,
            gamma: float, cost: float, kappa: float) -> Tuple[str, float]:
    """返回 (verdict, 目标仓位)。计算式语义:目标是连续值,verdict 是语言描述。"""
    var = sigma30 * sigma30
    if var <= 0:
        return "HOLD", a0
    if abs(fwd) <= kappa * sigma30:          # 显著性闸:无视图不交易
        return "HOLD", a0
    f_star = fwd / (gamma * var)
    wedge = cost / (gamma * var)
    if abs(f_star - a0) <= wedge:            # 成本闸:调仓收益盖不过手续费
        return "HOLD", a0
    f_c = f_star - (wedge if f_star > a0 else -wedge)
    if f_c >= 1.0:
        return ("HOLD", a0) if a0 >= 1.0 - EPS else ("BUY", 1.0)
    if f_c <= 0.0:
        return ("HOLD", a0) if a0 <= EPS else ("SELL", 0.0)
    return ("ACCUMULATE", f_c) if f_c > a0 else ("TRIM", f_c)


# ---------- Part A:κ 拨盘下的标签分布 ----------

def part_a(samples: List[Dict]) -> List[Dict]:
    grid = [(sm, ap) for sm in samples for ap in ASSET_PCTS]
    out = []
    print("Part A — 显著性闸 κ 对标签分布的拨盘(c=0.003)")
    for gamma in (5.0, 10.0):
        for kappa in KAPPAS:
            labels = [path_v4(ap, sm["fwd"], sm["sigma30"], gamma, COST, kappa)[0]
                      for sm, ap in grid]
            d = dist(labels)
            row = {"gamma": gamma, "kappa": kappa, "dist": d, "all_alive": all_alive(d)}
            out.append(row)
            print(f"  γ={gamma:<4} κ={kappa:<5} {d}  {'✅' if row['all_alive'] else '✗五档未全活'}")
    return out


# ---------- Part B:walk-forward γ 标定 ----------

def _metrics(curve: np.ndarray) -> Dict[str, float]:
    rets = np.diff(curve) / curve[:-1]
    total = curve[-1] / curve[0] - 1.0
    sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252)) if np.std(rets) > 0 else 0.0
    running = np.maximum.accumulate(curve)
    mdd = float(np.min(curve / running - 1.0))
    return {"total_pct": round(100 * total, 2), "sharpe": round(sharpe, 3),
            "mdd_pct": round(100 * mdd, 2)}


def walk_forward(symbol: str, gamma: float, kappa: float) -> Dict:
    """从空仓起步,每 4 交易日按 path_v4 目标调仓,日频复利,扣成本;对比 BAH。"""
    df = load_history(symbol)
    close = df["Close"].to_numpy(dtype=float)
    logret = np.diff(np.log(close))
    n = len(close)
    a = 0.0                      # 当前仓位
    v = 1.0                      # 组合净值
    curve = [1.0]
    start = TRAIL_DAYS
    end = n - FWD_DAYS
    for i in range(start, end):
        if (i - start) % SAMPLE_EVERY_N == 0:
            window = close[i: i + FWD_DAYS + 1]
            fwd = window[-1] / window[0] - 1.0
            sigma30 = float(np.std(logret[i - TRAIL_DAYS: i], ddof=1) * np.sqrt(FWD_DAYS))
            _, target = path_v4(a, fwd, sigma30, gamma, COST, kappa)
            if abs(target - a) > EPS:
                v *= 1.0 - COST * abs(target - a)
                a = target
        day_ret = close[i + 1] / close[i] - 1.0
        v *= 1.0 + a * day_ret
        curve.append(v)
    strat = _metrics(np.array(curve))
    bah = _metrics(close[start:end + 1] / close[start])
    return {"symbol": symbol, "strategy": strat, "bah": bah,
            "alpha_pct": round(strat["total_pct"] - bah["total_pct"], 2),
            "sharpe_diff": round(strat["sharpe"] - bah["sharpe"], 3),
            "mdd_improve_pct": round(strat["mdd_pct"] - bah["mdd_pct"], 2)}


def part_b() -> List[Dict]:
    print("\nPart B — walk-forward γ 标定(hindsight 标签 ⇒ 上界/一致性检验,非可报业绩)")
    print(f"{'γ':>5} {'κ':>5} | {'均alpha%':>9} {'胜BAH':>6} {'均Sharpe差':>10} {'均MDD改善pp':>11}")
    out = []
    for gamma in GAMMAS:
        for kappa in (0.0, 0.5, 1.0):
            rows = [walk_forward(s, gamma, kappa) for s in SYMBOLS]
            alphas = [r["alpha_pct"] for r in rows]
            row = {
                "gamma": gamma, "kappa": kappa,
                "mean_alpha_pct": round(float(np.mean(alphas)), 2),
                "beat_bah": f"{sum(1 for x in alphas if x >= 0)}/{len(alphas)}",
                "mean_sharpe_diff": round(float(np.mean([r["sharpe_diff"] for r in rows])), 3),
                "mean_mdd_improve_pp": round(float(np.mean([r["mdd_improve_pct"] for r in rows])), 2),
                "per_symbol": rows,
            }
            out.append(row)
            print(f"{gamma:>5} {kappa:>5} | {row['mean_alpha_pct']:>9} {row['beat_bah']:>6} "
                  f"{row['mean_sharpe_diff']:>10} {row['mean_mdd_improve_pp']:>11}")
    return out


def main() -> None:
    samples: List[Dict] = []
    for s in SYMBOLS:
        try:
            samples.extend(build_samples(s))
        except Exception as e:  # noqa: BLE001
            print(f"{s} 跳过:{e}")
    a = part_a(samples)
    b = part_b()
    out = HERE / "calibration.json"
    out.write_text(json.dumps({"part_a_kappa_dial": a, "part_b_gamma_walkforward": b},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写 {out}")


if __name__ == "__main__":
    main()
