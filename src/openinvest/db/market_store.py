import os
import sqlite3
import threading
from openinvest.calc.symbols import is_closed_weekend
from openinvest.paths import INVEST_ROOT

DB_PATH = str(INVEST_ROOT / "db" / "market_data.db")


# 周末 bar 对【周一~周五交易】的标的是幽灵 —— 多半是 tz 错位造的(把 Asia/AU 的周一
# 挂牌错位成周日)，且该 bar 收盘常 == 次日真实价 → 未来泄漏 + 滚动指标失真。
# 写库 chokepoint 的后挡；口径与复盘的周末重复样本共用 calc.symbols.is_closed_weekend。
_is_phantom_weekend = is_closed_weekend


# 2026-10-07 #231：原 INSERT OR REPLACE = 删旧行再插 → 只传 close 的写入（gold_price
# 现价缓存 GC=F/USDCNY=X、betashares NAV 兜底）把同日已回填的 high/low/volume 冲成
# NULL，ATR/RVOL 静默退化成收盘价差。改 upsert：close/source 后写者赢（同旧语义），
# OHLCV 新值为 NULL 时保留旧值。
#
# ADR-027 PR2：close = 复权价（yfinance Adj Close，拆股 + 分红都复权，所有指标 / regime 读它，语义不变）；
# close_raw = 原始价（yfinance Close：只按拆股复权、不含分红复权），账户估值用它——账本里的分红现金是真钱，
# 历史价再复权一遍就算了两次。只有 yfinance 路径写它；场外基金 / 黄金代理 / betashares NAV 兜底不写（NULL）。
# 与 OHLCV 同款：新值为 NULL 时保留旧值。
_UPSERT_DAILY_PRICE = (
    "INSERT INTO daily_prices (symbol, date, close, source, high, low, volume, close_raw) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(symbol, date) DO UPDATE SET "
    "close = excluded.close, source = excluded.source, "
    "high = COALESCE(excluded.high, high), "
    "low = COALESCE(excluded.low, low), "
    "volume = COALESCE(excluded.volume, volume), "
    "close_raw = COALESCE(excluded.close_raw, close_raw)"
)


class MarketStore:
    """线程安全 + 多进程并发安全的 SQLite 行情库

    跨线程：check_same_thread=False + RLock
    跨进程：journal_mode=WAL（默认 DELETE 模式锁整个文件，多进程会饿死）
            + busy_timeout=5000（被锁时最多等 5s 而不是立即 OperationalError）

    场景：scheduler / web_api 多个进程同时读写时不再互相阻塞。
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
        # #104：每次进程启动回收 WAL 膨胀（TRUNCATE 失败退化为不回收，无害）。
        # 长命读连接（exchange_fee 全局 _STORE）持有的游离读快照会让被动
        # checkpoint 饥饿——启动时主动截断 + 读路径 rollback 双保险
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.conn.commit()

        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        # 1. 价格历史（含 OHLCV：high/low 给真 TR/ATR 用，volume 给 RVOL 用）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                symbol TEXT, date TEXT, close REAL, source TEXT,
                high REAL, low REAL, volume REAL, close_raw REAL,
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
        # 兼容迁移：给老库的 daily_prices 补 high/low/volume/close_raw 列
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
            for col in ("high", "low", "volume", "close_raw"):
                if col not in existing:
                    try:
                        cursor.execute(
                            f"ALTER TABLE daily_prices ADD COLUMN {col} REAL"
                        )
                    except sqlite3.OperationalError:
                        pass  # 另一个进程刚加完（并发启动 check-then-alter 竞态）：列已在，等价于已迁移
            self.conn.commit()

    def save_ndq_snapshot(self, date_str, nav, stats, holdings, sectors):
        with self._lock:
            cursor = self.conn.cursor()
            # betashares NAV 只有收盘：close/source 后写者赢，high/low/volume 走
            # _UPSERT_DAILY_PRICE 的 COALESCE 保留 yfinance 已填的 OHLCV（#231）。
            cursor.execute(
                _UPSERT_DAILY_PRICE,
                ("NDQ.AX", date_str, nav, "betashares_scraper", None, None, None, None),
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
            self.conn.rollback()  # #104
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
            self.conn.rollback()  # #104：结束隐式读事务，别钉住 WAL checkpoint
        if not df.empty:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
            # 丢弃全 NaN 的 OHLCV 列（老库未回填时 high/low/volume 全空）
            for col in ("High", "Low", "Volume"):
                if col in df.columns and df[col].isna().all():
                    df = df.drop(columns=[col])
        return df.tail(days)

    def backfill_ohlcv_row(self, symbol, date_str, close, high, low, volume,
                           source="yfinance_backfill", close_raw=None):
        """回填一日 OHLCV（非破坏式）：

        - (symbol, date) 已存在：只补 high/low/volume（和非空的 close_raw），**不动既有 close/source**
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
                    "UPDATE daily_prices SET high = ?, low = ?, volume = ?, "
                    "close_raw = COALESCE(?, close_raw) "
                    "WHERE symbol = ? AND date = ?",
                    (high, low, volume, close_raw, symbol, date_str),
                )
                result = "updated"
            else:
                cursor.execute(
                    "INSERT INTO daily_prices "
                    "(symbol, date, close, source, high, low, volume, close_raw) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, date_str, close, source, high, low, volume, close_raw),
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
                           high=None, low=None, volume=None, close_raw=None):
        """存储一日行情。close 必填；high/low/volume/close_raw 可选（OHLCV 回填 / 日常刷新用）。

        只传 close 的调用方（gold_price 现价缓存 / betashares 兜底）：新行
        high/low/volume 落 NULL；已有行只改 close/source，OHLCV 保留（#231）。
        """
        if _is_phantom_weekend(symbol, date_str):
            return
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                _UPSERT_DAILY_PRICE,
                (symbol, date_str, close, source, high, low, volume, close_raw),
            )
            self.conn.commit()
    def get_close_pair_df(self, symbol):
        """账户估值用：复权价 + 原始价成对读出，index=Date，列 ``Close`` / ``CloseRaw``（NULL → NaN）。

        单独一个方法而不是给 get_history_df 加列——后者的列集合是 regime / 技术指标一路下游的契约
        （"列存在 ⟺ 有数据"），``close_raw`` 只给估值层看。
        """
        import pandas as pd
        with self._lock:
            df = pd.read_sql_query(
                "SELECT date as Date, close as Close, close_raw as CloseRaw "
                "FROM daily_prices WHERE symbol = ? ORDER BY date ASC",
                self.conn, params=(symbol,),
            )
            self.conn.rollback()  # #104
        if not df.empty:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date")
        return df

    def set_close_raw(self, symbol, rows):
        """只回填已有行的 close_raw（``rows``: [(date_str, raw_close)]），不新增行、不动 close / OHLCV。
        返回 (更新行数, 库里没有这天的行数)。backfill_raw_close 用。"""
        updated = missing = 0
        with self._lock:
            cursor = self.conn.cursor()
            for date_str, raw in rows:
                cursor.execute(
                    "UPDATE daily_prices SET close_raw = ? WHERE symbol = ? AND date = ?",
                    (raw, symbol, date_str),
                )
                if cursor.rowcount:
                    updated += 1
                else:
                    missing += 1
            self.conn.commit()
        return updated, missing
