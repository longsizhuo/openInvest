"""Phase A 信号存在性评分 — 确定性，预注册判定。

预注册标准（跑前写死，见 docs 计划）：
- 主口径：30d 方向命中率（仅统计 stance≠neutral 的样本），Wilson 95% CI
- 分析师"有信号" = 其命中率 CI 下界 > max(①同子集方向基率, ③机械映射点估计)
  ——①在分析师自己表态的子集上算（同分母 paired），赢不了"无脑猜多数方向"
  或赢不了自己输入数据的机械映射，都不算 LLM 增量；news 无③ → 只比①
  （2026-06-11 全量跑之前收紧定稿）
- 辅助口径：60d 命中率、coverage（非 neutral 占比）、与基线在共同样本上的
  paired 对比、按资产分解
- 基线②（生产概率表方向）含全样本前视，仅作参考不进判定

输出 markdown 表 + 每分析师 PASS/FAIL。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import os as _os
REPORTS = ROOT / _os.getenv("INVEST_TA_OUT", "data/2022_mimo-flash.jsonl")
HORIZONS = (30, 60)
_PRICES = {}


def load_prices(path: Path) -> None:
    """Frozen long-form closes; never download or read a personal market DB."""
    df = pd.read_csv(path)
    if not {"date", "symbol", "close"}.issubset(df.columns):
        raise ValueError("prices CSV requires date,symbol,close columns")
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["close"] = pd.to_numeric(df["close"], errors="raise")
    if df.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate (date,symbol) prices")
    if not np.isfinite(df["close"]).all() or (df["close"] <= 0).any():
        raise ValueError("prices must be finite and positive")
    _PRICES.clear()
    for symbol, sub in df.groupby("symbol"):
        _PRICES[symbol] = sub.set_index("date")["close"].sort_index()


def _fwd_return(symbol: str, date: str, days: int) -> Optional[float]:
    if symbol not in _PRICES:
        raise ValueError(f"prices CSV missing required symbol: {symbol}")
    s = _PRICES[symbol]
    idx = s.index
    ts = pd.Timestamp(date)
    i = idx.searchsorted(ts, side="right") - 1   # 决策日当天或之前最近收盘
    if i < 0:
        return None
    j = idx.searchsorted(ts + pd.Timedelta(days=days), side="left")
    if j >= len(idx):
        return None
    return float(s.iloc[j] / s.iloc[i] - 1)


def wilson_ci(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def _hit(stance: str, fwd: float) -> Optional[bool]:
    if stance == "bullish":
        return fwd > 0
    if stance == "bearish":
        return fwd < 0
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen TA Gate replay (no LLM / personal DB)")
    parser.add_argument("--reports", type=Path, default=REPORTS)
    parser.add_argument("--prices", type=Path, required=True,
                        help="frozen CSV: date,symbol,close; same adjustment convention as original")
    args = parser.parse_args()
    try:
        load_prices(args.prices)
        rows = [json.loads(l) for l in args.reports.open() if l.strip()]
        if not rows:
            raise ValueError("empty report file")
        required = {"key", "date", "symbol", "analyst", "stance", "det_stance"}
        if any(not required.issubset(r) for r in rows):
            raise ValueError("report missing required fields")
        if any(r["stance"] not in ("bullish", "bearish", "neutral", None) for r in rows):
            raise ValueError("invalid stance")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for path in (args.reports, args.prices):
        print(f"input {path.name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    # key 去重（断点续跑/补漏并发的双写护栏，首条为准）
    seen, uniq = set(), []
    for r in rows:
        if r["key"] not in seen:
            seen.add(r["key"])
            uniq.append(r)
        elif next(x for x in uniq if x["key"] == r["key"]) != r:
            raise ValueError(f"conflicting duplicate report key: {r['key']}")
    rows = uniq
    df = pd.DataFrame(rows)
    print(f"报告 {len(df)} 条，缺 STANCE {int(df['stance'].isna().sum())} 条")
    for h in HORIZONS:
        df[f"fwd_{h}"] = [
            _fwd_return(r.symbol, r.date, h) for r in df.itertuples()
        ]

    lines: List[str] = []
    verdicts: Dict[str, str] = {}
    for analyst in sorted(df["analyst"].unique()):
        sub = df[df["analyst"] == analyst].dropna(subset=["fwd_30"])
        n_all = len(sub)

        # LLM stance
        act = sub[sub["stance"].isin(["bullish", "bearish"])]
        # ①方向基率：在分析师自己表态的子集上算（同分母 paired 才公平）
        if len(act):
            up_share = float((act["fwd_30"] > 0).mean())
            down_share = float((act["fwd_30"] < 0).mean())
            base1 = max(up_share, down_share)
        else:
            base1 = float("nan")
        hits = [_hit(r.stance, r.fwd_30) for r in act.itertuples()]
        k, n = sum(bool(x) for x in hits), len(hits)
        hr = k / n if n else float("nan")
        lo, hi_ = wilson_ci(k, n)

        # 基线③（det_stance；news 全 None → 用 ①）
        det = sub[sub["det_stance"].isin(["bullish", "bearish"])]
        if len(det):
            dhits = [_hit(r.det_stance, r.fwd_30) for r in det.itertuples()]
            dk, dn = sum(bool(x) for x in dhits), len(dhits)
            base3 = dk / dn if dn else base1
            base3_label = f"det机械映射 {base3:.1%} (n={dn})"
        else:
            base3 = base1
            base3_label = f"①方向基率 {base3:.1%}"

        # 60d 辅助
        act60 = act.dropna(subset=["fwd_60"])
        h60 = [_hit(r.stance, r.fwd_60) for r in act60.itertuples()]
        hr60 = (sum(bool(x) for x in h60) / len(h60)) if h60 else float("nan")

        # 共同样本 paired（LLM 与 det 都非 neutral 的行）
        both = sub[sub["stance"].isin(["bullish", "bearish"])
                   & sub["det_stance"].isin(["bullish", "bearish"])]
        agree = float((both["stance"] == both["det_stance"]).mean()) if len(both) else float("nan")

        bar = max(b for b in (base1, base3) if not math.isnan(b)) if n else float("nan")
        ok = (not math.isnan(lo)) and lo > bar
        verdicts[analyst] = ("PASS" if ok else "FAIL") if n else "INSUFFICIENT_DATA"
        lines.append(
            f"| {analyst} | {n}/{n_all} ({n/n_all if n_all else 0:.0%}) | {hr:.1%} "
            f"[{lo:.1%},{hi_:.1%}] | {hr60:.1%} | {base1:.1%} | {base3_label} "
            f"| {agree if not math.isnan(agree) else float('nan'):.0%} | **{verdicts[analyst]}** |"
        )
        # 按资产分解
        for sym in sorted(sub["symbol"].unique()):
            a2 = act[act["symbol"] == sym]
            h2 = [_hit(r.stance, r.fwd_30) for r in a2.itertuples()]
            if h2:
                lines.append(
                    f"|   └ {sym} | {len(h2)} | "
                    f"{sum(bool(x) for x in h2)/len(h2):.1%} | | | | | |")

    print("\n| 分析师 | 表态数/总数(coverage) | 30d命中 [95%CI] | 60d命中 "
          "| ①基率 | ③基线 | 与③一致率 | 判定 |")
    print("|---|---|---|---|---|---|---|---|")
    for l in lines:
        print(l)
    print("\n预注册 Gate：CI 下界 > max(①同子集基率, ③机械映射) → PASS")
    print("注意：coverage=表态比例，不是预测区间覆盖率；Wilson 未修正重叠窗口。")
    print("③沿用原 Gate 的自身表态子集，非共同样本检验；不据此声称统计显著。")
    print("Gate 结果:", verdicts)
    n_pass = sum(1 for v in verdicts.values() if v == "PASS")
    print(f"\n=> {'进入 Phase B（只集成 PASS 的分析师）' if n_pass else '不跑 Phase B，结论落文档'}")


if __name__ == "__main__":
    main()
