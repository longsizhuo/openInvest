import os
from typing import Optional
from dotenv import load_dotenv
from agent import SimpleAgent
from exchange_fee import get_rate, get_stock
from news import get_real_finance_news

# 加载 .env 文件中的环境变量
load_dotenv()

def LLM() -> Optional[SimpleAgent]:
    # 1. 获取 DeepSeek 配置
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    if not deepseek_api_key:
        print("错误: 未在 .env 文件中找到 DEEPSEEK_API_KEY。请确保创建了 .env 文件并配置了该变量。")
        return None

    print(f"正在初始化 Agent (Model: deepseek-chat, Base URL: {deepseek_base_url})...")

    # 2. 创建 Agent
    # DeepSeek 兼容 OpenAI 接口，我们使用 openai_api_key 和 openai_api_base 参数
    # 启用 enable_search 才能让 Agent 使用我们新添加的 finance_news 工具
    return SimpleAgent(
        temperature=0.1,
        enable_search=True, # 修正此处，启用搜索功能
        model="deepseek-chat",
        openai_api_key=deepseek_api_key,
        openai_api_base=deepseek_base_url,
        verbose=True
    )

def main():
    # 1. 查询汇率，获得汇率系数 rate=10-rate , 纳指系数
    rate, stock = get_rate(), get_stock()
    if rate == 0 or not stock:
        print('')
        return
    agent = LLM() # 修正此处，LLM 函数不接受参数
    if agent:
        response = agent.run("Please help me analyze the recent important financial news on the NASDAQ market and their potential impact on the market.")
        print(response)


if __name__ == "__main__":
    main()
