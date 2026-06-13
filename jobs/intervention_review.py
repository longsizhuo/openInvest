"""反事实记账复盘 — 给 interventions.jsonl 回填"如果没拦会怎样"（确定性，零 LLM）。

每条干预记录（确定性规则改写 CIO 裁决）在决策 30/60/90 **日历天**后可结算
（口径同路径分布/概率表，统一走 core.regime_probability.forward_return）：

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

WINDOWS = (30, 60, 90)   # 日历天（非交易日）；JSON key fwd_{w}d / pnl_{w}d_cny


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
    """决策日 → days 个**日历日**后的收益。未到期返回 None。

    口径单一可信源：直接调 core.regime_probability.forward_return（与路径分布、
    概率表逐位一致的日历天口径）。2026-06-13 修漂移前这里曾是交易日口径
    （30≈42 日历天），与 CIO 看的 30d 路径分布跨度对不上。
    """
    from core.regime_probability import forward_return
    return forward_return(symbol, date, days)


def latest_return(symbol: str, date: str) -> Optional[Dict[str, Any]]:
    """决策日收盘 → 最新收盘的收益 + 经过的交易日数（未结算预览用）。"""
    from db.market_store import MarketStore
    df = MarketStore().get_history_df(symbol, days=100000)
    if df is None or df.empty:
        return None
    upto = df.loc[:date]
    if upto.empty:
        return None
    base = float(upto["Close"].iloc[-1])
    after = df.loc[date:]["Close"]
    return {"ret": float(after.iloc[-1]) / base - 1.0, "days": len(after) - 1}


def score(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """逐条回填各窗口反事实损益（未到期窗口为 None）+ 最新收盘浮动预览。"""
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
        lt = latest_return(r["asset"], r["date"])
        out["preview_days"] = lt["days"] if lt else None
        out["preview_pnl_cny"] = round(delta * lt["ret"], 1) if lt else None
        scored.append(out)
    return scored


def _family_of(r: Dict[str, Any]) -> str:
    """取 rule_family（旧行无此字段 → 用 mapper 从 rule 回退推导）。"""
    fam = r.get("rule_family")
    if fam:
        return fam
    from core.committee_runner import rule_family
    return rule_family(r.get("rule", ""))


def summarize(scored: List[Dict[str, Any]], key: str = "rule") -> Dict[str, Any]:
    """按 key（"rule" 细粒度 / "rule_family" 并桶）聚合：n、已结算 n、各窗损益合计。

    正值 = 拦截让用户少赚/多亏了这么多（拦错）；负值 = 拦截避免了这么多损失（拦对）。
    """
    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, **{f"settled_{w}d": 0 for w in WINDOWS},
                 **{f"sum_pnl_{w}d": 0.0 for w in WINDOWS}})
    for r in scored:
        k = _family_of(r) if key == "rule_family" else r.get(key, "?")
        a = agg[k]
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
    print(f"干预记录 {len(rows)} 条（{rows[0]['date']} → {rows[-1]['date']}）\n")

    def _print_table(summ: Dict[str, Any], col: str, width: int = 36) -> None:
        print(f"{col:<{width}}{'n':>4}" + "".join(f"{f'{w}d结算/合计':>16}" for w in WINDOWS))
        for k, a in sorted(summ.items()):
            cells = "".join(
                f"{a[f'settled_{w}d']:>5}/{a[f'sum_pnl_{w}d']:>+9.0f}" for w in WINDOWS)
            print(f"{k:<{width}}{a['n']:>4}{cells}")

    # 主视图：rule_family 并桶（live + reconstructed 同底层规则合并 → 回答"省钱费钱"）
    print("【按规则家族（live+重建并桶）】")
    _print_table(summarize(scored, key="rule_family"), "rule_family")
    print("\n【按细粒度 rule（明细）】")
    _print_table(summarize(scored, key="rule"), "rule")
    # 未结算预览（按最新收盘的浮动反事实——信息参考，不是预注册判定口径）
    prev = defaultdict(float)
    prev_n = defaultdict(int)
    for r in scored:
        if r.get("preview_pnl_cny") is not None:
            prev[r["rule"]] += r["preview_pnl_cny"]
            prev_n[r["rule"]] += 1
    print("\n未结算预览（决策日→最新收盘，浮动口径仅参考）：")
    for rule in sorted(prev):
        print(f"  {rule:<36}{prev_n[rule]:>3} 条  浮动反事实 ¥{prev[rule]:+,.0f}")
    print("\n口径：正=拦错（用户少赚/多亏），负=拦对（避免损失）。"
          "每 rule 独立干预 <20 条前不下结论；正式判定只看 30/60/90d 结算窗。")
    if args.jsonl:
        p = MemoryStore().root / ".dreams" / "interventions_scored.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for r in scored:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"已写 {p}")


if __name__ == "__main__":
    main()
