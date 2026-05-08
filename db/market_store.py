import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "market_data.db")


class MarketStore:
    """线程安全 + 多进程并发安全的 SQLite 行情库

    跨线程：check_same_thread=False + RLock
    跨进程：journal_mode=WAL（默认 DELETE 模式锁整个文件，多进程会饿死）
            + busy_timeout=5000（被锁时最多等 5s 而不是立即 OperationalError）

    场景：scheduler / web_api / napcat_bot 三个进程同时读写时不再互相阻塞。
    """

    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        # 允许跨线程使用（配合下面的 _lock）
        self.conn = sqlite3.connect(
            DB_PATH,
            check_same_thread=False,
            timeout=5.0,  # busy_timeout fallback for older sqlite versions
        )
        # 跨进程并发：WAL 让 reader / writer 不再互相 block
        # busy_timeout 让被 lock 时等 5s 而不是立刻抛 OperationalError
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL 是推荐组合
        self.conn.commit()

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

    def get_latest_date(self, symbol):
        """该 symbol DB 里最新的日期字符串 (YYYY-MM-DD)，没记录返回 None。

        给上层做 staleness 判断用：scrape/yfinance 失败时 daily_report 仍能
        从这里看到"我手里这个价是 N 天前的"，决定是降级跑还是跳过该资产委员会。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT date FROM daily_prices WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,))
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