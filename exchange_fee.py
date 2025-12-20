import pandas as pd
import numpy as np
import os
import yfinance as yf
from datetime import datetime

CACHE_DIR = "cache_data"


# -----------------------------
# 1. 通用数据获取 (Generic Fetcher)
# -----------------------------
def get_history_data(symbol: str, period: str = "2y") -> pd.DataFrame:
    """
    通用获取函数：既可以抓股票(NDQ.AX)，也可以抓汇率(AUDCNY=X)。
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    # 处理一下文件名，避免 symbol 里有特殊字符导致路径错误
    safe_symbol = symbol.replace("=", "").replace(".", "_")
    csv_path = os.path.join(CACHE_DIR, f"{safe_symbol}_{period}.csv")

    # [Hit]
    if os.path.exists(csv_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(csv_path))
        if file_time.date() == datetime.now().date():
            return pd.read_csv(csv_path, index_col=0, parse_dates=True)

    # [Miss]
    print(f"🔄 [API Update] 正在更新数据: {symbol}...")
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if not hist.empty:
            hist.to_csv(csv_path)
            return hist
    except Exception as e:
        print(f"⚠️ 获取数据失败 {symbol}: {e}")

    return pd.DataFrame()


# -----------------------------
# 2. 数学工具 (保持不变)
# -----------------------------
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


# -----------------------------
# 3. 分析逻辑 (支持传入自定义标题)
# -----------------------------
def analyze_multi_timeframe(hist: pd.DataFrame, title: str) -> str:
    """
    通用分析器：传入 Dataframe 和 标题(如 'NDQ' 或 'AUD/CNY')
    """
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

    report_lines = []
    # 使用 Markdown 分隔线区分不同资产
    report_lines.append(f"--- {title} ANALYSIS ---")
    report_lines.append(f"Current Price: {current_price:.4f} | RSI(14): {rsi_14:.2f}")

    # 判断当前价格位置 (Percentile)
    high_2y = hist['Close'].max()
    low_2y = hist['Close'].min()
    pos = (current_price - low_2y) / (high_2y - low_2y)
    report_lines.append(f"Price Rank (2y): {pos:.0%} (0%=Low, 100%=High)")

    report_lines.append("**Timeframe Performance:**")
    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("**Key Levels:**")
    report_lines.append(f"- MA120 (Trend): {ma_120:.4f}")
    report_lines.append(f"- MA250 (Base): {ma_250:.4f}")
    if pd.notna(ma_250):
        bias = (current_price / ma_250 - 1)
        report_lines.append(f"- MA250 Deviation: {bias:.2%}")

    return "\n".join(report_lines)


# -----------------------------
# 4. 对外接口：获取整合报告
# -----------------------------
def get_macro_data() -> str:
    """
    获取宏观关键指标：10年期美债收益率 (^TNX) 和 恐慌指数 (^VIX)
    """
    try:
        tnx = get_history_data("^TNX", "1mo") # 10-Year Treasury Yield
        vix = get_history_data("^VIX", "1mo") # CBOE Volatility Index
        
        tnx_last = tnx['Close'].iloc[-1] if not tnx.empty else 0.0
        tnx_change = _calc_change(tnx['Close'].iloc[0], tnx_last)
        
        vix_last = vix['Close'].iloc[-1] if not vix.empty else 0.0
        vix_change = _calc_change(vix['Close'].iloc[0], vix_last)
        
        return f"""
--- MACRO INDICATORS (Reference) ---
1. US 10Y Treasury Yield (^TNX): {tnx_last:.2f}% (MoM: {tnx_change:+.2%})
   *Note: Rising yields often hurt tech stock valuations.*

2. CBOE Volatility Index (^VIX): {vix_last:.2f} (MoM: {vix_change:+.2%})
   *Note: VIX > 20 indicates fear; VIX < 15 indicates complacency.*
"""
    except Exception as e:
        return f"Error fetching macro data: {e}"

def get_full_market_data() -> str:
    # 1. 获取 纳指ETF (NDQ.AX) 数据
    df_ndq = get_history_data("NDQ.AX", "2y")
    report_ndq = analyze_multi_timeframe(df_ndq, "TARGET ASSET (NDQ.AX)")

    # 2. 获取 澳元兑人民币 (AUDCNY=X) 数据
    # Yahoo Finance 代码: AUDCNY=X
    df_fx = get_history_data("AUDCNY=X", "2y")
    report_fx = analyze_multi_timeframe(df_fx, "CURRENCY RATE (AUD/CNY)")

    return f"""
{report_ndq}

{report_fx}
"""


# --- 测试 ---
if __name__ == "__main__":
    print(get_full_market_data())