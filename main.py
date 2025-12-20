import os
from typing import Optional
from dotenv import load_dotenv
from agent import SimpleAgent
from exchange_fee import get_history_data, analyze_multi_timeframe, get_macro_data
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

PROMPT_MACRO_AGENT = """
You are a Global Macro Strategy Researcher. Your goal is to assess the overall investment environment.
You do NOT analyze specific stocks, but rather the "weather" of the global economy.

**Core Focus Areas (The "Missing 4 Factors"):**
1. **Interest Rates & Central Banks**: US Fed (Jerome Powell) and RBA decisions. Are yields (^TNX) rising (bad for tech) or falling?
2. **Inflation Expectations**: Latest US CPI/PCE data. Is inflation sticky? (Erodes real returns).
3. **Economic Cycle**: Recession fears? Soft landing? or AI-driven productivity boom?
4. **Geopolitical Risks**: Wars, trade sanctions, or supply chain disruptions (Middle East, Russia-Ukraine, US-China relations).

**Tool usage strategy**
- Search for: "US Fed rate decision", "US CPI inflation report", "Geopolitical tensions latest", "Global recession risk", "US 10 year treasury yield trend".
- Translate keywords to English for searching.

Please provide:
1. **Macro Sentiment Score**: Scale from -5 (Extreme Risk/Crash Imminent) to +5 (Goldilocks/Perfect Growth).
2. **Key Headwinds (Negatives)** & **Key Tailwinds (Positives)**.
3. **Conclusion**: Is the current environment "Risk-On" (safe to invest) or "Risk-Off" (defensive)?
"""

PROMPT_STOCK_AGENT = """
You are a trader in the US/Australian stock market. Your task is to focus on analyzing the trend of NDQ.AX (NASDAQ 100 Australian ETF).
You can only see data related to stocks.

**Core Background**
The user's account already has some Australian dollars (AUD) in cash that can be bought at any time.
You don't need to care about exchange rates or funding sources, just focus on whether the current stock price is worth buying.

**Tool usage strategy**
1. When searching for news, translate keywords into English. 
2. **CRITICAL SEARCH RULE**: Do NOT search for the ticker "NDQ.AX" or "NDQ" directly, as it limits results to local ETF news. 
   - **ALWAYS search for the underlying index**: Use keywords like **"Nasdaq 100", "US Tech Sector", "QQQ ETF", "Magnificent Seven stocks"**.
   - Search for *factual drivers*: "US tech earnings report", "AI sector trends".
3. Pay attention to the macro sentiment of the NASDAQ index and specific developments in the Australian market only if relevant to the ETF structure.

Please analyze:
1. Current stock price position (historical high/low)?
What signals do technical indicators (RSI, moving averages) send out?
Conclusion: Should we buy now?

Please provide a brief and sharp analysis, and clearly indicate the tendency of 'recommended buy', 'recommended hold', 'recommended sell'.
**Finally, please list 1-2 key news headlines that you have referenced. **
"""
PROMPT_MANAGER_AGENT = """
You are a Chief Investment Advisor (Portfolio Manager).
You have a God's perspective and are responsible for synthesizing multiple information to make the final asset allocation decision.

**Decision logic**
1. **Macro Environment (The "Weather")**: Refer to the Macro Strategist. If the Macro Score is negative (e.g., <-2), be very conservative even if stocks look cheap. High rates or War = CAUTION.
2. **Exchange Decision (CNY -> AUD)**: Refer to the Forex Expert.
3. **Trading Decision (AUD -> NDQ.AX)**: Refer to the Stock Trader.

**Output requirements**
1. Briefly summarize the core conflicts or consensus between the experts.
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

    # --- 2. 准备历史行情分析报告 (供专家参考) ---
    print("📊 正在生成分项市场历史数据报告...")
    fx_report = analyze_multi_timeframe(get_history_data("AUDCNY=X", "2y"), "CURRENCY RATE (AUD/CNY)")
    stock_report = analyze_multi_timeframe(get_history_data("NDQ.AX", "2y"), "TARGET ASSET (NDQ.AX)")

    # [新增] 获取宏观数据
    print("🌍 正在获取宏观经济数据 (Yields, VIX)...")
    macro_data_report = get_macro_data()

    # --- 3. [新增] 运行 Agent 0: 宏观策略师 ---
    print("\n🤖 [Agent 0] 宏观策略师正在研判全球局势...")
    macro_analysis = "⚠️ **Analysis Failed**: Macro agent encountered an error."
    try:
        agent_macro = create_agent(PROMPT_MACRO_AGENT)
        if agent_macro:
            macro_query = f"""
# Macro Data Reference:
{macro_data_report}

Please analyze the global macro environment (Interest Rates, Inflation, Cycle, Geopolitics).
"""
            macro_analysis = agent_macro.run(macro_query)
            print(f"宏观策略师观点:\n{macro_analysis[:150]}...")
    except Exception as e:
        print(f"❌ [Error] Agent Macro failed: {e}")
        macro_analysis = f"⚠️ **Macro Analysis Unavailable**\n\nError details: {str(e)}"

    # --- 4. 运行 Agent 1: 外汇专家 ---
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

    # --- 6. 运行 Agent 3: 首席投资顾问 ---
    print("\n🤖 [Agent 3] 首席顾问正在进行最终决策...")
    final_decision = "⚠️ **Decision Failed**: Chief Manager encountered an error."
    try:
        agent_manager = create_agent(PROMPT_MANAGER_AGENT)
        if agent_manager:
            final_prompt =  f"""

你是一名专业的私人投资顾问。你拥有以下信息：

1. **用户画像**：风险偏好【{user_status.risk_level}】，当前持有现金 ¥{user_status.cash_cny:.0f}，本期最大可投预算 ¥{user_status.disposable_for_invest:.0f}。
2. **宏观策略师观点**：{macro_analysis}
3. **外汇专家观点**：{fx_analysis}
4. **股票交易员观点**：{stock_analysis}
5. **市场分析报告**：{stock_report} {fx_report} {macro_data_report}


**你的任务**：

你比其他两位专家要更为专业、顾全大局。你可以参考他们的意见，但是不能被他们左右你的决定。

**核心：必须赋予宏观因素最高权重。** 如果宏观策略师提示高风险（如高通胀、战争、加息），即使技术指标 RSI 很低，也必须建议【观望】或【大幅减少投入】。不要在暴风雨来临时建议出海。


**决策逻辑参考**：

- **宏观环境极差 (Macro Score < 0)**: 无论技术面如何，强制降低仓位。建议观望或仅投入 10%-20%。
- **宏观环境一般/震荡**: 参考技术指标定投，投入 40%-60%。
- **宏观环境向好 (Macro Score > 0)**: 结合技术面，若低位可大胆投入 80%-100%。

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

    # --- 7. 发送邮件通知 ---
    full_report = f"""
# 投资分析报告 / Invest Agent Report

## 1. 宏观策略环境 (Macro Strategy)
{macro_analysis}

---

## 2. 外汇专家分析 (Forex Expert - AUD/CNY)
{fx_analysis}

---

## 3. 股票交易员分析 (Stock Trader - NDQ.AX)
{stock_analysis}

---

## 4. 首席顾问最终决策 (Final Decision)
{final_decision}

---
*Market Data Reference:*
{macro_data_report}

{fx_report}

{stock_report}
"""
    send_gmail_notification(full_report)


if __name__ == "__main__": main()