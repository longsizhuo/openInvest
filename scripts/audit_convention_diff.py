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

    print(f"=== 防御 flag 差异：730(生产) vs 504(canonical) · 防御线 {defense_q:.0%} ===")
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
        rows.append({
            "date": str(d.date()), "vix_db_value": round(v, 2),
            "vix_value_source": "transcript冻结" if has_ts else "当前DB(可能被修订)",
            "q_730_prod": round(q730, 3), "q_504_canon": round(q504, 3),
            "flag_730": int(f730), "flag_504": int(f504),
            "defense_flips": int(f730 != f504),
        })
    flips = [r for r in rows if r["defense_flips"]]
    print(f"  近 {args.recent} 交易日：防御 flag 翻转 {len(flips)} 天（v1 错报 33，那是全量基线）")
    print(f"  {'日期':<12}{'VIX':>6}{'730分位':>8}{'504分位':>8}  方向")
    for r in flips:
        arrow = ("730=on→504=off(原拦现放)" if r["flag_730"]
                 else "730=off→504=on(原放现拦)")
        print(f"  {r['date']:<12}{r['vix_db_value']:>6.1f}"
              f"{r['q_730_prod']:>7.0%}{r['q_504_canon']:>8.0%}  {arrow}")

    out = ROOT / args.csv
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  每日明细 CSV（全 {len(rows)} 天，可独立对照）：{out}")
    print("  列：date / vix_db_value / vix_value_source / q_730_prod / q_504_canon / "
          "flag_730 / flag_504 / defense_flips")


if __name__ == "__main__":
    main()
