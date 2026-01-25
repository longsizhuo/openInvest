import pandas as pd
import requests
import io
import re

def get_stooq_history(symbol):
    """从 stooq.com 获取历史数据 (CSV 接口)"""
    # Stooq 常用代码转换
    stooq_symbol = symbol.lower()
    if "audcny=x" in stooq_symbol:
        stooq_symbol = "audcny"
    elif "^tnx" in stooq_symbol:
        stooq_symbol = "10usy.b" # Stooq 的 10Y Yield
    elif "^vix" in stooq_symbol:
        stooq_symbol = "^vix"
    
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        
        # 检查是否包含有效数据
        if "N/D" in r.text or len(r.text.strip().split('\n')) < 2:
            print(f"⚠️ Stooq: No data for {stooq_symbol}")
            return pd.DataFrame()

        df = pd.read_csv(io.StringIO(r.text))
        
        # Stooq 返回的 CSV 列名可能是大写也可能是小写，且可能包含空格
        df.columns = [c.strip().capitalize() for c in df.columns]
        
        if "Close" not in df.columns:
            print(f"⚠️ Stooq columns error for {stooq_symbol}: {df.columns.tolist()}")
            return pd.DataFrame()
            
        # 处理 N/D
        if pd.isna(df["Close"].iloc[0]):
            return pd.DataFrame()

        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date")
            
        return df
    except Exception as e:
        print(f"⚠️ Stooq Failed for {symbol}: {e}")
        return pd.DataFrame()

def get_stooq_forex(symbol="AUDCNY"):
    """获取实时汇率 (Stooq)"""
    df = get_stooq_history(symbol)
    if not df.empty:
        try:
            return float(df["Close"].iloc[0])
        except:
            return None
    return None