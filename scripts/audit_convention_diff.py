"""口径差异回算 v2（生产忠实，transcript 校验过）—— 2026-06-13 修正。

v1 的错（已认）：用 days=100000（全量 36 年）当"旧口径"，但生产实际用
get_history_df 默认 **730 行**。v1 的"33 天 / 73%→85% / 全量历史"全是对错误基线算的。

v2 纪律：**transcript 是金标准**（唯一未被事后修订的冻结真相），脚本是被校验的对象。
已用 4 个 transcript 冻结点反向校验，确认生产=730 窗：
    日期      冻结VIX  transcript  504窗  730窗  全量
    6/9       21.4     87%        82%   88%   69%
    6/11      21.4     87%        82%   87%   69%
    5/22      20.7     85%        80%   85%   66%
    6/8       20.7     85%        79%   85%   66%
→ 730 窗逐位复现 transcript；504 差 ~5pp；全量差 ~20pp（v1 用的就是全量，错）。

⚠ **范围限定**：本脚本只建 **VIX 哨兵腿**（INDEP_DEFENSE_FLAG），不建 ATR 腿。生产最终
防御 = VIX腿 OR atr_defense_on。所以"VIX 腿翻转 N 天"是实际防御决策变化的**上界**——
VIX 腿 off→on 当天若 ATR 腿本就触发，最终防御不变。要精确算实际防御变化需逐资产 ATR，
未做。

口径（都只用 ≤D 收盘价，无前视）：
- 730（生产真实）= price_D 在近 730 行里的百分位
- 504（canonical/回测口径）= price_D 在近 504 行里的百分位

⚠ 可复现性上限：无 transcript 的历史日，只能用**当前 DB** 的 VIX 值，而行情数据会被
yfinance 事后修订（实测 6/9 DB=19.9 vs transcript 冻结 21.4）。所以**窗口方法已被
transcript 校验，但无 transcript 日的 per-day VIX 值是近似**——该列标注来源。

用法: uv run python scripts/audit_convention_diff.py [--recent N] [--csv 路径]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from db.market_store import MarketStore  # noqa: E402

W_PROD = 730   # get_history_df 默认 = 生产真实窗口（已 transcript 校验）
W_CANON = 504  # TRADING_DAYS_2Y = 回测/canonical 口径

# transcript 冻结校验点（金标准；脚本必须复现这些才可信）
TRANSCRIPT_CHECKS = [
    ("2026-06-09", 21.4, 0.87), ("2026-06-11", 21.4, 0.87),
    ("2026-05-22", 20.7, 0.85), ("2026-06-08", 20.7, 0.85),
]


def _pct(series, value, window):
    seg = series.tail(window) if window else series
    return float((seg <= value).mean())


def validate(vix) -> bool:
    """对 transcript 冻结点反向校验：730 窗能否复现冻结分位（容差 2pp）。"""
    print("=== transcript 金标准校验（730 窗须复现冻结分位）===")
    print("  ⚠ 校验集 4 点全在 2026-05-22~06-11（~3 周）——730 窗忠实性只在该段被证；")
    print("     2025 年的翻转日无 transcript、数据修订更多，属外推（CSV 标 vix_value_source）。\n")
    ok = True
    for d, vval, target in TRANSCRIPT_CHECKS:
        sub = vix[vix.index <= d]
        q730 = _pct(sub, vval, W_PROD)
        passed = abs(q730 - target) <= 0.02
        ok &= passed
        print(f"  {d}: 冻结VIX={vval} transcript={target:.0%}  "
              f"脚本730窗={q730:.0%}  {'✓' if passed else '✗ 缺口！'}")
    print(f"  → {'全部通过，窗口方法可信' if ok else '未通过，不许报数'}\n")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=250)
    ap.add_argument("--csv", default="scripts/audit_convention_diff_result.csv")
    args = ap.parse_args()
    ms = MarketStore()
    defense_q = load_config().sentiment.vix_defense_quantile

    vix = ms.get_history_df("^VIX", days=100000)["Close"].dropna()
    if not validate(vix):
        print("校验未过，停止——不输出可能不忠实的差异数字。")
        return

    print(f"=== VIX 腿分位翻转：730(生产) vs 504(canonical) · 防御线 {defense_q:.0%} ===")
    print("  ⚠ 这是 **VIX 哨兵腿** 的分位翻转，不是最终防御决策。生产最终防御 =")
    print("     INDEP_DEFENSE_FLAG(VIX腿) OR atr_defense_on(ATR腿，本脚本未建模)。")
    print("     VIX 腿 off→on 的日子若当天 ATR 腿本就触发，实际防御决策不变。")
    print("     所以下面的天数是 **VIX 腿翻转的上界，≥实际防御变化**。\n")
    rows = []
    recent = vix.index[-args.recent:]
    for d in recent:
        sub = vix[vix.index <= d]
        if len(sub) < W_CANON:
            continue
        v = float(sub.iloc[-1])
        q730 = _pct(sub, v, W_PROD)
        q504 = _pct(sub, v, W_CANON)
        f730 = q730 >= defense_q
        f504 = q504 >= defense_q
        has_ts = any(c[0] == str(d.date()) for c in TRANSCRIPT_CHECKS)
        # 边界标注：两分位任一贴着防御线(±2pp 内)→ 翻转落在数据修订噪声里，不稳健
        near = min(abs(q730 - defense_q), abs(q504 - defense_q)) <= 0.02
        rows.append({
            "date": str(d.date()), "vix_db_value": round(v, 2),
            "vix_value_source": "transcript冻结" if has_ts else "当前DB(可能被修订)",
            "defense_q": defense_q, "w_prod": W_PROD, "w_canon": W_CANON,
            "q_730_prod": round(q730, 3), "q_504_canon": round(q504, 3),
            "flag_730": int(f730), "flag_504": int(f504),
            "vix_leg_flip": int(f730 != f504),
            "near_boundary_noise": int(near),
        })
    if not rows:
        print("  无足够历史(<504行)，无输出。")
        return
    flips = [r for r in rows if r["vix_leg_flip"]]
    fragile = sum(r["near_boundary_noise"] for r in flips)
    print(f"  近 {args.recent} 交易日：VIX 腿翻转 {len(flips)} 天"
          f"（其中 {fragile} 天贴防御线±2pp，落在数据修订噪声里、具体哪天不稳健）")
    print(f"  {'日期':<12}{'VIX':>6}{'730':>6}{'504':>6}  方向  边界?")
    for r in flips:
        arrow = ("730=on→504=off" if r["flag_730"] else "730=off→504=on")
        edge = "⚠贴线" if r["near_boundary_noise"] else ""
        print(f"  {r['date']:<12}{r['vix_db_value']:>6.1f}"
              f"{r['q_730_prod']:>6.0%}{r['q_504_canon']:>6.0%}  {arrow}  {edge}")

    out = ROOT / args.csv
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  每日明细 CSV（全 {len(rows)} 天，含阈值/窗口/边界列，可独立对照）：{out}")


if __name__ == "__main__":
    main()
