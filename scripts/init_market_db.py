"""legacy: BetaShares NDQ.AX 持仓快照初始化（已被 yfinance 通用路径取代）

B6 (2026-05) 起 utils/exchange_fee.py 不再走 BetaShares 特殊分支。
本脚本仅在以下场景仍有价值：
  - 想保存 NDQ.AX 的 ETF top 10 持仓 / 行业分布详情（这部分 yfinance 没有）
  - BetaShares 站点 fix 了 403 反爬

陌生 fork 用户（非 NDQ.AX 持有者）跑这个脚本无意义，建议跳过。
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from openinvest.utils.betashares_scraper import scrape_full_ndq_data
from openinvest.db.market_store import MarketStore


def sync_data():
    print("🚀 Starting BetaShares NDQ snapshot sync (legacy NDQ-only path)...")
    print("   非 NDQ.AX 持有者请跳过；价格数据走 utils/exchange_fee.py 的 yfinance 主路径。")
    store = MarketStore()

    snapshot = scrape_full_ndq_data()

    if snapshot and snapshot["date"] and snapshot["nav"]:
        print(f"✅ Data fetched for {snapshot['date']}. NAV: ${snapshot['nav']}")
        store.save_ndq_snapshot(
            snapshot["date"],
            snapshot["nav"],
            snapshot["stats"],
            snapshot["holdings"],
            snapshot["sectors"]
        )
        print("📁 Database updated successfully with holdings and sectors.")
    else:
        print("❌ Failed to sync data (BetaShares 站点可能 403)。")


if __name__ == "__main__":
    sync_data()
