import os
from typing import Optional
from dotenv import load_dotenv
from agent import SimpleAgent
from exchange_fee import get_rate, get_stock
from news import get_real_finance_news

# 加载 .env 文件中的环境变量
load_dotenv()

def LLM(query) -> Optional[SimpleAgent]:
    # 1. 获取 DeepSeek 配置
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    if not deepseek_api_key:
        print("错误: 未在 .env 文件中找到 DEEPSEEK_API_KEY。请确保创建了 .env 文件并配置了该变量。")
        return None

    print(f"正在初始化 Agent (Model: deepseek-chat, Base URL: {deepseek_base_url})...")

    # 2. 创建 Agent
    # DeepSeek 兼容 OpenAI 接口，我们使用 openai_api_key 和 openai_api_base 参数
    # 建议先关闭 enable_search (设置为 False)，除非你已经配置了 Bing 搜索相关的环境变量
    return SimpleAgent(
        temperature=0.1,
        enable_search=False,
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

    news = get_real_finance_news(
        topic_query="Nasdaq outlook Australia",
        max_results=25,
        whitelist_domains=None,   # 你后续可加：["reuters.com", "ft.com", ...]
        blacklist_domains=None,   # 你后续可加：明确低质量站点
        extract_fulltext=True,    # 强烈建议装 trafilatura
        sleep_sec=0.0
    )

    # 先简单看看结果
    print("=== TRUSTED ===")
    for x in news["trusted"][:5]:
        print(x["score"], x["title"], x["domain"])

    print("\n=== REVIEW ===")
    for x in news["review"][:5]:
        print(x["score"], x["title"], x["flags"])

    print("\n=== FILTERED ===")
    for x in news["filtered"][:5]:
        print(x["score"], x["flags"], x["title"])

if __name__ == "__main__":
    main()
