import logging
import os
from datetime import datetime
import yfinance as yf
import pandas as pd

# 缓存目录，避免频繁请求 API
CACHE_DIR = "cache_data"

# get_rate() 返回一个系数
# 逻辑：汇率(AUDCNY)越低，返回值(10-rate)越高，代表投资性价比越高
def get_rate() -> float :
    try:
        rate_ticker = yf.Ticker("AUDCNY=X")
        rate = rate_ticker.history(period="1d")['Close'].iloc[-1]
        return round(rate, 4)
    except Exception as e:
        print(f"⚠️ 汇率接口异常: {e}")
        return 0.0

# --- 获取股价 (yfinance) ---
def get_stock() -> float:
    price = None
    try:
        # yfinance 内部其实处理得不错，但我们要包一层防崩
        ticker = yf.Ticker("NDQ.AX")
        # 获取当天数据
        hist = ticker.history(period="1d")
        if not hist.empty:
            price = hist['Close'].iloc[-1]
            print(f"✅ 股价获取成功: ${price:.2f}")
        else:
            print("⚠️ 股市休市或数据延迟")
    except Exception as e:
        print(f"⚠️ 股价接口异常: {e}")
    return price

# --- 获取历史数据 (带缓存) ---
def get_stock_history(symbol: str = "NDQ.AX", period: str = "2y") -> pd.DataFrame:
    """
    获取历史数据。建议 period 至少为 "1y" 甚至 "2y"，
    这样才能计算出稳定的 MA250 (年线)。
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    csv_path = os.path.join(CACHE_DIR, f"{symbol}_{period}.csv")

    # 缓存命中逻辑 (Hit)
    if os.path.exists(csv_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(csv_path))
        if file_time.date() == datetime.now().date():
            # print(f"✅ [Cache] 命中本地数据: {symbol}")
            return pd.read_csv(csv_path, index_col=0, parse_dates=True)

    # 缓存未命中 (Miss) -> 请求 API
    print(f"🔄 [API] 正在更新数据: {symbol}...")
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if not hist.empty:
            hist.to_csv(csv_path)
            return hist
    except Exception as e:
        print(f"⚠️ 获取历史数据失败: {e}")

    return pd.DataFrame()

# --- 3. 增强：加入均线分析 ---
def analyze_history(hist: pd.DataFrame) -> str:
    """
    计算技术指标，生成 Prompt 素材。
    核心增加了：MA120(半年线) 和 MA250(年线) 的对比。
    """
    if hist.empty:
        return "暂无历史数据。"

    # 基础数据
    current_price = hist['Close'].iloc[-1]
    start_price = hist['Close'].iloc[0]
    change_pct = ((current_price - start_price) / start_price) * 100

    high_year = hist['High'].max()
    low_year = hist['Low'].min()

    # --- 关键技术指标计算 (Pandas Rolling) ---
    # 半年线 (约120个交易日)
    ma_120 = hist['Close'].rolling(window=120).mean().iloc[-1]
    # 年线 (约250个交易日)
    ma_250 = hist['Close'].rolling(window=250).mean().iloc[-1]

    # 乖离率 (当前价格偏离年线的幅度)
    bias_250 = ((current_price - ma_250) / ma_250) * 100 if pd.notna(ma_250) else 0

    return f"""
    【技术面分析数据】
    1. 价格表现:
       - 最新收盘: ${current_price:.2f}
       - 过去{len(hist)}天涨幅: {change_pct:.2f}%
       - 价格区间: ${low_year:.2f} - ${high_year:.2f}
    
    2. 均线估值 (核心指标):
       - 半年线(MA120): ${ma_120:.2f}
       - 年线(MA250): ${ma_250:.2f} (长期成本线)
       - 当前偏离度: {bias_250:.2f}% 
         (注: 正值代表高于年线，负值代表低于年线/被低估)
    """

if __name__ == "__main__":
    # 获取2年数据，确保能算出年线
    df = get_stock_history(period="2y")
    summary = analyze_history(df)
    print(summary)