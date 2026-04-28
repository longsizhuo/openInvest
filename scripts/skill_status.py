"""Skill helper: 综合状态快照（持仓 + 实时价 + 浮盈）

输出 JSON，给 Claude 读。这是用户问 "我现在怎么样" 的标准入口。
"""
from __future__ import annotations

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
    store = MemoryStore()
    user = store.read("user")
    portfolio = store.read("portfolio")
    strategy = store.read("strategy")

    cash_cny = float(portfolio.get("cash_cny", 0))
    aud_cash = float(portfolio.get("aud_cash", 0))
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    gold_grams = float(portfolio.get("gold_grams", 0))
    gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0))

    ndq_price = _safe_close("NDQ.AX")
    audcny = _safe_close("AUDCNY=X")
    gold_snap = get_gold_snapshot(offset_pct=0.0)

    gold_now = gold_snap.spot_cny_per_gram if gold_snap else 0.0
    gold_value = gold_now * gold_grams
    gold_pnl = (gold_now - gold_avg) * gold_grams if gold_avg else 0.0
    gold_pnl_pct = ((gold_now / gold_avg) - 1) * 100 if gold_avg > 0 else 0.0

    ndq_value_aud = ndq_shares * ndq_price
    ndq_value_cny = ndq_value_aud * audcny

    total_assets_cny = cash_cny + aud_cash * audcny + ndq_value_cny + gold_value

    out = {
        "as_of": gold_snap.gold_usd_per_oz if gold_snap else None,
        "user": {
            "name": user.get("display_name") if user else "unknown",
            "risk_tolerance": user.get("risk_tolerance") if user else None,
        },
        "cash": {
            "cny": round(cash_cny, 2),
            "aud": round(aud_cash, 2),
            "aud_in_cny": round(aud_cash * audcny, 2),
        },
        "ndq": {
            "shares": ndq_shares,
            "price_aud": round(ndq_price, 2),
            "value_aud": round(ndq_value_aud, 2),
            "value_cny": round(ndq_value_cny, 2),
        },
        "gold": {
            "grams": gold_grams,
            "avg_cost_cny_per_gram": gold_avg,
            "now_cny_per_gram": round(gold_now, 2),
            "value_cny": round(gold_value, 2),
            "pnl_cny": round(gold_pnl, 2),
            "pnl_pct": round(gold_pnl_pct, 2),
        },
        "total_assets_cny": round(total_assets_cny, 2),
        "fx": {"audcny": round(audcny, 4)},
        "live_prices": {
            "gold_usd_per_oz": gold_snap.gold_usd_per_oz if gold_snap else None,
            "usdcny": gold_snap.usdcny_rate if gold_snap else None,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
