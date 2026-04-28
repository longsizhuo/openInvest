import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import os
import pandas as pd
from db.market_store import MarketStore

def parse_betashares_content(html):
    """
    使用 BeautifulSoup 精准解析 HTML 标签
    """
    soup = BeautifulSoup(html, 'lxml')
    data = {
        "nav": None,
        "date": None,
        "stats": {},
        "holdings": [],
        "sectors": []
    }
    
    # 1. 提取 NAV
    nav_tag = soup.find(string=re.compile(r"NAV/Unit", re.I))
    if nav_tag:
        # NAV 通常在同级的某个地方或紧随其后的文本中
        # 考虑到结构复杂，我们用正则在全文搜
        text = soup.get_text(separator=' ', strip=True)
        nav_match = re.search(r"NAV/Unit.*?\$\s*([\d.]+)", text, re.I)
        if nav_match: data["nav"] = float(nav_match.group(1))
        
        date_match = re.search(r"As at\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text, re.I)
        if date_match:
            try:
                dt = datetime.strptime(date_match.group(1), "%d %B %Y")
                data["date"] = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass  # 日期格式异常，data["date"] 保持空

    # 2. 提取表格数据 (Holdings & Sectors)
    # 我们遍历所有的 <tr>
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and td:
            name = th.get_text(strip=True)
            val_str = td.get_text(strip=True).replace('%', '')
            try:
                val = float(val_str)
                # 根据名称特征分类
                if any(x in name for x in ["CORP", "INC", "AMAZON", "TESLA", "WALMART", "BROADCOM"]):
                    data["holdings"].append((name, val))
                elif any(x in name for x in ["Technology", "Services", "Discretionary", "Health", "Staples", "Industrials", "Utilities", "Materials", "Energy", "Financials"]):
                    data["sectors"].append((name, val))
            except ValueError:
                continue  # 不是数字（如 "N/A"），跳过这行

    # 3. 补全统计指标
    text = soup.get_text(separator=' ', strip=True)
    units_match = re.search(r"Units outstanding.*?\s*([\d,]+)", text, re.I)
    if units_match: data["stats"]["units_outstanding"] = float(units_match.group(1).replace(',', ''))
    
    assets_match = re.search(r"Net assets.*?\s*\$([\d,]+)", text, re.I)
    if assets_match: data["stats"]["net_assets"] = float(assets_match.group(1).replace(',', ''))

    return data

def scrape_full_ndq_data():
    url = "https://www.betashares.com.au/fund/nasdaq-100-etf/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return parse_betashares_content(r.text)
    except Exception as e:
        print(f"❌ Scraper Error: {e}")
        return None

def get_ndq_local_history():
    store = MarketStore()
    snapshot = scrape_full_ndq_data()
    if snapshot and snapshot["date"] and snapshot["nav"]:
        store.save_ndq_snapshot(
            snapshot["date"],
            snapshot["nav"],
            snapshot["stats"],
            snapshot["holdings"],
            snapshot["sectors"]
        )
    
    cursor = store.conn.cursor()
    cursor.execute("SELECT date, close FROM daily_prices WHERE symbol = 'NDQ.AX' ORDER BY date ASC")
    rows = cursor.fetchall()
    if rows:
        df = pd.DataFrame(rows, columns=["Date", "Close"])
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date")
    return pd.DataFrame()