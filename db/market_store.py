import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "market_data.db")

class MarketStore:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH)
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        # 1. 价格历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                symbol TEXT, date TEXT, close REAL, source TEXT,
                PRIMARY KEY (symbol, date)
            )""")
        # 2. ETF 持仓详情 (Top 10)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS etf_holdings (
                etf_symbol TEXT, date TEXT, asset_name TEXT, weight REAL,
                PRIMARY KEY (etf_symbol, date, asset_name, weight)
            )""")
        # 3. ETF 行业分布
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS etf_sectors (
                etf_symbol TEXT, date TEXT, sector_name TEXT, weight REAL,
                PRIMARY KEY (etf_symbol, date, sector_name)
            )""")
        # 4. ETF 关键指标
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS etf_stats (
                symbol TEXT, date TEXT, key TEXT, value REAL,
                PRIMARY KEY (symbol, date, key)
            )""")
        self.conn.commit()

    def save_ndq_snapshot(self, date_str, nav, stats, holdings, sectors):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO daily_prices VALUES (?, ?, ?, ?)", ("NDQ.AX", date_str, nav, "betashares_scraper"))
        for k, v in stats.items():
            cursor.execute("INSERT OR REPLACE INTO etf_stats VALUES (?, ?, ?, ?)", ("NDQ.AX", date_str, k, v))
        for name, weight in holdings:
            cursor.execute("INSERT OR REPLACE INTO etf_holdings VALUES (?, ?, ?, ?)", ("NDQ.AX", date_str, name, weight))
        for name, weight in sectors:
            cursor.execute("INSERT OR REPLACE INTO etf_sectors VALUES (?, ?, ?, ?)", ("NDQ.AX", date_str, name, weight))
        self.conn.commit()

    def get_latest_price(self, symbol):
        cursor = self.conn.cursor()
        cursor.execute("SELECT close FROM daily_prices WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_history_df(self, symbol, days=730):
        """返回 Pandas DataFrame 格式的历史数据"""
        import pandas as pd
        query = "SELECT date as Date, close as Close FROM daily_prices WHERE symbol = ? ORDER BY date ASC"
        df = pd.read_sql_query(query, self.conn, params=(symbol,))
        if not df.empty:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
        return df.tail(days)

    def save_generic_price(self, symbol, date_str, close, source="yfinance"):
        """存储通用价格（汇率、收益率等）"""
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO daily_prices (symbol, date, close, source) VALUES (?, ?, ?, ?)", 
                       (symbol, date_str, close, source))
        self.conn.commit()