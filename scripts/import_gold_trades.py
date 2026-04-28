"""一次性导入浙商积存金交易记录到 memory/portfolio_history.jsonl

数据来源：从 git-ignored 私有文件 memory/.state/gold_trades.private.json 读取，
绝不硬编码到入库代码（audit C1）。Schema 见该文件首行 _comment 注释。

不存在私有文件时使用合成 demo 数据，让公开仓库 clone 的人能跑通流程，
但拿不到任何真实人的持仓信息。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

PRIVATE_TRADES_PATH = ROOT / "memory" / ".state" / "gold_trades.private.json"

DEMO_TRADES: List[Dict] = [
    # 演示用合成数据：单笔 10g 买入 @ ¥500，便于跑通脚本；与任何真实账户无关
    {"ts_origin": "2024-01-15T10:00:00", "action": "bought", "symbol": "GOLD-CNY",
     "channel": "浙商积存金", "kind": "实时", "units": 10.0,
     "price_per_unit": 500.00, "total_amount": 5000.00, "fee": 0, "currency": "CNY"},
]


def _load_trades() -> List[Dict]:
    """从私有 JSON 加载，转换 schema 为本脚本期望的格式"""
    if not PRIVATE_TRADES_PATH.exists():
        return DEMO_TRADES
    with open(PRIVATE_TRADES_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f).get("trades", [])
    out: List[Dict] = []
    for t in raw:
        # private json schema: {ts, kind, grams, price, total}
        # 转成 portfolio_history 期望的: {ts_origin, action, units, price_per_unit, total_amount, ...}
        is_gift = t["kind"] == "赠金"
        out.append({
            "ts_origin": t["ts"].replace(" ", "T"),
            "action": "gift" if is_gift else "bought",
            "symbol": "GOLD-CNY",
            "channel": "浙商积存金",
            "kind": t["kind"].replace("买金-", ""),
            "units": t["grams"],
            "price_per_unit": t["price"],
            "total_amount": t["total"],
            "fee": 0,
            "currency": "CNY",
        })
    return out


TRADES = _load_trades()


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
