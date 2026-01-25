import sys
import os

# 路径设置
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.betashares_scraper import scrape_full_ndq_data
from db.market_store import MarketStore

def sync_data():
    print("🚀 Starting full market data sync...")
    store = MarketStore()
    
    # 获取全量快照
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
        print("❌ Failed to sync data.")

if __name__ == "__main__":
    sync_data()
