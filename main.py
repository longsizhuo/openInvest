import os
from typing import Optional
from dotenv import load_dotenv
from agent import SimpleAgent
from exchange_fee import get_history_data, analyze_multi_timeframe
from portfolio_manager import PortfolioManager

from notifier import send_gmail_notification

load_dotenv()

PROMPT_FOREX_AGENT = """
You are a foreign exchange trading expert. Your task is to focus on analyzing the trend of AUD/CNY exchange rate.
You can only see data related to exchange rates.

**Core constraint: T+7 rule**
It takes 7 natural days for the exchange of Chinese yuan to Australian dollar to arrive.
This means that your suggestion today is for the fund reserve service next week (7 days later).
You don't need to consider the stock market situation, just focus on when it's most cost-effective to exchange currency.

**Tool usage strategy**
1. When searching for news, translate keywords into English. **CRITICAL: Do NOT search for opinions or forecasts (e.g., avoid "AUD CNY forecast", "prediction", "outlook").** Instead, search for *factual drivers* such as "RBA interest rate minutes", "China manufacturing PMI", "Iron ore prices", or "Australia inflation data".
2. You must form your own prediction based on these facts, rather than relying on search results for the conclusion.

Please analyze:
Is the current exchange rate high or low? (Reference historical percentile)
2. What is the expected trend for the next week (combined with news and technical aspects)?
Conclusion: Should we exchange Chinese yuan for Australian dollars now?

Please provide a brief and sharp analysis, and clearly indicate the tendency towards "suggested exchange" or "suggested waiting".
**Finally, please list 1-2 key news headlines that you have referenced. **
"""

PROMPT_STOCK_AGENT = """
You are a trader in the US/Australian stock market. Your task is to focus on analyzing the trend of NDQ.AX (NASDAQ 100 Australian ETF).
You can only see data related to stocks.

**Core Background**
The user's account already has some Australian dollars (AUD) in cash that can be bought at any time.
You don't need to care about exchange rates or funding sources, just focus on whether the current stock price is worth buying.

**Tool usage strategy**
1. When searching for news, translate keywords into English. **CRITICAL: Do NOT search for opinions or forecasts (e.g., avoid "NASDAQ outlook", "NDQ forecast").** Instead, search for *factual drivers* such as "US tech sector earnings", "Federal Reserve interest rate decision", "US CPI data", or "NASDAQ volatility".
2. Pay attention to the macro sentiment of the NASDAQ index and specific developments in the Australian market.

Please analyze:
1. Current stock price position (historical high/low)?
What signals do technical indicators (RSI, moving averages) send out?
Conclusion: Should we buy now?

Please provide a brief and sharp analysis, and clearly indicate the tendency of 'recommended buy', 'recommended hold', or 'recommended sell'.
**Finally, please list 1-2 key news headlines that you have referenced. **
"""

PROMPT_MANAGER_AGENT = """
You are a Chief Investment Advisor (Portfolio Manager).
You have a God's perspective and are responsible for synthesizing multiple information to make the final asset allocation decision.

**Decision logic**
1. * * Exchange Decision (CNY ->AUD) * *: Refer to the opinions of foreign exchange experts.
2. * * Trading Decision (AUD ->NDQ. AX) * *: Refer to the opinions of stock traders.

**Output requirements**
1. Briefly summarize the core conflicts or consensus between the two experts.
2. Provide specific operational recommendations based on user funds.
"""


# ==========================================
# 2. 工具函数
# ==========================================

def get_agent_config():
    return {
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY"),
        "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    }


def create_agent(system_prompt: str, model_name="deepseek-chat") -> Optional[SimpleAgent]:
    cfg = get_agent_config()
    if not cfg["deepseek_api_key"]:
        print("❌ Error: DEEPSEEK_API_KEY not found.")
        return None

    # 优化点：统一 Agent 初始化参数
    return SimpleAgent(
        temperature=0.1,  # 保持冷静
        enable_search=True,  # 允许专家上网查新闻
        model=model_name,
        openai_api_key=cfg["deepseek_api_key"],
        openai_api_base=cfg["deepseek_base_url"],
        system_prompt=system_prompt,  # 这里的参数名根据你的 SimpleAgent 实现可能需要调整
        debug=False
    )


# ==========================================
# 3. 主流程
# ==========================================

def main():
    print("🚀 启动多智能体协作系统 (Multi-Agent System)...")

    # --- 1. 初始化用户状态 ---
    pm = PortfolioManager()
    pm.process_income()

    # 获取实时价格用于估值
    try:
        # 获取 1d 数据即可，不用拉 2y，加快速度
        # 但 report 生成需要 2y
        df_ndq = get_history_data("NDQ.AX", "1d")
        current_price = df_ndq['Close'].iloc[-1]

        df_fx = get_history_data("AUDCNY=X", "1d")
        current_rate = df_fx['Close'].iloc[-1]
    except Exception as e:
        print(f"❌ 初始化数据失败: {e}")
        return

    user_status = pm.get_user_status(current_price, current_rate)

    aud_cash = pm.profile['current_assets'].get('aud_cash', 0.0)

    # --- 2. 准备数据报告 ---
    print("📊 正在生成分项市场报告...")
    # 这里需要传 2y 数据给分析函数
    fx_report = analyze_multi_timeframe(get_history_data("AUDCNY=X", "2y"), "CURRENCY RATE (AUD/CNY)")
    stock_report = analyze_multi_timeframe(get_history_data("NDQ.AX", "2y"), "TARGET ASSET (NDQ.AX)")

    # --- 3. 运行 Agent 1: 外汇专家 ---
    print("\n🤖 [Agent 1] 外汇专家正在分析汇率...")
    fx_analysis = "⚠️ **Analysis Failed**: Forex agent encountered an error."
    try:
        agent_fx = create_agent(PROMPT_FOREX_AGENT)
        if agent_fx:
            fx_query = f"""
# market data: 
{fx_report}

Please analyze the trend of AUD/CNY exchange rate and provide exchange recommendations.
"""
            fx_analysis = agent_fx.run(fx_query)
            print(f"外汇专家观点:\n{fx_analysis[:150]}...")
    except Exception as e:
        print(f"❌ [Error] Agent 1 failed: {e}")
        fx_analysis = f"⚠️ **Forex Analysis Unavailable**\n\nError details: {str(e)}"

    # --- 4. 运行 Agent 2: 股票交易员 ---
    print("\n🤖 [Agent 2] 股票交易员正在分析盘面...")
    stock_analysis = "⚠️ **Analysis Failed**: Stock agent encountered an error."
    try:
        agent_stock = create_agent(PROMPT_STOCK_AGENT)
        if agent_stock:
            stock_query = f"""
# market data: 
{stock_report}

Please analyze the trend of NDQ.AX and provide buying and selling recommendations.
"""
            stock_analysis = agent_stock.run(stock_query)
            print(f"✅ 股票交易员观点:\n{stock_analysis[:150]}...")
    except Exception as e:
        print(f"❌ [Error] Agent 2 failed: {e}")
        stock_analysis = f"⚠️ **Stock Analysis Unavailable**\n\nError details: {str(e)}"

    # --- 5. 运行 Agent 3: 首席投资顾问 ---
    print("\n🤖 [Agent 3] 首席顾问正在进行最终决策...")
    final_decision = "⚠️ **Decision Failed**: Chief Manager encountered an error."
    try:
        agent_manager = create_agent(PROMPT_MANAGER_AGENT)
        if agent_manager:
            final_prompt =  f"""

你是一名专业的私人投资顾问。你拥有以下信息：

1. **用户画像**：风险偏好【{user_status.risk_level}】，当前持有现金 ¥{user_status.cash_cny:.0f}，本期最大可投预算 ¥{user_status.disposable_for_invest:.0f}。
2. **外汇专家观点**：{fx_analysis}
3. **股票交易员观点**：{stock_analysis}
4. **市场分析报告**：{stock_report} {fx_report}


**你的任务**：

你比其他两位专家要更为专业、顾全大局。你可以参考他们的意见，但是不能被他们左右你的决定，并且基于市场数据（特别是RSI、均线偏离度、回撤）和用户风险偏好，计算本期具体的【建议投资金额】。

**注意：如果某位专家的观点显示为“Error”或“Unavailable”，请基于现有的市场数据报告（Market Data Reference）自行判断，并在风险提示中说明数据来源的不完整性。**


**决策逻辑参考**：

- 如果市场处于低位/超卖（RSI<30 或 价格<MA200）：建议加大投入，使用 80%-100% 的预算。

- 如果市场处于中位/震荡：建议定投，使用 40%-60% 的预算。

- 如果市场处于高位/超买（RSI>70 或 价格远超 MA200）：建议观望，投入 0% 或仅 10%。



请分两部分回答：

1. **市场形势分析**：简述判断理由（结合新闻和数据）。

2. **行动指南**：

- 给出明确的**投资金额 (CNY)**。

- 给出**换汇建议**（是否立即换汇）。

"""
            final_decision = agent_manager.run(final_prompt)
            print(final_decision)
    except Exception as e:
        print(f"❌ [Error] Agent 3 failed: {e}")
        final_decision = f"⚠️ **Final Decision Unavailable**\n\nSystem encountered a critical error during final synthesis.\nError: {str(e)}"

    # --- 6. 发送邮件通知 ---
    full_report = f"""
# 投资分析报告 / Invest Agent Report

## 1. 外汇专家分析 (Forex Expert - AUD/CNY)
{fx_analysis}

---

## 2. 股票交易员分析 (Stock Trader - NDQ.AX)
{stock_analysis}

---

## 3. 首席顾问最终决策 (Final Decision)
{final_decision}

---
*Market Data Reference:*
{fx_report}

{stock_report}
"""
    send_gmail_notification(full_report)


if __name__ == "__main__": main()