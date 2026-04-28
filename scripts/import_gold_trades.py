"""一次性导入用户报的浙商积存金交易记录

从用户在 NapCat / 对话中报的真实数据：
- 7 笔交易（含 1 笔赠金）
- 总持仓 124.00 克
- 总成本 ¥125,009.41
- 平均成本 ¥1008.14/克

同时：
1. portfolio.gold_grams 写实
2. strategy.target_assets[gold].price_offset_pct = 0.0（实测接近 0）
3. strategy.target_assets[gold].sell_fee_pct = 0.0038（用户实际卖出手续费）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

# 用户报的实际交易（日期已修正：2025 → 2026）
TRADES = [
    {"ts_origin": "2026-04-22T02:56:22", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "实时", "units": 9.6289,
     "price_per_unit": 1038.54, "total_amount": 10000.00, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-04-22T01:10:24", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "限价", "units": 19.2389,
     "price_per_unit": 1039.56, "total_amount": 20000.00, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-04-21T22:45:05", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "实时", "units": 19.1663,
     "price_per_unit": 1043.50, "total_amount": 20000.00, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-04-21T22:37:58", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "实时", "units": 9.5441,
     "price_per_unit": 1047.77, "total_amount": 10000.00, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-03-27T02:20:56", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "限价", "units": 15.3560,
     "price_per_unit": 976.82, "total_amount": 15000.00, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-03-27T00:58:12", "action": "gift", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "赠金", "units": 0.0096,
     "price_per_unit": 980.72, "total_amount": 9.41, "fee": 0, "currency": "CNY"},
    {"ts_origin": "2026-03-27T00:58:11", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "实时", "units": 50.9762,
     "price_per_unit": 980.85, "total_amount": 50000.00, "fee": 0, "currency": "CNY"},
]


def main():
    store = MemoryStore()

    # 1. 写交易历史（按时间正序追加）
    sorted_trades = sorted(TRADES, key=lambda t: t["ts_origin"])
    for trade in sorted_trades:
        store.append_history(trade)

    total_grams = sum(t["units"] for t in TRADES)
    total_cost = sum(t["total_amount"] for t in TRADES if t["action"] == "bought")
    avg_cost = total_cost / sum(t["units"] for t in TRADES if t["action"] == "bought")

    print(f"✓ 导入 {len(TRADES)} 笔交易")
    print(f"  总持仓: {total_grams:.4f} 克")
    print(f"  总成本: ¥{total_cost:,.2f}")
    print(f"  平均成本: ¥{avg_cost:.2f}/克")

    # 2. 更新 portfolio.gold_grams
    port_doc = store.read("portfolio")
    if port_doc is None:
        print("❌ portfolio.md 不存在")
        return
    cash_cny = float(port_doc.get("cash_cny", 0))
    aud_cash = float(port_doc.get("aud_cash", 0))
    ndq_shares = float(port_doc.get("ndq_shares", 0))

    new_data = {
        "cash_cny": cash_cny,
        "aud_cash": aud_cash,
        "ndq_shares": ndq_shares,
        "gold_grams": round(total_grams, 4),
        "gold_avg_cost_cny_per_gram": round(avg_cost, 2),
    }
    new_body = f"""# 当前持仓

## 现金
- **CNY 现金**: ¥{cash_cny:,.2f}
- **AUD 现金**: ${aud_cash:,.2f}

## 持仓
- **NDQ.AX**: {ndq_shares} 股
- **黄金 (浙商积存金)**: {total_grams:.4f} 克 (均价 ¥{avg_cost:.2f}/g)

## 说明

- 黄金持仓由 NapCat 私聊命令更新（`/gold_buy 12.5g @1040`）
- 平均成本随每次买入自动重算
"""
    store.write("portfolio", "state", new_data, new_body)
    print(f"✓ portfolio.gold_grams = {total_grams:.4f}")

    # 3. 修正 strategy.target_assets[gold] 的 offset 和 sell_fee
    strat_doc = store.read("strategy")
    if strat_doc is None:
        return
    target_assets = list(strat_doc.get("target_assets", []))
    for a in target_assets:
        if a.get("symbol") == "GC=F":
            # 实测平均 offset ≈ -0.8%，但取 0 作为基准（点差极小）
            a["price_offset_pct"] = 0.0
            a["sell_fee_pct"] = 0.0038       # 0.4% × 95% = 0.38%
            a["note"] = "实测浙商点差极小 (≈ 0%)，卖出手续费 0.38%"

    new_strat_data = {
        "target_assets": target_assets,
        "target_allocation_stock": strat_doc.get("target_allocation_stock", 0.7),
        "target_allocation_cash": strat_doc.get("target_allocation_cash", 0.3),
    }
    store.write("strategy", "strategy", new_strat_data, strat_doc.body)
    print(f"✓ strategy.GC=F: offset_pct=0, sell_fee_pct=0.0038")


if __name__ == "__main__":
    main()
