"""反事实记账复盘 — 给 interventions.jsonl 回填"如果没拦会怎样"（确定性，零 LLM）。

每条干预记录（确定性规则改写 CIO 裁决）在决策 30/60/90 天后可结算：

    counterfactual_pnl_w = delta_exposure_cny × fwd_return_w

delta_exposure = original_alloc − final_alloc：
- 被拦买入（ACCUMULATE ¥3000 → HOLD 0）：delta=+3000，金价涨则正值=拦截踏空成本
- 被拦减仓（TRIM −¥20000 → HOLD 0）：delta=−20000，金价跌则正值=拦截多亏的钱
  （即"听 CIO 卖掉本可避免的损失"）

口径声明：这是单笔静态反事实（不复利、不考虑反事实后续决策链），用于按 rule
聚合回答"这条拦截规则到底在省钱还是费钱"。样本攒够（每 rule ≥20 条独立干预）
前只展示不下结论。

用法：
    uv run python -m jobs.intervention_review            # 打印汇总
    uv run python -m jobs.intervention_review --jsonl    # 额外落 interventions_scored.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

WINDOWS = (30, 60, 90)


def load_interventions(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or MemoryStore().root / ".dreams" / "interventions.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.open(encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def fwd_return(symbol: str, date: str, days: int) -> Optional[float]:
    """决策日（含）后第 days 个交易日的收盘 / 决策日收盘 − 1。未到期返回 None。"""
    from db.market_store import MarketStore
    df = MarketStore().get_history_df(symbol, days=100000)
    if df is None or df.empty:
        return None
    upto = df.loc[:date]
    if upto.empty:
        return None
    base = float(upto["Close"].iloc[-1])
    after = df.loc[date:]["Close"]
    if len(after) <= days:
        return None
    return float(after.iloc[days]) / base - 1.0


def score(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """逐条回填各窗口反事实损益（未到期窗口为 None）。"""
    scored = []
    for r in rows:
        out = dict(r)
        delta = float(r.get("delta_exposure_cny", 0) or 0)
        for w in WINDOWS:
            f = fwd_return(r["asset"], r["date"], w)
            out[f"fwd_{w}d"] = None if f is None else round(f, 4)
            out[f"counterfactual_pnl_{w}d_cny"] = (
                None if f is None else round(delta * f, 1)
            )
        scored.append(out)
    return scored


def summarize(scored: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 rule 聚合：n、已结算 n、各窗口反事实损益合计。

    正值 = 拦截让用户少赚/多亏了这么多（拦错）；负值 = 拦截避免了这么多损失（拦对）。
    """
    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, **{f"settled_{w}d": 0 for w in WINDOWS},
                 **{f"sum_pnl_{w}d": 0.0 for w in WINDOWS}})
    for r in scored:
        a = agg[r["rule"]]
        a["n"] += 1
        for w in WINDOWS:
            pnl = r.get(f"counterfactual_pnl_{w}d_cny")
            if pnl is not None:
                a[f"settled_{w}d"] += 1
                a[f"sum_pnl_{w}d"] += pnl
    return dict(agg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", action="store_true",
                    help="额外写 interventions_scored.jsonl")
    args = ap.parse_args()

    rows = load_interventions()
    if not rows:
        print("interventions.jsonl 为空——还没有攒到干预记录（这是新机制，从今天起攒）")
        return
    scored = score(rows)
    summ = summarize(scored)
    print(f"干预记录 {len(rows)} 条（{rows[0]['date']} → {rows[-1]['date']}）\n")
    print(f"{'rule':<36}{'n':>4}" + "".join(f"{f'{w}d结算/合计':>16}" for w in WINDOWS))
    for rule, a in sorted(summ.items()):
        cells = "".join(
            f"{a[f'settled_{w}d']:>5}/{a[f'sum_pnl_{w}d']:>+9.0f}" for w in WINDOWS)
        print(f"{rule:<36}{a['n']:>4}{cells}")
    print("\n口径：正=拦错（用户少赚/多亏），负=拦对（避免损失）。"
          "每 rule 独立干预 <20 条前不下结论。")
    if args.jsonl:
        p = MemoryStore().root / ".dreams" / "interventions_scored.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for r in scored:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"已写 {p}")


if __name__ == "__main__":
    main()
