import os
import math
import time
import weakref
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from datetime import timezone

import numpy as np
import pandas as pd
import yfinance as yf
from openinvest.db.market_store import MarketStore
from openinvest.utils.eastmoney_fund import fetch_fund_nav_history

# 纯计算核已迁 calc 层（ADR-026）——导回保持历史导出面；本文件只剩 IO shell。
# monkeypatch 计算逻辑请钉 openinvest.calc.{transaction_costs,timeframe_analysis}。
from openinvest.calc.timeframe_analysis import (  # noqa: F401
    _analyze_slice,
    _apply_cutoff,
    _apply_period,
    _calc_change,
    _calc_max_drawdown,
    _calc_volatility,
    analyze_multi_timeframe,
)
from openinvest.calc.market_metrics import METRICS_PERIOD
from openinvest.calc.transaction_costs import (  # noqa: F401
    CostSnapshot,
    ForexFriction,
    StockFriction,
    TransactionCostCalculator,
    format_cost_report,
)

_STORE = MarketStore()
_SOURCE_METADATA = weakref.WeakKeyDictionary()


def _source_cache(symbol):
    return _SOURCE_METADATA.setdefault(_STORE, {}).setdefault(symbol, {})


def _clock_age(key):
    try:
        value = _STORE.seconds_since_fetch(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float('inf')
    except (AttributeError, TypeError, ValueError):
        return float('inf')


def _clock_stamp(key):
    age = _clock_age(key)
    return datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat() if math.isfinite(age) and age >= 0 else None


def _frame_signature(frame):
    return hashlib.sha256(frame.to_json(date_format='iso').encode()).hexdigest()


def _source_json(value):
    if hasattr(value,'isoformat'):
        return value.isoformat()
    if isinstance(value,np.generic):
        return value.item()
    if isinstance(value,pd.DataFrame):
        return value.to_dict('records')
    raise TypeError('unsupported source metadata value')


def _persist_source(symbol, saved):
    """Share the original acquisition witness, never a refreshed cache clock."""
    try:
        value = {key:saved[key] for key in ('provider','metadata','basis','signature','received_at','failed','observed_prices')}
        payload = json.dumps({'schema':1,'symbol':symbol,**value},sort_keys=True,separators=(',',':'),default=_source_json)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with _STORE._lock, _STORE.conn:
            _STORE.conn.execute('CREATE TABLE IF NOT EXISTS source_observation_cache '
                                '(symbol TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
            _STORE.conn.execute('INSERT INTO source_observation_cache VALUES(?,?,?) '
                'ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256',
                (symbol,payload,digest))
    except (sqlite3.Error,AttributeError,TypeError):
        pass  # An unavailable cache never creates source qualification.


def _restore_source(symbol, signature):
    from openinvest.utils.market_observation import utc_time
    try:
        with _STORE._lock:
            if not _STORE.conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_observation_cache'").fetchone():
                return None
            row = _STORE.conn.execute('SELECT payload,sha256 FROM source_observation_cache WHERE symbol=?',(symbol,)).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            return None
        value = json.loads(row[0])
        if (type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('symbol') != symbol or value.get('signature') != signature
                or value.get('provider') not in {'yfinance','equibles','betashares'}
                or not isinstance(value.get('metadata'),dict) or not isinstance(value.get('observed_prices'),dict)
                or not isinstance(value.get('failed'),bool) or not isinstance(value.get('basis'),str)
                or utc_time(value.get('received_at')) is None):
            return None
        if any(not isinstance(day,str) or isinstance(price,bool) or not isinstance(price,(int,float))
               or not math.isfinite(price) for day,price in value['observed_prices'].items()):
            return None
        return value
    except (sqlite3.Error,AttributeError,TypeError,ValueError):
        return None


def _history_basis(store, symbol, frame):
    """An issuer NAV cannot silently become an exchange-traded close series."""
    try:
        with store._lock:
            rows = store.conn.execute('SELECT date,source FROM daily_prices WHERE symbol=?',(symbol,)).fetchall()
            store.conn.rollback()
        sources = {str(row[0]):str(row[1] or '') for row in rows}
        selected = [sources.get(index.date().isoformat(),'unknown') for index in frame.index]
        # A successful alternative quote does not attest that its scale can be
        # spliced into the provider-adjusted historical series.
        compatible = all(value.startswith('yfinance') for value in selected)
        return {'history_basis_qualified':bool(selected) and compatible,
                'history_basis':'compatible_adjusted_closes' if compatible else 'mixed_or_unverified_reference',
                'last_row_source':selected[-1] if selected else None}
    except (AttributeError,TypeError,ValueError):
        return {'history_basis_qualified':False,'history_basis':'unverified_reference'}


def _history_source(frame, symbol, *, provider=None, metadata=None, basis='adjusted_close', failed=False, received_at=None,
                    observed_prices=None):
    from openinvest.utils.market_observation import history_observation,utc_time
    saved = _source_cache(symbol)
    signature = _frame_signature(frame)
    if provider is not None:
        saved.update(provider=provider, metadata=metadata or {}, basis=basis, signature=signature,
                     received_at=received_at or datetime.now(timezone.utc).isoformat(), failed=failed,
                     observed_prices=dict(observed_prices or {}))
        _persist_source(symbol,saved)
    else:
        restored = _restore_source(symbol,signature)
        if (restored and (saved.get('signature') != signature or utc_time(saved.get('received_at')) is None
                          or utc_time(restored['received_at']) >= utc_time(saved['received_at']))):
            saved.update(restored)
    aligned = saved.get('signature') == signature
    selected = saved.get('provider', 'cache_unverified') if aligned else 'cache_unverified'
    source_failed = failed or saved.get('failed', False)
    if aligned and selected == 'yfinance':
        source_failed = source_failed or _clock_age('ATTEMPT:yfinance:'+symbol) < _clock_age('SUCCESS:yfinance:'+symbol)
    state = 'failed' if source_failed else 'ok' if provider is not None else 'cached' if aligned else 'unverified'
    result = history_observation(frame, provider=selected, metadata=saved.get('metadata', {}) if aligned else {},
        price_basis=saved.get('basis', 'unverified') if aligned else 'unverified', fetch_status=state,
        received_at=saved.get('received_at') if aligned else None)
    latest_date = frame.index[-1].date().isoformat() if not frame.empty else None
    latest_observed = (aligned and latest_date in saved.get('observed_prices',{})
        and saved['observed_prices'][latest_date] == float(frame['Close'].iloc[-1]))
    result['latest_price_observed'] = bool(latest_observed)
    if not latest_observed:
        # Metadata from a successful historical row cannot date a retained,
        # unrefreshed latest price. Keep the old value solely as a reference.
        result.update(quote_fresh=False,quote_asof=None,daily_alert_eligible=False,
                      latest_bar_completed=False,freshness='reference_only',
                      latest_price_reason='latest_cache_row_not_in_selected_response')
        result['completed_session_dates'] = [day for day in result['completed_session_dates'] if day != latest_date]
        result['completion_ends'].pop(latest_date,None)
    result.update(last_attempt_at=_clock_stamp('ATTEMPT:'+selected+':'+symbol),
                  last_successful_fetch_at=_clock_stamp('SUCCESS:'+selected+':'+symbol),
                  received_at=result.get('received_at') if aligned else None)
    result['symbol'] = symbol
    frame.attrs['source_observation'] = result
    # Legacy name retained: it describes a failure of the selected source,
    # never a successful alternative provider's Yahoo predecessor.
    frame.attrs['yf_fetch_failed'] = bool(source_failed)
    return frame

# BetaShares 官网可兜底现价的 symbol（yfinance 被墙/抓空时的最后一道）。
# 目前 scraper 只实现 NDQ；扩品种时加 symbol + 给 scraper 加对应 fund 页。
_BETASHARES_SYMBOLS = {"NDQ.AX"}


def _betashares_fallback(symbol: str) -> bool:
    """yfinance 失败时从 BetaShares 官网抓当前 NAV 写进 MarketStore。

    只兜现价（官网无免费历史序列，历史缺口不硬造）。成功返回 True。
    失败静默退化——保持"取不到价就没价"的原行为。
    """
    if symbol not in _BETASHARES_SYMBOLS:
        return False
    try:
        _STORE.mark_fetched('ATTEMPT:betashares:'+symbol)
        from openinvest.utils.betashares_scraper import scrape_full_ndq_data
        snap = scrape_full_ndq_data()
        _source_cache(symbol)['alternative_received_at'] = datetime.now(timezone.utc).isoformat()
        if (snap and snap.get("nav") and snap.get("date")
                and math.isfinite(float(snap['nav'])) and float(snap['nav']) > 0):
            _STORE.save_generic_price(
                symbol, snap["date"], snap["nav"], source="betashares_fallback"
            )
            _STORE.mark_fetched('SUCCESS:betashares:'+symbol)
            _source_cache(symbol)['alternative_observed_prices'] = {str(snap['date']):float(snap['nav'])}
            print(
                f"🛟 [betashares_fallback] {symbol} NAV {snap['nav']} @ {snap['date']}"
                " (yfinance unavailable, scraped betashares.com.au)"
            )
            return True
    except Exception:
        pass  # ponytail: 兜底的兜底不存在——抓不到就退回原"无数据"行为
    return False


# Equibles 兜底每 symbol 至少隔这么久才再调（跨进程走 fetch_log）。它的日线一天更新一次、免费档
# 100 次/天，而 Yahoo 挂着时每次 get_history_data 都会走到兜底分支（价格哨兵 10 分钟一轮）。
_EQUIBLES_RETRY_S = 6 * 3600


def _equibles_fallback(symbol: str, df_db: pd.DataFrame, today_str: str) -> bool:
    """yfinance 失败时从 Equibles 补 A 股 / 港股个股日线（需 EQUIBLES_API_KEY）。成功返回 True。

    Equibles 的 Close / Adj Close 与 yfinance ``auto_adjust=False`` 逐位相同，过 adjusted_with_raw
    落库后和 yfinance 路径同口径、能直接拼接。库里够深 → 只拉最后一根往前 7 天；不够 → 拉 2 年。
    增量的重叠日复权基准变了（分红 / 拆股后全历史重算）→ 同 splice 哨兵，改拉 2 年重写。
    ETF 不在 Equibles 覆盖内（如 510300.SS）。它的数据比 Yahoo 晚 1~2 个交易日，调用方照样标陈旧。
    """
    from openinvest.utils.equibles import fetch_daily_prices, to_mic

    if not to_mic(symbol) or not os.getenv("EQUIBLES_API_KEY", "").strip():
        return False
    log_key = f"EQUIBLES:{symbol}"
    if min(_clock_age('ATTEMPT:equibles:'+symbol), _clock_age(log_key)) < _EQUIBLES_RETRY_S:
        return False
    _STORE.mark_fetched('ATTEMPT:equibles:'+symbol)

    two_years_ago = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    deep = len(df_db) >= _MIN_HISTORY_ROWS
    start = (df_db.index[-1] - timedelta(days=7)).strftime("%Y-%m-%d") if deep else two_years_ago
    raw = fetch_daily_prices(symbol, start)
    basis = 'adjusted_close' if raw is not None and 'Adj Close' in raw.columns else 'raw_close'
    df = adjusted_with_raw(raw)
    received_at = datetime.now(timezone.utc).isoformat()
    if deep and df is not None and len(_splice_mismatch_days(df, df_db, today_str)) >= 2:
        print(f"⚠️ [equibles_fallback] {symbol} 重叠日复权基准变化，改拉 2 年重写")
        raw = fetch_daily_prices(symbol, two_years_ago)
        basis = 'adjusted_close' if raw is not None and 'Adj Close' in raw.columns else 'raw_close'
        df = adjusted_with_raw(raw)
        received_at = datetime.now(timezone.utc).isoformat()
    if df is None or df.empty:
        return False
    valid_rows = 0
    observed_prices = {}
    for idx, row in df.iterrows():
        close = _nan_to_none(row.get("Close"))
        if close is None or not math.isfinite(close) or close <= 0:
            continue
        _STORE.save_generic_price(
            symbol, idx.strftime("%Y-%m-%d"), close, source="equibles",
            high=_nan_to_none(row.get("High")),
            low=_nan_to_none(row.get("Low")),
            volume=_nan_to_none(row.get("Volume")),
            close_raw=_nan_to_none(row.get("CloseRaw")),
        )
        valid_rows += 1
        observed_prices[idx.strftime('%Y-%m-%d')] = close
    if not valid_rows:
        return False
    _STORE.mark_fetched('SUCCESS:equibles:'+symbol)
    _source_cache(symbol)['alternative_basis'] = basis
    _source_cache(symbol)['alternative_observed_prices'] = observed_prices
    _source_cache(symbol)['alternative_metadata'] = dict(df.attrs.get('history_metadata') or {})
    _source_cache(symbol)['alternative_received_at'] = received_at
    print(f"🛟 [equibles_fallback] {symbol} {len(df)} 根日线（yfinance unavailable，Data: Equibles）")
    return True


def _splice_mismatch_days(df_new: pd.DataFrame, df_db: pd.DataFrame, today_str: str) -> list[str]:
    """复权拼接哨兵（issue #179 P1-A④）：新拉的帧和库里重叠日 close 偏移 >1% 的日子。

    分红 / 拆股后数据源会**全历史回溯**重新复权，增量只更新最近几行，旧复权基准与新增行在缝合点
    断裂（ATR/RSI/regime 全被假跳变污染）。复权变化会让**所有**重叠日同时偏移，单日偏移多半是
    BetaShares NAV 兜底混写——调用方按 ≥2 天判定。今天这根是盘中临时值，不参与。
    """
    # 只建重叠段（df_db 是全历史，^GSPC 2.5 万行逐行 strftime 会拖慢每次增量刷新）
    db_close = {
        ts.strftime("%Y-%m-%d"): _nan_to_none(c)
        for ts, c in df_db["Close"].tail(len(df_new) + 10).items()
    }
    mismatched = []
    for idx, row in df_new.iterrows():
        day = idx.strftime("%Y-%m-%d")
        if day == today_str:
            continue
        new_c, old_c = _nan_to_none(row.get("Close")), db_close.get(day)
        if new_c and old_c and abs(new_c / old_c - 1) > 0.01:
            mismatched.append(day)
    return mismatched

# 历史行数低于此阈值 → 视为深度不足，触发 2y 全量回填。取 250 = 最长指标 MA250 的窗口，
# 保证 RSI/MA120/MA250/regime 全部算得出（仅 60 会让 60~249 根的 symbol 仍缺 MA120/MA250
# → REGIME=unknown）。年轻 symbol（上市不足 ~1 年）会每次拉 2y 取尽可用历史，可接受。
_MIN_HISTORY_ROWS = 250
# get_history_df 的 days 是行数上限；取足够大 = 全历史（同 verdict_review / backtest 口径）
_ALL_ROWS = 100000
# symbol → 今天已为「今天这根缺 H/L」补刷过的日期。每进程每 symbol 每天最多补一次：
# yfinance 本就给不出 H/L 的 bar（FX 周末、盘中半成型）不会每次调用都重拉。
_OHLCV_GAP_TRIED: dict[str, str] = {}
# 今天这根 bar 距上次刷新超过这么久就再刷 5d（盘中临时值，见 get_history_data）。
_TODAY_BAR_TTL_S = 30 * 60
# 场外基金 symbol → 上次成功拉东方财富净值历史的 monotonic 时刻。净值一天只更新一次，
# 每次全量（几百 KB）覆盖写库：常驻进程（MCP / scheduler）30 分钟内不重拉。
_FUND_REFRESHED: dict[str, float] = {}
_FUND_REFRESH_TTL = 30 * 60


def _nan_to_none(v):
    """yfinance 行里的 NaN / 缺失值转 None（落库为 NULL）。

    FX (USDCNY=X) / 部分指数没有真实成交量，Volume 会是 NaN；High/Low 缺失同理。
    """
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(fv) else fv


def _rebuild_days_from_intraday(ticker, symbol: str, days: dict) -> dict:
    """日线 Close 为 NaN 的日子用同日 60m bar 重建。days: {日期: 日线自带的成交量或 None}。

    Yahoo 有时把日线 Close 给成 NaN（A 股 ETF 已收盘的日子也会，成交量却是全天的）。
    数据源闸跳过 NaN，库里那天就停在盘中某次抓到的半截 bar：价不对，量只有全天的一小截，
    RVOL 被读成"缩量"。重建口径：收 = 当天最后一根有效收盘，高 / 低取极值；量优先用日线
    自带的（60m 偶有缺根，合计可能只有日线的一半），没有才取 60m 合计。60m 拉不到或当天
    没有有效收盘就保持原样。
    ponytail: 只看最近 5 天的 60m（覆盖 5d 增量刷新）；更早的 NaN 日保持原样。
    """
    try:
        df60 = history_with_raw(ticker, period="5d", interval="60m")
        received_at = datetime.now(timezone.utc).isoformat()
        from openinvest.utils.market_observation import provider_metadata
        metadata = provider_metadata(ticker)
    except Exception as e:  # noqa: BLE001  重建是尽力而为，失败不挡主刷新
        print(f"⚠️ [intraday-rebuild] {symbol} 60m 拉取失败，NaN 日保持原样：{e}")
        return {}
    if df60 is None or df60.empty:
        return {}
    observed_prices = {}
    for day, g in df60.groupby(df60.index.strftime("%Y-%m-%d")):
        last = g["Close"].last_valid_index()
        if day not in days or last is None:
            continue
        close = float(g.at[last,'Close'])
        if not math.isfinite(close) or close <= 0:
            continue
        volume = days[day]
        if volume is None:
            volume = _nan_to_none(g["Volume"].sum(min_count=1))
        _STORE.save_generic_price(
            symbol, day, close, source="yfinance_60m",
            high=_nan_to_none(g["High"].max()),
            low=_nan_to_none(g["Low"].min()),
            volume=volume,
            close_raw=_nan_to_none(g.at[last, "CloseRaw"]),  # ADR-027：与日线路径同口径
        )
        observed_prices[day] = close
    return {'prices':observed_prices,'metadata':metadata,'received_at':received_at}


def adjusted_with_raw(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance ``history(auto_adjust=False)`` 的帧 → 复权帧 + ``CloseRaw`` 列。

    复权部分与 ``history(auto_adjust=True)`` **逐位相同**（同 ``yfinance.utils.auto_adjust`` 的算法：
    ratio = Adj Close / Close；Open/High/Low 乘 ratio；Close 取 Adj Close），所以 regime / 技术指标 /
    forward_return 读到的 ``close`` 不会因为抓取参数变了而变（``tests/test_raw_close.py`` 逐位断言）。
    ``CloseRaw`` = yfinance 的 ``Close``：只按拆股复权、不含分红复权——账户估值用它（账本里的分红现金
    是真钱，历史价再复权一遍就算了两次）。

    没有 ``Adj Close`` 列（指数 / 期货 / FX 的 Yahoo 响应没有 adjclose，yfinance 本身退化成 Close）→
    不做复权，``CloseRaw`` = ``Close``。
    """
    if df is None or df.empty or "Close" not in df.columns:
        return df
    out = df.copy()
    if "Adj Close" in df.columns:
        ratio = (df["Adj Close"] / df["Close"]).to_numpy()
        for col in ("Open", "High", "Low"):
            if col in df.columns:
                out[col] = df[col] * ratio
        out["Close"] = df["Adj Close"]
        out = out.drop(columns=["Adj Close"])
    out["CloseRaw"] = df["Close"]
    return out


def history_with_raw(ticker, **kwargs) -> pd.DataFrame:
    """``ticker.history(auto_adjust=False, **kwargs)`` 经 :func:`adjusted_with_raw` 折成「旧 close 语义 + CloseRaw」。
    get_history_data / scripts.backfill_history / backfill_raw_close 共用这一条抓取口径。"""
    return adjusted_with_raw(ticker.history(auto_adjust=False, **kwargs))


# ==========================================
# 1. 通用数据获取 (yfinance)
# ==========================================
def get_history_data(
    symbol: str, period: str = "2y", as_of_date: Optional[str] = None
) -> pd.DataFrame:
    """拉行情历史数据。

    Args:
        symbol: yfinance ticker（如 NDQ.AX / GC=F / AAPL）；场外公募基金用
            ``FUND:<六位代码>``，走东方财富前复权净值（见 _get_fund_history）
        period: yfinance period（1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max），**真截断**：
            d=最近 N 根 bar，mo/y=相对最后一根 bar（as_of_date 时为截断后的最后一根）
            的日历回看。2026-10 前此参数被忽略、一律返回 ~730 行（≈3 年）→ "1mo MoM"
            实为 ~3 年涨跌。要喂 compute_metrics 的调用方用 METRICS_PERIOD。
        as_of_date: **回测穿越防护**。如果给定 (ISO 'YYYY-MM-DD')，结果 df 会
            过滤到该日期**之前**（不含当日），所有数据源（DB 缓存 + yfinance 拉新
            + CSV 兜底）都受此约束。backtest_committee.py 的 _patch_tools_to_date
            注入此参数；正常 daily_report / NapCat 不传 → 跟旧行为一致。

            为什么需要：MarketStore DB 可能含 T+ 价格（之前 daily cron 写入），
            backtest 调 decision_date=2024-05-01 时若不过滤，LLM 会"看到未来"。
    """
    symbol = symbol.upper()
    if symbol.startswith("FUND:"):
        return _get_fund_history(symbol, period, as_of_date)

    # 1. 从数据库获取**全历史**（get_history_df 默认 tail(730) 在 cutoff 之前截，
    #    as_of 回测日窗口会被锚死在最近 730 行；period 截断统一放到最后）
    df_db = _STORE.get_history_df(symbol, days=_ALL_ROWS)

    # 2. 判断是否需要更新（如今天还没更新过）
    today_str = datetime.now().strftime('%Y-%m-%d')
    needs_update = df_db.empty or df_db.index[-1].strftime('%Y-%m-%d') != today_str
    # 2026-10：今天这根只有 close（gold_price 现价缓存 GC=F/USDCNY=X、betashares 兜底先写了）
    # → 上面判"已是今天"，5d 刷新整天跳过，H/L 一直 NULL，ATR/RVOL 静默退化成收盘价差
    # （生产 USDCNY=X 近 60 天 22 根）。该 symbol 平时有 H/L（列在）而今天这根缺 → 照样刷一次。
    ohlcv_gap = (
        not needs_update and as_of_date is None
        and "High" in df_db.columns and pd.isna(df_db["High"].iloc[-1])
        and _OHLCV_GAP_TRIED.get(symbol) != today_str
    )
    if ohlcv_gap:
        _OHLCV_GAP_TRIED[symbol] = today_str
    # 今天这根是盘中临时 bar：首个盘中调用落库后，同一天后面的调用都拿它当"今天收盘"
    # （收盘后跑的日报委员会也读到上午的价和量）。距上次刷新超过 TTL 就再刷 5d；
    # 刷新时间记在库里（fetch_log），CLI / MCP / cron 各进程共用。
    # ponytail: 固定 TTL，收盘后也照刷；要按交易所收盘时间停刷时再加。
    today_bar_stale = (
        not needs_update and not ohlcv_gap and as_of_date is None
        and min(_clock_age('ATTEMPT:yfinance:'+symbol), _clock_age(symbol)) > _TODAY_BAR_TTL_S
    )

    # backtest 模式 yfinance 策略：
    # - 实盘（as_of_date=None）：照常拉最新 5d
    # - backtest cutoff < today - 30d：**安全放行** yfinance 拉 2y 历史
    #   （即使拉到最新数据，as_of_date 过滤会截断，不会穿越；但能保证 DB 有
    #   cutoff 之前的足够数据让 RSI/ATR 等指标算得出来）
    # - backtest cutoff >= today - 30d：保守，仍 skip（防边界情况）
    should_fetch_yf = False
    if needs_update or ohlcv_gap or today_bar_stale:
        if as_of_date is None:
            should_fetch_yf = True
            # 深度不足（empty 或仅几天）→ 全量回填 2y，保证 RSI/MA120/MA250/regime
            # 算得出；已有足够历史 → 5d 增量。修「新 symbol 只 5d → REGIME=unknown」bug，
            # 并自愈已浅的 symbol（如先前被 5d 抓过的 510500/SPY 再被跟踪时）。
            fetch_period = "5d" if len(df_db) >= _MIN_HISTORY_ROWS else "2y"
        else:
            from datetime import datetime as _dt, timedelta as _td
            try:
                cutoff_dt = _dt.strptime(as_of_date, "%Y-%m-%d").date()
                today_dt = _dt.now().date()
                if (today_dt - cutoff_dt) > _td(days=30):
                    # cutoff 足够老，拉 2y 历史给 backtest 用
                    should_fetch_yf = True
                    fetch_period = "2y"
            except ValueError:
                pass  # 日期格式错误就不拉

    if as_of_date is None and _clock_age('ATTEMPT:yfinance:'+symbol) < _TODAY_BAR_TTL_S:
        should_fetch_yf = False
    yf_fetch_failed = False
    selected_provider = None
    selected_metadata = None
    selected_received_at = None
    observed_prices = {}
    selected_basis = 'adjusted_close'
    if should_fetch_yf:
        yf_got_data = False
        _STORE.mark_fetched('ATTEMPT:yfinance:'+symbol)
        try:
            print(f"🔄 [yfinance] Refreshing {symbol} (period={fetch_period})...")
            ticker = yf.Ticker(symbol)
            df_yf = history_with_raw(ticker, period=fetch_period)
            selected_received_at = datetime.now(timezone.utc).isoformat()
            # 复权拼接哨兵（见 _splice_mismatch_days）：≥2 个重叠日差 >1% 才判定复权基准
            # 变化，升级 2y 全量重取（单日偏移多半是 BetaShares 兜底混写，本轮落库自愈）。
            if fetch_period == "5d" and not df_yf.empty and not df_db.empty:
                mismatched = _splice_mismatch_days(df_yf, df_db, today_str)
                if len(mismatched) >= 2:
                    print(
                        f"⚠️ [splice-sentinel] {symbol} 重叠日 {mismatched} close "
                        f"整体偏移 >1%，疑似分红/拆股复权基准变化，改 2y 全量重取自愈"
                    )
                    df_yf = history_with_raw(ticker, period="2y")
                    selected_received_at = datetime.now(timezone.utc).isoformat()
            if not df_yf.empty:
                from openinvest.utils.market_observation import provider_metadata
                selected_metadata = provider_metadata(ticker)
                nan_days = {}
                rebuilt = {}
                for idx, row in df_yf.iterrows():
                    # 数据源闸：close=NaN（yfinance 收盘前半成型 bar）不落库。
                    # 否则它以 NULL 入 daily_prices，下游读价 float(NULL)→NaN，穿过
                    # 全链路 is-None 守卫污染总资产（2026-06-23 根因）。
                    close = _nan_to_none(row.get('Close'))
                    if close is None or not math.isfinite(close):
                        nan_days[idx.strftime('%Y-%m-%d')] = _nan_to_none(row.get('Volume'))
                        continue
                    # 一并落 OHLCV：High/Low 给真 TR/ATR，Volume 给 RVOL。
                    # NaN（如 FX/指数无成交量）转 None，落 NULL。
                    _STORE.save_generic_price(
                        symbol, idx.strftime('%Y-%m-%d'), close,
                        high=_nan_to_none(row.get('High')),
                        low=_nan_to_none(row.get('Low')),
                        volume=_nan_to_none(row.get('Volume')),
                        close_raw=_nan_to_none(row.get('CloseRaw')),
                    )
                    observed_prices[idx.strftime('%Y-%m-%d')] = close
                if nan_days and as_of_date is None:
                    rebuilt = _rebuild_days_from_intraday(ticker, symbol, nan_days)
                    observed_prices.update(rebuilt.get('prices',{}))
                yf_got_data = bool(observed_prices)
                if yf_got_data:
                    _STORE.mark_fetched(symbol)
                    _STORE.mark_fetched('SUCCESS:yfinance:'+symbol)
                    selected_provider = 'yfinance'
                df_db = _STORE.get_history_df(symbol, days=_ALL_ROWS)
                if (not df_db.empty and rebuilt.get('prices',{}).get(df_db.index[-1].date().isoformat())
                        == float(df_db['Close'].iloc[-1])):
                    # Only a selected latest row from this reconstruction can
                    # use its metadata and receipt; an older repaired hole
                    # cannot move the asof of an untouched daily response.
                    selected_received_at = rebuilt['received_at']
                    selected_metadata = rebuilt['metadata']
        except Exception as e:
            print(f"❌ yfinance sync failed for {symbol}: {e}")
        # yfinance 失败/返回空（被墙/被限流）→ BetaShares 官网兜底当前 NAV。
        # 只在实盘路径（as_of_date=None）触发：scraper 只有"现在"这一个点，
        # 对历史 cutoff 无意义。失败只保留 ATTEMPT 时刻，不更新 SUCCESS；
        # 缓存价格不能证明本次数据源正常。
        # A 股 / 港股个股再走 Equibles 日线（配了 EQUIBLES_API_KEY 才有）。
        if not yf_got_data and as_of_date is None:
            if _betashares_fallback(symbol):
                selected_provider, selected_basis, selected_metadata = 'betashares', 'published_nav_reference', {}
            elif _equibles_fallback(symbol, df_db, today_str):
                selected_provider, selected_metadata = 'equibles', _source_cache(symbol).get('alternative_metadata', {})
                selected_basis = _source_cache(symbol).get('alternative_basis','raw_close')
        if selected_provider is not None and selected_provider != 'yfinance':
            df_db = _STORE.get_history_df(symbol, days=_ALL_ROWS)
            selected_received_at = _source_cache(symbol).get('alternative_received_at')
            observed_prices = _source_cache(symbol).get('alternative_observed_prices',{})
        yf_fetch_failed = selected_provider is None
        if yf_fetch_failed:
            _source_cache(symbol)['failed'] = True

    if not df_db.empty:
        _history_source(df_db, symbol, provider=selected_provider, metadata=selected_metadata,
                        basis=selected_basis, failed=yf_fetch_failed, received_at=selected_received_at,
                        observed_prices=observed_prices)
        out = _apply_period(_apply_cutoff(df_db, as_of_date), period)
        basis = _history_basis(_STORE,symbol,out)
        out.attrs['source_observation'] = {**out.attrs.get('source_observation',{}),**basis}
        if basis.get('last_row_source') == 'betashares_fallback':
            out.attrs['source_observation'].update(price_basis='published_nav_reference',quote_fresh=False,
                                                  freshness='reference_only',daily_alert_eligible=False)
        elif not str(basis.get('last_row_source') or '').startswith('yfinance') and basis.get('last_row_source') != 'equibles':
            out.attrs['source_observation'].update(price_basis='unverified',quote_fresh=False,
                                                  freshness='reference_only',daily_alert_eligible=False)
        # 本次该刷没刷到（yfinance 挂/空）→ 返回的是库里旧数据。挂在 attrs 上不改返回类型
        # （调用方零改）；utils.quotes 据此标 is_stale，dca_daily 区分「行情源挂了」
        # 与「休市没有新 bar」（2026-10）。
        if as_of_date is not None:
            out.attrs['source_observation'] = {**out.attrs.get('source_observation', {}),
                'freshness':'historical_reference', 'quote_fresh':False, 'daily_alert_eligible':False,
                'quote_asof':None,'completed_session_dates':[],'completion_ends':{},
                'bar_asof':str(out.index[-1]) if not out.empty else None,
                'historical_availability':'unverified_historical_vintage'}
        return out

    return pd.DataFrame()


def _get_fund_history(symbol: str, period: str, as_of_date: Optional[str]) -> pd.DataFrame:
    """场外基金（``FUND:<六位代码>``）：yfinance 没有，改拉东方财富前复权净值。

    全量覆盖写库而不是增量：前复权口径下每次分红都会改写全部历史值。只有收盘价，
    没有 H/L/Volume——ATR 走 |ΔClose| 退化口径，RVOL 为空。回测（as_of_date）同样
    拉全量：入库后由 _apply_cutoff 截断，不会穿越。拉取失败就用库里已有的，
    attrs 标 yf_fetch_failed（与 yfinance 路径同一契约）。
    """
    now = time.monotonic()
    last = _FUND_REFRESHED.get(symbol)
    refresh_failed = False
    if last is None or now - last >= _FUND_REFRESH_TTL:
        history = fetch_fund_nav_history(symbol, adjusted=True)
        if history:
            for date_str, nav in history:
                _STORE.save_generic_price(symbol, date_str, nav, source="eastmoney_fund")
            _FUND_REFRESHED[symbol] = now
        else:
            refresh_failed = True
    df_db = _STORE.get_history_df(symbol, days=_ALL_ROWS)
    if df_db.empty:
        return pd.DataFrame()
    out = _apply_period(_apply_cutoff(df_db, as_of_date), period)
    out.attrs["yf_fetch_failed"] = refresh_failed
    return out


# ==========================================
# 3. 对外接口
# ==========================================
def _safe_last_change(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """拉 1mo 行情，返回 (last_close, MoM_change)；无数据返回 (None, None)。graceful。"""
    try:
        df = get_history_data(symbol, "1mo")
    except Exception:
        return None, None
    if df is None or df.empty or "Close" not in df:
        return None, None
    last = float(df["Close"].iloc[-1])
    from openinvest.utils.market_observation import frame_observation
    change = (_calc_change(float(df["Close"].iloc[0]), last)
              if frame_observation(df).get('history_basis_qualified') is not False else None)
    return last, change


class MacroReport(str):
    def __new__(cls, value, health):
        obj = super().__new__(cls, value)
        obj.input_health = health
        return obj


def macro_input_health(value) -> dict:
    if hasattr(value, "input_health"):
        return value.input_health
    if not value or (isinstance(value, str) and (value.startswith("Error fetching macro data:") or "[MACRO_INPUT_UNAVAILABLE]" in value)):
        return {"valid": False, "reason": "macro_source_unavailable"}
    return {"valid": None, "reason": "legacy_input_health_unspecified"}


def get_macro_data() -> str:
    try:
        tnx = get_history_data("^TNX", "1mo")
        vix = get_history_data("^VIX", "1mo")

        from openinvest.core.config import load_config
        from datetime import datetime, timezone
        from openinvest.utils.market_observation import frame_observation
        max_age = load_config().staleness.price_stale_days
        sources = {}
        for name, frame in (("TNX", tnx), ("VIX", vix)):
            valid = not frame.empty and not frame.attrs.get("yf_fetch_failed")
            if valid:
                valid = (math.isfinite(float(frame["Close"].iloc[-1])) and
                         0 <= (datetime.now(timezone.utc).date() - frame.index[-1].date()).days <= max_age)
            sources[name] = {"valid": bool(valid), "last_bar": str(frame.index[-1]) if not frame.empty else None,
                             'source_observation':frame_observation(frame)}
        if not all(item["valid"] for item in sources.values()):
            return MacroReport("[MACRO_INPUT_UNAVAILABLE] Required macro observations unavailable/stale; values are unknown.", {"valid": False, "sources": sources})
        tnx_last = tnx['Close'].iloc[-1]
        tnx_change = (_calc_change(tnx['Close'].iloc[0], tnx_last)
                      if frame_observation(tnx).get('history_basis_qualified') is not False else None)
        vix_last = vix['Close'].iloc[-1]
        vix_change = (_calc_change(vix['Close'].iloc[0], vix_last)
                      if frame_observation(vix).get('history_basis_qualified') is not False else None)
        change_label = lambda value: f'{value:+.2%}' if value is not None else 'unknown (history scale unverified)'

        # CONTAMINATION CHANNEL (ADR-022): 绝对价位/宏观点位逐字进 prompt → 记忆过历史的 LLM 可反推年代;归一化能压低但杀纪律规则(VIX>20=fear 吃绝对值),不可消除。
        report = f"""
--- MACRO INDICATORS (Reference) ---
1. US 10Y Treasury Yield (^TNX): {tnx_last:.2f}% (MoM: {change_label(tnx_change)})
   *Note: Rising yields often hurt tech stock valuations.*

2. CBOE Volatility Index (^VIX): {vix_last:.2f} (MoM: {change_label(vix_change)})
   *Note: This is the observed index level, not a calibrated forecast or a universal fear/complacency cutoff.*
"""

        # 货币因素（黄金/商品类资产的"基本面"）—— 逐项 graceful，缺一项不影响其余。
        # DX-Y.NYB = ICE 美元指数（DX=F 已被 Yahoo 下架，必须用 DX-Y.NYB）。
        dxy_last, dxy_change = _safe_last_change("DX-Y.NYB")
        if dxy_last is not None:
            report += (
                f"\n3. US Dollar Index (DXY, DX-Y.NYB): {dxy_last:.2f} (MoM: {change_label(dxy_change)})\n"
                f"   *Note: Strong/rising USD pressures gold & commodities; weak USD is a tailwind.*\n"
            )
        # TIP is an ETF price observation, not a measurement of real yields.
        _tip_last, tip_change = _safe_last_change("TIP")
        if _tip_last is not None:
            report += (
                f"\n4. TIPS bond ETF price (TIP, MoM): {change_label(tip_change)}\n"
                f"   *Note: This ETF price change also reflects duration, inflation indexation and distributions. "
                f"It does not establish the level or direction of real yields, or a gold trading signal.*\n"
            )

        return MacroReport(report, {"valid": True, "sources": sources})
    except Exception as e:
        return MacroReport(f"[MACRO_INPUT_UNAVAILABLE] {type(e).__name__}; values unknown", {"valid": False, "reason": type(e).__name__})


def get_full_market_data(target_asset: str, fx_symbol: Optional[str] = None) -> str:
    """合并目标资产 + 关联汇率的多时间维度报告

    Args:
        target_asset: yfinance ticker（510300.SS / AAPL / NDQ.AX 等都行，
            必填——之前写死一个默认 ticker，会让 fork 用户错拉到与自己无关的标的数据）
        fx_symbol: 关联汇率 ticker（None 则 daily_report 单独决定）
    """
    df_asset = get_history_data(target_asset, METRICS_PERIOD)
    report_asset = analyze_multi_timeframe(df_asset, f"TARGET ASSET ({target_asset})")

    if fx_symbol is None:
        return report_asset

    df_fx = get_history_data(fx_symbol, METRICS_PERIOD)
    report_fx = analyze_multi_timeframe(df_fx, f"CURRENCY RATE ({fx_symbol})")
    return f"{report_asset}\n\n{report_fx}\n"


def get_cost_snapshot(
    invest_cny: float,
    amount_aud: Optional[float] = None,
    spot_rate: Optional[float] = None
) -> CostSnapshot:
    calc = TransactionCostCalculator()

    if spot_rate is None:
        df_fx = get_history_data("AUDCNY=X", "1d")
        if not df_fx.empty:
            spot_rate = df_fx['Close'].iloc[-1]
        else:
            spot_rate = 0.0

    fx_data = calc.calculate_forex_friction(invest_cny, spot_rate)

    trade_from_fx = amount_aud is None
    if amount_aud is None:
        amount_aud = fx_data.net_aud if fx_data.is_viable else 0.0

    stock_data = calc.calculate_stock_friction(amount_aud)

    combined_fee_cny = None
    combined_friction_pct = None
    if trade_from_fx and invest_cny > 0 and spot_rate > 0 and amount_aud > 0:
        combined_fee_cny = fx_data.total_fee_cny + (stock_data.fee_aud * spot_rate)
        combined_friction_pct = (combined_fee_cny / invest_cny) * 100

    return CostSnapshot(
        invest_cny=invest_cny,
        spot_rate=spot_rate,
        forex=fx_data,
        trade_aud=amount_aud,
        stock=stock_data,
        combined_fee_cny=combined_fee_cny,
        combined_friction_pct=combined_friction_pct
    )


def get_cost_report(
    invest_cny: float,
    amount_aud: Optional[float] = None,
    spot_rate: Optional[float] = None
) -> str:
    snapshot = get_cost_snapshot(
        invest_cny=invest_cny,
        amount_aud=amount_aud,
        spot_rate=spot_rate
    )
    return format_cost_report(snapshot)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(get_full_market_data(sym))
