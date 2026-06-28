import datetime as _dt
import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "market_data.db")


def _is_phantom_weekend(symbol: str, date_str: str) -> bool:
    """周末日期的 bar 对【周一~周五交易】的标的是幽灵 —— 多半是 tz 错位造的(把 Asia/AU
    的周一挂牌错位成周日)，且该 bar 收盘常 == 次日真实价 → 未来泄漏 + 滚动指标失真。
    豁免:FX(``=X``,~24/5) 和加密(``-USD``,24/7) 真实有周末报价，放行。
    这是写库 chokepoint 的后挡 —— 即便某条 ingestion 路径忘了用 tz_localize，也漏不出幽灵。"""
    s = (symbol or "").upper()
    if s.endswith("=X") or s.endswith("-USD"):
        return False
    try:
        return _dt.date.fromisoformat(date_str).weekday() >= 5
    except (ValueError, TypeError):
        return False


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
        # 1. 价格历史（含 OHLCV：high/low 给真 TR/ATR 用，volume 给 RVOL 用）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                symbol TEXT, date TEXT, close REAL, source TEXT,
                high REAL, low REAL, volume REAL,
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
        # 兼容迁移：给老库的 daily_prices 补 high/low/volume 列
        self._migrate_ohlcv_columns()

    def _migrate_ohlcv_columns(self):
        """向后兼容迁移：旧 daily_prices 只有 close，给它补 high/low/volume 列。

        ALTER TABLE ADD COLUMN 的默认值是 NULL —— 旧行读出来 high/low/volume = None，
        市场指标层的 ATR 会对这些行退化为收盘价差（不报错），回填后才走真 TR。
        幂等：列已存在就跳过，重复启动安全。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA table_info(daily_prices)")
            existing = {row[1] for row in cursor.fetchall()}
            for col in ("high", "low", "volume"):
                if col not in existing:
                    cursor.execute(
                        f"ALTER TABLE daily_prices ADD COLUMN {col} REAL"
                    )
            self.conn.commit()

    def save_ndq_snapshot(self, date_str, nav, stats, holdings, sectors):
        with self._lock:
            cursor = self.conn.cursor()
            # 列名显式写出（表已扩 OHLCV 列，positional VALUES 会列数不匹配报错）。
            # betashares NAV 只有收盘，high/low/volume 留 NULL；NDQ.AX 的 OHLC 由
            # get_history_data 的 yfinance 刷新填（同 PK INSERT OR REPLACE，后写者赢）。
            cursor.execute(
                "INSERT OR REPLACE INTO daily_prices (symbol, date, close, source) VALUES (?, ?, ?, ?)",
                ("NDQ.AX", date_str, nav, "betashares_scraper"),
            )
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
        """返回 Pandas DataFrame 格式的历史数据（含 OHLCV）。

        SELECT 出 high/low/volume，让 DataFrame 真正带 High/Low/Volume → 市场指标
        层的真 TR / RVOL 分支才能活。**全列为 NaN 的 OHLCV 列会被丢弃**，保持
        "列存在 ⟺ 有数据" 的契约（未回填的老 symbol 不会误触发真 TR 分支）。
        """
        import pandas as pd
        with self._lock:
            query = (
                "SELECT date as Date, close as Close, high as High, "
                "low as Low, volume as Volume FROM daily_prices "
                "WHERE symbol = ? ORDER BY date ASC"
            )
            df = pd.read_sql_query(query, self.conn, params=(symbol,))
        if not df.empty:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
            # 丢弃全 NaN 的 OHLCV 列（老库未回填时 high/low/volume 全空）
            for col in ("High", "Low", "Volume"):
                if col in df.columns and df[col].isna().all():
                    df = df.drop(columns=[col])
        return df.tail(days)

    def backfill_ohlcv_row(self, symbol, date_str, close, high, low, volume,
                           source="yfinance_backfill"):
        """回填一日 OHLCV（非破坏式）：

        - (symbol, date) 已存在：只补 high/low/volume，**不动既有 close/source**
          （保留 betashares NAV 等权威收盘价，避免回填顺手改写历史决策依据）。
        - 不存在：整行 INSERT，close 用 yfinance。

        返回 "updated" | "inserted" | "skipped_weekend"。
        """
        if _is_phantom_weekend(symbol, date_str):
            return "skipped_weekend"
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM daily_prices WHERE symbol = ? AND date = ?",
                (symbol, date_str),
            )
            exists = cursor.fetchone() is not None
            if exists:
                cursor.execute(
                    "UPDATE daily_prices SET high = ?, low = ?, volume = ? "
                    "WHERE symbol = ? AND date = ?",
                    (high, low, volume, symbol, date_str),
                )
                result = "updated"
            else:
                cursor.execute(
                    "INSERT INTO daily_prices "
                    "(symbol, date, close, source, high, low, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (symbol, date_str, close, source, high, low, volume),
                )
                result = "inserted"
            self.conn.commit()
            return result

    def distinct_symbols(self):
        """daily_prices 里出现过的所有 symbol（回填脚本枚举用）"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol")
            return [r[0] for r in cursor.fetchall()]

    def save_generic_price(self, symbol, date_str, close, source="yfinance",
                           high=None, low=None, volume=None):
        """存储一日行情。close 必填；high/low/volume 可选（OHLCV 回填 / 日常刷新用）。

        向后兼容：只传 close 的老调用方行为不变（high/low/volume 落 NULL）。
        """
        if _is_phantom_weekend(symbol, date_str):
            return
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(symbol, date, close, source, high, low, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, date_str, close, source, high, low, volume),
            )
            self.conn.commit()