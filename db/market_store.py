import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "market_data.db")


class MarketStore:
    """线程安全的 SQLite 行情库

    旧版 sqlite3.connect() 默认 check_same_thread=True，多线程跑 agent 时
    任意一个 agent 在 worker thread 里访问全局 _STORE 都会抛 ProgrammingError。
    现在加 check_same_thread=False + 显式锁，跨线程安全。
    """

    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        # 允许跨线程使用（配合下面的 _lock）
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._lock = threading.RLock()
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
        with self._lock:
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
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT close FROM daily_prices WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_history_df(self, symbol, days=730):
        """返回 Pandas DataFrame 格式的历史数据"""
        import pandas as pd
        with self._lock:
            query = "SELECT date as Date, close as Close FROM daily_prices WHERE symbol = ? ORDER BY date ASC"
            df = pd.read_sql_query(query, self.conn, params=(symbol,))
        if not df.empty:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
        return df.tail(days)

    def save_generic_price(self, symbol, date_str, close, source="yfinance"):
        """存储通用价格（汇率、收益率等）"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO daily_prices (symbol, date, close, source) VALUES (?, ?, ?, ?)",
                           (symbol, date_str, close, source))
            self.conn.commit()