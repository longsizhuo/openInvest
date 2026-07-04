import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from openinvest.db.market_store import MarketStore

# B6: betashares_scraper 历史上做 NDQ.AX 专用爬取，但 BetaShares 站点长期 403
# + 它返的 etf_holdings/sectors/stats 全仓零消费 → 改成统一 yfinance 路径，
# 文件保留作 optional plugin（万一未来想看 ETF top10 持仓再启用）。

CACHE_DIR = "cache_data"
_STORE = MarketStore()

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
# 0. 数据结构定义
# ==========================================
@dataclass
class ForexFriction:
    input_cny: float
    net_aud: float
    spot_rate: float
    effective_rate: float
    friction_pct: float
    total_fee_cny: float
    break_even_pct: float
    is_viable: bool


@dataclass
class StockFriction:
    input_aud: float
    fee_aud: float
    friction_pct: float


@dataclass
class CostSnapshot:
    invest_cny: float
    spot_rate: float
    forex: ForexFriction
    trade_aud: float
    stock: StockFriction
    combined_fee_cny: Optional[float]
    combined_friction_pct: Optional[float]


class TransactionCostCalculator:
    def __init__(self):
        self.cn_cable_fee = 150.0
        self.cn_commission_rate = 0.001
        self.cn_commission_min = 50.0
        self.cn_commission_max = 260.0
        self.au_inward_fee = 15.0
        self.commsec_tier_1 = 5.0
        self.commsec_tier_2 = 10.0
        self.commsec_tier_3 = 19.95
        self.commsec_rate_high = 0.0012

    def calculate_forex_friction(self, invest_cny: float, spot_rate: float) -> ForexFriction:
        if invest_cny <= 0 or spot_rate <= 0:
            return ForexFriction(0, 0, 0, 0, 0, 0, 0, False)

        commission = max(self.cn_commission_min, min(invest_cny * self.cn_commission_rate, self.cn_commission_max))
        cn_total_fee = self.cn_cable_fee + commission
        remaining_cny = invest_cny - cn_total_fee

        if remaining_cny <= 0:
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, cn_total_fee, float('inf'), False)

        gross_aud = remaining_cny / spot_rate
        net_aud = gross_aud - self.au_inward_fee

        if net_aud <= 0:
            total_fee_cny_equiv = cn_total_fee + (gross_aud * spot_rate)
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, total_fee_cny_equiv, float('inf'), False)

        effective_rate = invest_cny / net_aud
        value_loss_cny = invest_cny - (net_aud * spot_rate)
        friction_pct = (value_loss_cny / invest_cny) * 100
        break_even_pct = (1 / (1 - friction_pct / 100) - 1) * 100 if friction_pct < 100 else float('inf')

        return ForexFriction(
            input_cny=invest_cny,
            net_aud=net_aud,
            spot_rate=spot_rate,
            effective_rate=effective_rate,
            friction_pct=friction_pct,
            total_fee_cny=value_loss_cny,
            break_even_pct=break_even_pct,
            is_viable=True
        )

    def calculate_stock_friction(self, amount_aud: float) -> StockFriction:
        if amount_aud <= 0:
            return StockFriction(0, 0, 0)
        
        if amount_aud <= 1000:
            fee = self.commsec_tier_1
        elif amount_aud <= 10000:
            fee = self.commsec_tier_2
        elif amount_aud <= 25000:
            fee = self.commsec_tier_3
        else:
            fee = amount_aud * self.commsec_rate_high

        friction_pct = (fee / amount_aud) * 100
        return StockFriction(input_aud=amount_aud, fee_aud=fee, friction_pct=friction_pct)


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
        try:
            print(f"🔄 [yfinance] Refreshing {symbol} (period={fetch_period})...")
            ticker = yf.Ticker(symbol)
            df_yf = ticker.history(period=fetch_period)
            if not df_yf.empty:
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

    if not df_db.empty:
        return _apply_cutoff(df_db, as_of_date)

    # 3. 兜底：读旧 CSV 缓存同步至 DB
    safe_symbol = symbol.replace("=", "").replace(".", "_").replace("/", "")
    csv_path = os.path.join(CACHE_DIR, f"{safe_symbol}_{period}.csv")
    if os.path.exists(csv_path):
        print(f"⚠️ [Emergency] DB Empty. Using legacy CSV for {symbol}")
        try:
            df_csv = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if not df_csv.empty:
                for idx, row in df_csv.iterrows():
                    _STORE.save_generic_price(symbol, idx.strftime('%Y-%m-%d'), row['Close'], source="legacy_csv")
                return _apply_cutoff(df_csv, as_of_date)
        except Exception:
            pass

    return pd.DataFrame()


def _apply_cutoff(df: pd.DataFrame, as_of_date: Optional[str]) -> pd.DataFrame:
    """把 df 截到 cutoff 当日（含）。

    语义：T 日决策时**可以看 T 日的 close**（用户场景：晚间 cron 跑委员会 / 用户睡前
    查 verdict，市场已收盘）。所以保留 `index <= cutoff` 数据，去掉 cutoff 之后所有
    交易日（保证 LLM 看不到未来）。

    跟 backtest_committee.py:_patch_tools_to_date 原有的 `df.index < next_day`
    语义一致。
    """
    if as_of_date is None or df.empty:
        return df
    cutoff = pd.to_datetime(as_of_date)
    # df.index 可能是 tz-aware（yfinance 默认带时区），cutoff 是 naive → 对齐
    try:
        if df.index.tz is not None:
            cutoff = cutoff.tz_localize(df.index.tz)
    except (AttributeError, TypeError):
        pass
    return df[df.index <= cutoff]


# ==========================================
# 2. 数学工具
# ==========================================
def _calc_change(start: float, end: float) -> float:
    if start == 0: return 0.0
    return (end - start) / start


def _calc_max_drawdown(series: pd.Series) -> float:
    if series.empty: return 0.0
    roll_max = series.cummax()
    drawdown = (series - roll_max) / roll_max
    return drawdown.min()


def _calc_volatility(series: pd.Series) -> float:
    if len(series) < 2: return 0.0
    return series.pct_change().std() * np.sqrt(252)


def _calc_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1: return 50.0
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    if loss.iloc[-1] == 0: return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def _analyze_slice(df_slice: pd.DataFrame, label: str, current_price: float) -> str:
    if df_slice.empty:
        return f"- **{label}**: No Data"
    start_price = df_slice['Close'].iloc[0]
    change = _calc_change(start_price, current_price)
    mdd = _calc_max_drawdown(df_slice['Close'])
    vol_str = ""
    if len(df_slice) > 20:
        vol = _calc_volatility(df_slice['Close'])
        vol_str = f", Vol: {vol:.2%}"
    return f"- **{label}**: Ret: {change:.2%}, MaxDD: {mdd:.2%}{vol_str}"


def analyze_multi_timeframe(hist: pd.DataFrame, title: str) -> str:
    """格式化层 — 数值计算交给 utils.market_metrics.compute_metrics（SSOT 唯一来源）。

    本函数只负责：拿 metrics dict + 切窗口算阶段收益 + 拼成给 LLM 看的字符串。
    任何 MA / RSI / 分位 / ATR 改动 → 改 utils/market_metrics.py，不要在这里加。
    """
    from .market_metrics import compute_metrics

    if hist.empty:
        return f"数据缺失: {title}"

    metrics = compute_metrics(hist)
    current_price = metrics["current_price"]
    if current_price is None:
        return f"数据缺失: {title}"

    ma_120 = metrics["ma120"]
    ma_250 = metrics["ma250"]
    rsi_14 = metrics["rsi14"]
    pos = metrics["price_quantile_2y"]
    rvol = metrics.get("rvol")

    slices = {
        "1-Week": hist.tail(5),
        "1-Month": hist.tail(21),
        "6-Months": hist.tail(126),
        "1-Year": hist.tail(252),
        "2-Years": hist
    }

    rsi_str = f"{rsi_14:.2f}" if rsi_14 is not None else "N/A"
    # CONTAMINATION CHANNEL (ADR-022): 绝对价位/宏观点位逐字进 prompt → 记忆过历史的 LLM 可反推年代;归一化能压低但杀纪律规则(VIX>20=fear 吃绝对值),不可消除。
    report_lines = [
        f"--- {title} ANALYSIS ---",
        # RSI(14) 为 Wilder 平滑（与 TradingView/券商口径一致）
        f"Current Price: {current_price:.4f} | RSI(14, Wilder): {rsi_str}",
    ]

    if pos is not None:
        # 真百分位排名：历史 X% 的交易日收盘价 ≤ 当前价（不是区间归一位置）
        report_lines.append(
            f"Price Percentile (2y): {pos:.0%} (历史 {pos:.0%} 交易日收盘价 ≤ 当前价)"
        )
    if rvol is not None:
        # 相对成交量：> 1 放量，< 1 缩量（依赖 DB 补存 Volume）
        report_lines.append(f"RVOL(20): {rvol:.2f}x (当日量 / 前 20 日均量)")

    report_lines.append("**Timeframe Performance:**")
    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("**Key Levels:**")
    if ma_120 is not None:
        report_lines.append(f"- MA120 (Trend): {ma_120:.4f}")
    if ma_250 is not None:
        report_lines.append(f"- MA250 (Base): {ma_250:.4f}")
        if ma_250 != 0:
            bias = (current_price / ma_250 - 1)
            report_lines.append(f"- MA250 Deviation: {bias:.2%}")

    return "\n".join(report_lines)


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
            # Last resort: Try stale cache from file
            safe_symbol = "AUDCNY=X".replace("=", "").replace(".", "_").replace("/", "")
            stale_path = os.path.join(CACHE_DIR, f"{safe_symbol}_2y.csv")
            if os.path.exists(stale_path):
                print("⚠️ [Emergency] Using stale cache for spot rate.")
                try:
                    df_stale = pd.read_csv(stale_path, index_col=0, parse_dates=True)
                    spot_rate = float(df_stale['Close'].iloc[-1])
                except:
                    spot_rate = 0.0
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


def format_cost_report(snapshot: CostSnapshot) -> str:
    fx = snapshot.forex
    stock = snapshot.stock

    lines = [
        "--- FRICTION COST REPORT (Pre-calculated) ---",
        f"Input CNY: ¥{snapshot.invest_cny:.2f}",
        f"Spot Rate (AUD/CNY): {snapshot.spot_rate:.4f}",
        "",
        "[Scenario 1: Forex Transfer (CNY -> AUD)]",
        f"- Net AUD Received: ${fx.net_aud:.2f}",
        f"- Effective Rate (after fees): {fx.effective_rate:.4f}",
        f"- Total Friction Loss: {fx.friction_pct:.2f}% (¥{fx.total_fee_cny:.2f})",
        f"- Break-even Requirement: AUD must appreciate {fx.break_even_pct:.2f}%",
    ]
    if not fx.is_viable:
        lines.append("- Status: Not viable (fees exceed principal or inbound fees)")

    lines.extend([
        "",
        "[Scenario 2: Stock Trading (AUD -> NDQ)]",
        f"- Trade AUD: ${snapshot.trade_aud:.2f}",
        f"- Brokerage Fee: ${stock.fee_aud:.2f}",
        f"- Friction Loss: {stock.friction_pct:.2f}%",
    ])

    if snapshot.combined_fee_cny is not None:
        lines.extend([
            "",
            "[Scenario 3: Combined (FX + Brokerage)]",
            f"- Total Friction Loss: {snapshot.combined_friction_pct:.2f}% (¥{snapshot.combined_fee_cny:.2f})"
        ])

    return "\n".join(lines)


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
