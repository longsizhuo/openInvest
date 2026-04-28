"""Skill helper: what-if P&L 模拟

让 Claude 对话时能直接算"如果黄金涨到 1100 / NDQ 跌到 50 / AUDCNY 升到 5.0
我的资产怎么变"——零 LLM 成本，纯算术。

用法：
    python scripts/skill_what_if.py --gold-price 1100
    python scripts/skill_what_if.py --ndq-price 60 --audcny 5.0
    python scripts/skill_what_if.py --gold-pct +5 --ndq-pct -10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402
from utils.exchange_fee import get_history_data  # noqa: E402
from utils.gold_price import get_gold_snapshot  # noqa: E402


def _safe_close(symbol: str) -> float:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold-price", type=float,
                        help="假设黄金克价 (CNY/g)")
    parser.add_argument("--gold-pct", type=float,
                        help="假设黄金涨跌百分比，例如 +5 或 -3")
    parser.add_argument("--ndq-price", type=float,
                        help="假设 NDQ.AX 价格 (AUD)")
    parser.add_argument("--ndq-pct", type=float,
                        help="假设 NDQ 涨跌百分比")
    parser.add_argument("--audcny", type=float,
                        help="假设 AUD/CNY 汇率")
    args = parser.parse_args()

    store = MemoryStore()
    portfolio = store.read("portfolio")
    if portfolio is None:
        print(json.dumps({"error": "portfolio.md 不存在"}))
        return

    cash_cny = float(portfolio.get("cash_cny", 0))
    aud_cash = float(portfolio.get("aud_cash", 0))
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    gold_grams = float(portfolio.get("gold_grams", 0))
    gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0))

    # 现状
    snap = get_gold_snapshot(offset_pct=0.0)
    cur_gold = snap.spot_cny_per_gram if snap else 1000.0
    cur_ndq = _safe_close("NDQ.AX")
    cur_audcny = _safe_close("AUDCNY=X") or 4.9

    # 应用假设
    new_gold = args.gold_price if args.gold_price else cur_gold
    if args.gold_pct is not None:
        new_gold = cur_gold * (1 + args.gold_pct / 100)

    new_ndq = args.ndq_price if args.ndq_price else cur_ndq
    if args.ndq_pct is not None:
        new_ndq = cur_ndq * (1 + args.ndq_pct / 100)

    new_audcny = args.audcny if args.audcny else cur_audcny

    # 估值
    cur_total = (cash_cny + aud_cash * cur_audcny
                 + ndq_shares * cur_ndq * cur_audcny
                 + gold_grams * cur_gold)

    new_total = (cash_cny + aud_cash * new_audcny
                 + ndq_shares * new_ndq * new_audcny
                 + gold_grams * new_gold)

    delta = new_total - cur_total
    delta_pct = (delta / cur_total) * 100 if cur_total else 0.0

    out = {
        "current": {
            "gold_cny_per_g": round(cur_gold, 2),
            "ndq_aud": round(cur_ndq, 2),
            "audcny": round(cur_audcny, 4),
            "total_cny": round(cur_total, 2),
        },
        "scenario": {
            "gold_cny_per_g": round(new_gold, 2),
            "ndq_aud": round(new_ndq, 2),
            "audcny": round(new_audcny, 4),
            "total_cny": round(new_total, 2),
        },
        "delta_cny": round(delta, 2),
        "delta_pct": round(delta_pct, 2),
        "breakdown": {
            "gold_grams": gold_grams,
            "gold_avg_cost": gold_avg,
            "gold_pnl_at_scenario_cny": round((new_gold - gold_avg) * gold_grams, 2),
            "ndq_shares": ndq_shares,
            "ndq_value_at_scenario_cny": round(ndq_shares * new_ndq * new_audcny, 2),
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
