import pandas as pd
import numpy as np
import os
import yfinance as yf
from datetime import datetime

# 缓存目录
CACHE_DIR = "cache_data"


# -----------------------------
# 1. 基础数据获取 (Master Cache)
# -----------------------------
def get_stock_history(symbol: str = "NDQ.AX", period: str = "2y") -> pd.DataFrame:
    """
    获取“主数据”。
    策略：总是获取最长周期(2y)的数据并缓存。
    其他短周期(1w, 1m, 6m, 1y)直接从这份数据中切片，无需单独请求/存储。
    这样保证了所有时间维度的数据一致性。
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    # 缓存文件名包含周期，例如 NDQ.AX_2y.csv
    csv_path = os.path.join(CACHE_DIR, f"{symbol}_{period}.csv")

    # [Hit] 检查缓存：如果是今天的文件，直接读取
    if os.path.exists(csv_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(csv_path))
        if file_time.date() == datetime.now().date():
            # print(f"✅ [Cache Hit] 读取本地数据: {symbol}")
            return pd.read_csv(csv_path, index_col=0, parse_dates=True)

    # [Miss] 请求 API 并写入缓存
    print(f"🔄 [API Update] 正在更新数据: {symbol}...")
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if not hist.empty:
            hist.to_csv(csv_path)
            return hist
    except Exception as e:
        print(f"⚠️ 获取历史数据失败: {e}")

    return pd.DataFrame()


# -----------------------------
# 2. 核心计算逻辑 (数学工具)
# -----------------------------
def _calc_change(start: float, end: float) -> float:
    """计算涨跌幅"""
    if start == 0: return 0.0
    return (end - start) / start


def _calc_max_drawdown(series: pd.Series) -> float:
    """计算期间最大回撤"""
    if series.empty: return 0.0
    roll_max = series.cummax()
    drawdown = (series - roll_max) / roll_max
    return drawdown.min()


def _calc_volatility(series: pd.Series) -> float:
    """计算年化波动率 (需数据量 > 2)"""
    if len(series) < 2: return 0.0
    # 年化系数: sqrt(252)
    return series.pct_change().std() * np.sqrt(252)


def _calc_rsi(series: pd.Series, period: int = 14) -> float:
    """计算 RSI 指标"""
    if len(series) < period + 1: return 50.0  # 数据不足时返回中性
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    if loss.iloc[-1] == 0: return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


# -----------------------------
# 3. 切片分析逻辑 (通用化)
# -----------------------------
def _analyze_slice(df_slice: pd.DataFrame, label: str, current_price: float) -> str:
    """
    通用函数：分析任意一个时间切片的数据，返回格式化字符串。
    """
    if df_slice.empty:
        return f"- **{label}**: No Data"

    start_price = df_slice['Close'].iloc[0]
    change = _calc_change(start_price, current_price)
    mdd = _calc_max_drawdown(df_slice['Close'])

    # 波动率只在数据量足够时计算 (大于20天)
    vol_str = ""
    if len(df_slice) > 20:
        vol = _calc_volatility(df_slice['Close'])
        vol_str = f", Volatility: {vol:.2%}"

    return (
        f"- **{label}** ({len(df_slice)} days): "
        f"Return: {change:.2%}, Max Drawdown: {mdd:.2%}{vol_str}"
    )


# -----------------------------
# 4. 主入口：多维度分析
# -----------------------------
def analyze_multi_timeframe(hist: pd.DataFrame) -> str:
    """
    [LLM Prompt Generator]
    将2年数据切分为: 1周, 1月, 6月, 1年, 2年。
    """
    if hist.empty:
        return "数据缺失: 无法获取历史数据"

    # 获取最新价格
    current_price = hist['Close'].iloc[-1]

    # --- 关键均线 ---
    # 20日(月线), 120日(半年线), 250日(年线)
    ma_20 = hist['Close'].rolling(window=20).mean().iloc[-1]
    ma_120 = hist['Close'].rolling(window=120).mean().iloc[-1]
    ma_250 = hist['Close'].rolling(window=250).mean().iloc[-1]
    rsi_14 = _calc_rsi(hist['Close'])

    # --- 数据切片 (基于交易日近似值) ---
    # 1周=5, 1月=21, 6月=126, 1年=252
    slices = {
        "1-Week (Short-term)": hist.tail(5),
        "1-Month (Medium-term)": hist.tail(21),
        "6-Months (Trend)": hist.tail(126),
        "1-Year (Annual)": hist.tail(252),
        "2-Years (Macro)": hist  # 全部数据
    }

    # --- 生成报告文本 ---
    report_lines = [f"[MULTI-DIMENSIONAL QUANT DATA]",
                    f"Symbol: NDQ.AX | Current Price: ${current_price:.2f} | RSI(14): {rsi_14:.2f}", "",
                    "**Performance by Timeframe:**"]

    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("")
    report_lines.append("**Moving Average (Support/Resistance):**")
    report_lines.append(f"- MA20 (Short-term): ${ma_20:.2f}")
    report_lines.append(f"- MA120 (Half-Year): ${ma_120:.2f}")
    report_lines.append(f"- MA250 (Yearly Baseline): ${ma_250:.2f}")

    # 计算偏离度
    if pd.notna(ma_250):
        bias = (current_price / ma_250 - 1)
        report_lines.append(f"- Deviation from MA250: {bias:.2%}")

    return "\n".join(report_lines)


# --- 测试运行 ---
if __name__ == "__main__":
    # 只需要调用一次，内部自动切片
    df = get_stock_history("NDQ.AX", "2y")
    print(analyze_multi_timeframe(df))