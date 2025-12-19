import os
from dotenv import load_dotenv
from agent import SimpleAgent

# 加载 .env 文件中的环境变量
load_dotenv()

def main():
    # 1. 获取 DeepSeek 配置
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    if not deepseek_api_key:
        print("错误: 未在 .env 文件中找到 DEEPSEEK_API_KEY。请确保创建了 .env 文件并配置了该变量。")
        return

    print(f"正在初始化 Agent (Model: deepseek-chat, Base URL: {deepseek_base_url})...")

    # 2. 创建 Agent
    # DeepSeek 兼容 OpenAI 接口，我们使用 openai_api_key 和 openai_api_base 参数
    # 建议先关闭 enable_search (设置为 False)，除非你已经配置了 Bing 搜索相关的环境变量
    agent = SimpleAgent(
        temperature=0.1,
        enable_search=False, 
        model="deepseek-chat",
        openai_api_key=deepseek_api_key,
        openai_api_base=deepseek_base_url,
        verbose=True
    )

    # 3. 运行示例查询
    query = "你好，请做一个简单的自我介绍，并告诉我你能帮我做什么？"
    print(f"\nUser: {query}")
    
    try:
        response = agent.run(query)
        print(f"Agent: {response}")
    except Exception as e:
        print(f"运行出错: {e}")

if __name__ == "__main__":
    main()
