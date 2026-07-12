import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from openinvest.db.market_store import MarketStore

# 纯计算核已迁 calc 层（ADR-026）——导回保持历史导出面；本文件只剩 IO shell。
# monkeypatch 计算逻辑请钉 openinvest.calc.{transaction_costs,timeframe_analysis}。
from openinvest.calc.timeframe_analysis import (  # noqa: F401
    _analyze_slice,
    _apply_cutoff,
    _calc_change,
    _calc_max_drawdown,
    _calc_volatility,
    analyze_multi_timeframe,
)
from openinvest.calc.transaction_costs import (  # noqa: F401
    CostSnapshot,
    ForexFriction,
    StockFriction,
    TransactionCostCalculator,
    format_cost_report,
)

_STORE = MarketStore()

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
        from openinvest.utils.betashares_scraper import scrape_full_ndq_data
        snap = scrape_full_ndq_data()
        if snap and snap.get("nav") and snap.get("date"):
            _STORE.save_generic_price(
                symbol, snap["date"], snap["nav"], source="betashares_fallback"
            )
            print(
                f"🛟 [betashares_fallback] {symbol} NAV {snap['nav']} @ {snap['date']}"
                " (yfinance unavailable, scraped betashares.com.au)"
            )
            return True
    except Exception:
        pass  # ponytail: 兜底的兜底不存在——抓不到就退回原"无数据"行为
    return False

# 历史行数低于此阈值 → 视为深度不足，触发 2y 全量回填。取 250 = 最长指标 MA250 的窗口，
# 保证 RSI/MA120/MA250/regime 全部算得出（仅 60 会让 60~249 根的 symbol 仍缺 MA120/MA250
# → REGIME=unknown）。年轻 symbol（上市不足 ~1 年）会每次拉 2y 取尽可用历史，可接受。
_MIN_HISTORY_ROWS = 250


def _nan_to_none(v):
    """yfinance 行里的 NaN / 缺失值转 None（落库为 NULL）。

    FX (USDCNY=X) / 部分指数没有真实成交量，Volume 会是 NaN；High/Low 缺失同理。
    """
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(fv) else fv


# ==========================================
# 1. 通用数据获取 (yfinance)
# ==========================================
def get_history_data(
    symbol: str, period: str = "2y", as_of_date: Optional[str] = None
) -> pd.DataFrame:
    """拉行情历史数据。

    Args:
        symbol: yfinance ticker（如 NDQ.AX / GC=F / AAPL）
        period: yfinance 的 period 参数（兼容老调用方）
        as_of_date: **回测穿越防护**。如果给定 (ISO 'YYYY-MM-DD')，结果 df 会
            过滤到该日期**之前**（不含当日），所有数据源（DB 缓存 + yfinance 拉新
            + CSV 兜底）都受此约束。backtest_committee.py 的 _patch_tools_to_date
            注入此参数；正常 daily_report / NapCat 不传 → 跟旧行为一致。

            为什么需要：MarketStore DB 可能含 T+ 价格（之前 daily cron 写入），
            backtest 调 decision_date=2024-05-01 时若不过滤，LLM 会"看到未来"。
    """
    symbol = symbol.upper()

    # 1. 从数据库获取
    df_db = _STORE.get_history_df(symbol)

    # 2. 判断是否需要更新（如今天还没更新过）
    today_str = datetime.now().strftime('%Y-%m-%d')
    needs_update = df_db.empty or df_db.index[-1].strftime('%Y-%m-%d') != today_str

    # backtest 模式 yfinance 策略：
    # - 实盘（as_of_date=None）：照常拉最新 5d
    # - backtest cutoff < today - 30d：**安全放行** yfinance 拉 2y 历史
    #   （即使拉到最新数据，as_of_date 过滤会截断，不会穿越；但能保证 DB 有
    #   cutoff 之前的足够数据让 RSI/ATR 等指标算得出来）
    # - backtest cutoff >= today - 30d：保守，仍 skip（防边界情况）
    should_fetch_yf = False
    if needs_update:
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

    if should_fetch_yf:
        yf_got_data = False
        try:
            print(f"🔄 [yfinance] Refreshing {symbol} (period={fetch_period})...")
            ticker = yf.Ticker(symbol)
            df_yf = ticker.history(period=fetch_period)
            # 复权拼接哨兵（issue #179 P1-A④）：yfinance auto_adjust 在分红/拆股后
            # 会**全历史回溯**重新复权，而 5d 增量只更新最近几行——DB 里旧复权基准
            # 与新增行在缝合点断裂（ATR/RSI/regime 全被假跳变污染）。重叠日期的
            # close 本应逐字节一致；复权变化会让**所有**重叠日同时偏移，而单日
            # 偏移多半是 BetaShares NAV 兜底混写（本轮 5d 落库就会覆盖自愈）——
            # 所以 ≥2 个重叠日差 >1% 才判定复权基准变化，升级 2y 全量重取。
            if fetch_period == "5d" and not df_yf.empty and not df_db.empty:
                db_close = {
                    ts.strftime("%Y-%m-%d"): _nan_to_none(c)
                    for ts, c in df_db["Close"].items()
                }
                mismatched = []
                for idx, row in df_yf.iterrows():
                    day = idx.strftime("%Y-%m-%d")
                    new_c, old_c = _nan_to_none(row.get("Close")), db_close.get(day)
                    if new_c and old_c and abs(new_c / old_c - 1) > 0.01:
                        mismatched.append(day)
                if len(mismatched) >= 2:
                    print(
                        f"⚠️ [splice-sentinel] {symbol} 重叠日 {mismatched} close "
                        f"整体偏移 >1%，疑似分红/拆股复权基准变化，改 2y 全量重取自愈"
                    )
                    df_yf = ticker.history(period="2y")
            if not df_yf.empty:
                yf_got_data = True
                for idx, row in df_yf.iterrows():
                    # 数据源闸：close=NaN（yfinance 收盘前半成型 bar）不落库。
                    # 否则它以 NULL 入 daily_prices，下游读价 float(NULL)→NaN，穿过
                    # 全链路 is-None 守卫污染总资产（510300.SS 2026-06-23 根因）。
                    close = _nan_to_none(row.get('Close'))
                    if close is None:
                        continue
                    # 一并落 OHLCV：High/Low 给真 TR/ATR，Volume 给 RVOL。
                    # NaN（如 FX/指数无成交量）转 None，落 NULL。
                    _STORE.save_generic_price(
                        symbol, idx.strftime('%Y-%m-%d'), close,
                        high=_nan_to_none(row.get('High')),
                        low=_nan_to_none(row.get('Low')),
                        volume=_nan_to_none(row.get('Volume')),
                    )
                df_db = _STORE.get_history_df(symbol)
        except Exception as e:
            print(f"❌ yfinance sync failed for {symbol}: {e}")

        # yfinance 失败/返回空（被墙/被限流）→ BetaShares 官网兜底当前 NAV。
        # 只在实盘路径（as_of_date=None）触发：scraper 只有"现在"这一个点，
        # 对历史 cutoff 无意义。
        if not yf_got_data and as_of_date is None and _betashares_fallback(symbol):
            df_db = _STORE.get_history_df(symbol)

    if not df_db.empty:
        return _apply_cutoff(df_db, as_of_date)

    return pd.DataFrame()


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
    change = _calc_change(float(df["Close"].iloc[0]), last)
    return last, change


def get_macro_data() -> str:
    try:
        tnx = get_history_data("^TNX", "1mo")
        vix = get_history_data("^VIX", "1mo")

        tnx_last = tnx['Close'].iloc[-1] if not tnx.empty else 0.0
        tnx_change = _calc_change(tnx['Close'].iloc[0], tnx_last) if not tnx.empty else 0.0

        vix_last = vix['Close'].iloc[-1] if not vix.empty else 0.0
        vix_change = _calc_change(vix['Close'].iloc[0], vix_last) if not vix.empty else 0.0

        # CONTAMINATION CHANNEL (ADR-022): 绝对价位/宏观点位逐字进 prompt → 记忆过历史的 LLM 可反推年代;归一化能压低但杀纪律规则(VIX>20=fear 吃绝对值),不可消除。
        report = f"""
--- MACRO INDICATORS (Reference) ---
1. US 10Y Treasury Yield (^TNX): {tnx_last:.2f}% (MoM: {tnx_change:+.2%})
   *Note: Rising yields often hurt tech stock valuations.*

2. CBOE Volatility Index (^VIX): {vix_last:.2f} (MoM: {vix_change:+.2%})
   *Note: VIX > 20 indicates fear; VIX < 15 indicates complacency.*
"""

        # 货币因素（黄金/商品类资产的"基本面"）—— 逐项 graceful，缺一项不影响其余。
        # DX-Y.NYB = ICE 美元指数（DX=F 已被 Yahoo 下架，必须用 DX-Y.NYB）。
        dxy_last, dxy_change = _safe_last_change("DX-Y.NYB")
        if dxy_last is not None:
            report += (
                f"\n3. US Dollar Index (DXY, DX-Y.NYB): {dxy_last:.2f} (MoM: {dxy_change:+.2%})\n"
                f"   *Note: Strong/rising USD pressures gold & commodities; weak USD is a tailwind.*\n"
            )
        # 实际利率代理：TIP = iShares TIPS Bond ETF。TIP 价涨 = 实际利率走低 = 利好黄金；
        # TIP 价跌 = 实际利率走高 = 利空黄金。yahoo 内可达，绕开 FRED DFII10 的网络不稳。
        _tip_last, tip_change = _safe_last_change("TIP")
        if tip_change is not None:
            _dir = "falling (gold tailwind)" if tip_change > 0 else "rising (gold headwind)"
            report += (
                f"\n4. Real-rate proxy (TIPS ETF TIP, MoM): {tip_change:+.2%} → real yields {_dir}\n"
                f"   *Note: TIP up = real yields down = bullish gold; TIP down = real yields up = bearish gold.*\n"
            )

        return report
    except Exception as e:
        return f"Error fetching macro data: {e}"


def get_full_market_data(target_asset: str, fx_symbol: Optional[str] = None) -> str:
    """合并目标资产 + 关联汇率的多时间维度报告

    Args:
        target_asset: yfinance ticker（510300.SS / AAPL / NDQ.AX 等都行，
            必填——之前默认 "NDQ.AX" 会让 fork 用户错拉到作者持仓数据）
        fx_symbol: 关联汇率 ticker（None 则 daily_report 单独决定）
    """
    df_asset = get_history_data(target_asset, "2y")
    report_asset = analyze_multi_timeframe(df_asset, f"TARGET ASSET ({target_asset})")

    if fx_symbol is None:
        return report_asset

    df_fx = get_history_data(fx_symbol, "2y")
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
