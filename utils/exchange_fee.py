import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from .betashares_scraper import scrape_full_ndq_data
from db.market_store import MarketStore

CACHE_DIR = "cache_data"
_STORE = MarketStore()


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
def get_history_data(symbol: str, period: str = "2y") -> pd.DataFrame:
    symbol = symbol.upper()
    
    # 1. 尝试从数据库获取
    df_db = _STORE.get_history_df(symbol)
    
    # 2. 判断是否需要更新 (如果今天还没更新过)
    today_str = datetime.now().strftime('%Y-%m-%d')
    needs_update = df_db.empty or df_db.index[-1].strftime('%Y-%m-%d') != today_str

    if needs_update:
        if symbol == "NDQ.AX":
            print(f"📡 [Scraper] Updating database for {symbol}...")
            scrape_ok = False
            try:
                snapshot = scrape_full_ndq_data()
                if snapshot and snapshot["date"] and snapshot["nav"]:
                    _STORE.save_ndq_snapshot(
                        snapshot["date"], snapshot["nav"],
                        snapshot["stats"], snapshot["holdings"], snapshot["sectors"]
                    )
                    df_db = _STORE.get_history_df(symbol)
                    scrape_ok = True
            except Exception as e:
                print(f"❌ Scraper Error: {e}")

            # NDQ.AX scraper 经常被 BetaShares 反爬 403。如果失败，fallback 到 yfinance
            # 只能拉到 close 价（拿不到 holdings/sectors），但起码估值能跑下去
            if not scrape_ok:
                try:
                    print(f"🔄 [yfinance fallback] Refreshing {symbol}...")
                    df_yf = yf.Ticker(symbol).history(period="5d")
                    if not df_yf.empty:
                        for idx, row in df_yf.iterrows():
                            _STORE.save_generic_price(
                                symbol, idx.strftime('%Y-%m-%d'),
                                row['Close'], source="yfinance_fallback"
                            )
                        df_db = _STORE.get_history_df(symbol)
                except Exception as e:
                    print(f"❌ yfinance fallback also failed for {symbol}: {e}")
        else:
            try:
                print(f"🔄 [yfinance] Refreshing {symbol}...")
                ticker = yf.Ticker(symbol)
                df_yf = ticker.history(period="5d")
                if not df_yf.empty:
                    for idx, row in df_yf.iterrows():
                        _STORE.save_generic_price(symbol, idx.strftime('%Y-%m-%d'), row['Close'])
                    df_db = _STORE.get_history_df(symbol)
            except Exception as e:
                print(f"❌ yfinance sync failed for {symbol}: {e}")

    if not df_db.empty:
        return df_db

    # 3. 最终保底：读取旧 CSV 缓存并同步至 DB
    safe_symbol = symbol.replace("=", "").replace(".", "_").replace("/", "")
    csv_path = os.path.join(CACHE_DIR, f"{safe_symbol}_{period}.csv")
    if os.path.exists(csv_path):
        print(f"⚠️ [Emergency] DB Empty. Using legacy CSV for {symbol}")
        try:
            df_csv = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if not df_csv.empty:
                for idx, row in df_csv.iterrows():
                    _STORE.save_generic_price(symbol, idx.strftime('%Y-%m-%d'), row['Close'], source="legacy_csv")
                return df_csv
        except: pass

    return pd.DataFrame()


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
    if hist.empty:
        return f"数据缺失: {title}"

    current_price = hist['Close'].iloc[-1]
    ma_20 = hist['Close'].rolling(window=20).mean().iloc[-1]
    ma_120 = hist['Close'].rolling(window=120).mean().iloc[-1]
    ma_250 = hist['Close'].rolling(window=250).mean().iloc[-1]
    rsi_14 = _calc_rsi(hist['Close'])

    slices = {
        "1-Week": hist.tail(5),
        "1-Month": hist.tail(21),
        "6-Months": hist.tail(126),
        "1-Year": hist.tail(252),
        "2-Years": hist
    }

    report_lines = [f"--- {title} ANALYSIS ---", f"Current Price: {current_price:.4f} | RSI(14): {rsi_14:.2f}"]
    
    high_2y = hist['Close'].max()
    low_2y = hist['Close'].min()
    pos = (current_price - low_2y) / (high_2y - low_2y) if high_2y != low_2y else 0.5
    report_lines.append(f"Price Rank (2y): {pos:.0%} (0%=Low, 100%=High)")

    report_lines.append("**Timeframe Performance:**")
    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("**Key Levels:**")
    if pd.notna(ma_120): report_lines.append(f"- MA120 (Trend): {ma_120:.4f}")
    if pd.notna(ma_250): report_lines.append(f"- MA250 (Base): {ma_250:.4f}")
    if pd.notna(ma_250) and ma_250 != 0:
        bias = (current_price / ma_250 - 1)
        report_lines.append(f"- MA250 Deviation: {bias:.2%}")

    return "\n".join(report_lines)


# ==========================================
# 3. 对外接口
# ==========================================
def get_macro_data() -> str:
    try:
        tnx = get_history_data("^TNX", "1mo")
        vix = get_history_data("^VIX", "1mo")

        tnx_last = tnx['Close'].iloc[-1] if not tnx.empty else 0.0
        tnx_change = _calc_change(tnx['Close'].iloc[0], tnx_last) if not tnx.empty else 0.0

        vix_last = vix['Close'].iloc[-1] if not vix.empty else 0.0
        vix_change = _calc_change(vix['Close'].iloc[0], vix_last) if not vix.empty else 0.0

        return f"""
--- MACRO INDICATORS (Reference) ---
1. US 10Y Treasury Yield (^TNX): {tnx_last:.2f}% (MoM: {tnx_change:+.2%})
   *Note: Rising yields often hurt tech stock valuations.*

2. CBOE Volatility Index (^VIX): {vix_last:.2f} (MoM: {vix_change:+.2%})
   *Note: VIX > 20 indicates fear; VIX < 15 indicates complacency.*
"""
    except Exception as e:
        return f"Error fetching macro data: {e}"


def get_full_market_data(target_asset: str = "NDQ.AX") -> str:
    df_asset = get_history_data(target_asset, "2y")
    report_asset = analyze_multi_timeframe(df_asset, f"TARGET ASSET ({target_asset})")

    df_fx = get_history_data("AUDCNY=X", "2y")
    report_fx = analyze_multi_timeframe(df_fx, "CURRENCY RATE (AUD/CNY)")

    return f"""
{report_asset}

{report_fx}
"""


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
    print(get_full_market_data())
