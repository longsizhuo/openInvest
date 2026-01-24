import yfinance as yf
import shutil
import os
import sys

# 1. 测试 YFinance
print("Testing Yahoo Finance connection...")
ticker = "NDQ.AX"
try:
    data = yf.Ticker(ticker)
    hist = data.history(period="1d")
    if not hist.empty:
        print(f"✅ Success! {ticker} Last Close: {hist['Close'].iloc[-1]}")
    else:
        print(f"⚠️ Warning: No data returned for {ticker}")
except Exception as e:
    print(f"❌ Error connecting to YFinance: {e}")

# 2. 测试汇率
print("\nTesting Exchange Rate (AUDCNY=X)...")
try:
    forex = yf.Ticker("AUDCNY=X")
    hist = forex.history(period="1d")
    if not hist.empty:
        print(f"✅ Success! AUDCNY=X Rate: {hist['Close'].iloc[-1]}")
    else:
        print("⚠️ Warning: No forex data returned")
except Exception as e:
    print(f"❌ Error connecting to YFinance (Forex): {e}")

# 3. 简单的 ChromaDB 初始化测试
print("\nTesting ChromaDB Initialization...")
try:
    # 尝试模拟 agent.py 中的初始化
    from langchain_chroma import Chroma
    from langchain_openai import OpenAIEmbeddings
    from dotenv import load_dotenv
    
    load_dotenv()
    
    embeddings = OpenAIEmbeddings(
        openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
        openai_api_base=os.getenv("DEEPSEEK_BASE_URL")
    )
    
    # 尝试创建一个临时的 db
    test_db_dir = "test_db_init"
    if os.path.exists(test_db_dir):
        shutil.rmtree(test_db_dir)
        
    vectorstore = Chroma(
        persist_directory=test_db_dir,
        collection_name="test_collection",
        embedding_function=embeddings,
    )
    print("✅ ChromaDB initialized successfully.")
    
    # 清理
    if os.path.exists(test_db_dir):
        shutil.rmtree(test_db_dir)

except Exception as e:
    print(f"❌ Error initializing ChromaDB: {e}")
