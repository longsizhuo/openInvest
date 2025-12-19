import os
import json
import re
from typing import Optional, Dict
from dotenv import load_dotenv
from agent import SimpleAgent
from exchange_fee import get_history_data, analyze_multi_timeframe
from portfolio_manager import PortfolioManager

load_dotenv()

PROMPT_FOREX_AGENT = """
你是一名【外汇交易专家】。你的任务是专注于分析 AUD/CNY 汇率走势。
你只能看到汇率相关的数据。

**核心约束：T+7 规则**
人民币兑换澳元需要 7 个自然日才能到账。
这意味着你今天的建议是为了**下周**（7天后）的资金储备服务。
你**不需要**考虑股票市场的情况，只关注什么时候换汇最划算。

**工具使用策略**
1. **搜索新闻时，必须将关键词翻译成英文**（例如搜索 "AUD CNY forecast" 而不是 "澳元走势"），因为英文搜索结果更丰富且准确。
2. 优先关注 RBA（澳洲联储）政策、中国经济数据、大宗商品价格等宏观因素。

请分析：
1. 当前汇率是高还是低？（参考历史分位）
2. 未来一周的走势预期（结合新闻和技术面）？
3. 结论：现在是否应该用人民币换澳元？

请输出简短、犀利的分析，并明确给出“建议换汇”或“建议等待”的倾向。
**最后，请列出你参考的 1-2 条关键新闻标题。**
"""

PROMPT_STOCK_AGENT = """
你是一名【美股/澳股交易员】。你的任务是专注于分析 NDQ.AX (纳斯达克100澳洲ETF) 的走势。
你只能看到股票相关的数据。

**核心背景**
用户账户里已经有一些澳元（AUD）现金，可以随时买入。
你**不需要**关心汇率，也不需要关心资金来源，只关注现在的股价是否值得买入。

**工具使用策略**
1. **搜索新闻时，必须将关键词翻译成英文**（例如搜索 "NASDAQ 100 outlook" 或 "NDQ.AX analysis"），因为英文搜索结果更丰富且准确。
2. 关注纳斯达克指数的宏观情绪和澳洲市场的特定动态。

请分析：
1. 当前股价位置（历史高位/低位）？
2. 技术指标（RSI, 均线）发出了什么信号？
3. 结论：现在是否应该买入？

请输出简短、犀利的分析，并明确给出“建议买入”、“建议持有”或“建议卖出”的倾向。
**最后，请列出你参考的 1-2 条关键新闻标题。**
"""
PROMPT_MANAGER_AGENT = """
你是一名【首席投资顾问 (Portfolio Manager)】。
你拥有上帝视角，负责综合多方信息，做出最终的资产配置决策。

**决策逻辑**
1. **换汇决策 (CNY -> AUD)**：参考外汇专家的意见。如果专家说换，且用户CNY充足，则建议换汇。注意T+7延迟。
2. **交易决策 (AUD -> NDQ.AX)**：参考股票交易员的意见。如果专家说买，且用户AUD充足，则建议买入。

**输出要求**
1. 简要总结两位专家的核心冲突或共识。
2. 结合用户资金，给出具体操作建议。
3. 在回复的**最后一行**，必须严格输出 JSON 代码块，包含两个独立决策。
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


def extract_json_block(text: str) -> Optional[Dict]:
    """辅助函数：从 LLM 回复中提取 JSON"""
    try:
        # 尝试找 ```json ... ```
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        # 尝试找纯 {}
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except Exception as e:
        print(f"⚠️ JSON 解析失败: {e}")
    return None


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
    agent_fx = create_agent(PROMPT_FOREX_AGENT)
    if not agent_fx: return

    fx_query = f"""
【市场数据】
{fx_report}

请分析 AUD/CNY 汇率走势，并给出换汇建议。
"""
    fx_analysis = agent_fx.run(fx_query)
    print(f"外汇专家观点:\n{fx_analysis[:150]}...")  # 只打印前150字，保持清爽

        # --- 4. 运行 Agent 2: 股票交易员 ---
    print("\n🤖 [Agent 2] 股票交易员正在分析盘面...")
    agent_stock = create_agent(PROMPT_STOCK_AGENT)

    stock_query = f"""
【市场数据】
{stock_report}

请分析 NDQ.AX 走势，并给出买卖建议。
"""
    stock_analysis = agent_stock.run(stock_query)
    print(f"✅ 股票交易员观点:\n{stock_analysis[:150]}...")

    # --- 5. 运行 Agent 3: 首席投资顾问 ---
    print("\n🤖 [Agent 3] 首席顾问正在进行最终决策...")
    agent_manager = create_agent(PROMPT_MANAGER_AGENT)

    final_prompt =  f"""

你是一名专业的私人投资顾问。你拥有以下信息：

1. **用户画像**：风险偏好【{user_status.risk_level}】，当前持有现金 ¥{user_status.cash_cny:.0f}，本期最大可投预算 ¥{user_status.disposable_for_invest:.0f}。
2. **外汇专家观点**：{fx_analysis}
3. **股票交易员观点**：{stock_analysis}
4. **市场分析报告**：{stock_report} {fx_report}


**你的任务**：

你比其他两位专家要更为专业、顾全大局。你可以参考他们的意见，但是不能被他们左右你的决定，并且基于市场数据（特别是RSI、均线偏离度、回撤）和用户风险偏好，计算本期具体的【建议投资金额】。




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

    # --- 6. 闭环操作 (可选) ---
    # 尝试自动提取 JSON 并更新账本
    decision_json = extract_json_block(final_decision)
    if decision_json:
        print("\n" + "-" * 30)
        print("🛠️ 系统自动提取指令:")
        print(json.dumps(decision_json, indent=2, ensure_ascii=False))

        # 这里可以加代码：如果 forex_decision.action == "CONVERT_NOW"，自动扣 CNY 加 AUD
        # if decision_json['forex_decision']['action'] == 'CONVERT_NOW': ...
    else:
        print("\n⚠️ 未能自动提取 JSON 指令，请手动查阅报告。")


if __name__ == "__main__": main()