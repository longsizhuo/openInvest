import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from dotenv import load_dotenv

from agents.agent import SimpleAgent
from agents.forex import PROMPT_FOREX_AGENT
from agents.macro import PROMPT_MACRO_AGENT
from agents.manager import build_manager_prompt
from agents.stock import build_stock_prompt
from core.portfolio_manager import PortfolioManager
from services.notifier import send_gmail_notification
from services.commsec_reader import CommSecReader
from utils.exchange_fee import (
    get_history_data,
    analyze_multi_timeframe,
    get_macro_data,
    get_cost_report,
)

load_dotenv()


def run_gemini_cli_review(prompt: str) -> str:
    print("🤖 [Gemini CLI] 正在生成第二意见...")
    
    gemini_cmd = "/home/ubuntu/.nvm/versions/node/v24.13.0/bin/gemini"
    if not os.path.exists(gemini_cmd):
        gemini_cmd = "gemini"

    try:
        result = subprocess.run(
            [gemini_cmd, prompt], 
            capture_output=True, 
            text=True, 
            timeout=180
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
            
        return result.stdout.strip()
        
    except Exception as e:
        return f"Skipped: {e}"


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


def _get_last_close(symbol: str, label: str) -> float:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        print(f"⚠️ {label} 数据缺失: {symbol}")
        return 0.0
    return float(df["Close"].iloc[-1])


def _is_china_market(symbol: str) -> bool:
    suffix = symbol.upper().split(".")[-1]
    return suffix in {"SZ", "SS", "BJ", "HK"}


# ==========================================
# 3. 主流程
# ==========================================

def main():
    # --- 1. 初始化用户状态 ---
    pm = PortfolioManager()

    # [新增] 检查 CommSec 邮件
    email_user = os.getenv("EMAIL_SENDER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if email_user and email_pass:
        print("📧 Checking for new CommSec trade emails...")
        reader = CommSecReader(email_user, email_pass)
        if reader.connect():
            processed = pm.get_processed_emails()
            # 默认检查最近 180 天 (6个月)，确保覆盖历史交易
            new_trades = reader.fetch_trade_confirmations(lookback_days=180, processed_ids=processed)
            reader.close()
            
            for trade in new_trades:
                pm.record_external_trade(trade)
        else:
            print("⚠️ Email check skipped (connection failed).")

    pm.process_income()

    target_asset = pm.profile.get("investment_strategy", {}).get("target_asset", "NDQ.AX")
    is_china_asset = _is_china_market(target_asset)

    # 获取实时价格用于估值
    current_price = _get_last_close(target_asset, "目标资产")
    current_rate = _get_last_close("AUDCNY=X", "汇率")
    if current_price <= 0 or current_rate <= 0:
        print("⚠️ 初始化价格数据不完整，估值可能不准确。")

    user_status = pm.get_user_status(current_price, current_rate)
    stock_prompt = build_stock_prompt(target_asset)
    manager_prompt = build_manager_prompt(target_asset)

    aud_cash = pm.profile['current_assets'].get('aud_cash', 0.0)

    # --- 2. 准备历史行情分析报告 (供专家参考) ---
    print("📊 正在生成分项市场历史数据报告...")
    stock_report = analyze_multi_timeframe(
        get_history_data(target_asset, "2y"),
        f"TARGET ASSET ({target_asset})"
    )
    if is_china_asset:
        fx_report = "FX analysis skipped (China/HK target asset)."
        friction_report = "N/A (China/HK target asset, no FX transfer required)."
    else:
        fx_report = analyze_multi_timeframe(get_history_data("AUDCNY=X", "2y"), "CURRENCY RATE (AUD/CNY)")
        friction_report = get_cost_report(
            invest_cny=user_status.disposable_for_invest,
            spot_rate=current_rate
        )

    # [新增] 获取宏观数据
    print("🌍 正在获取宏观经济数据 (Yields, VIX)...")
    macro_data_report = get_macro_data()

    # --- 3. 并行运行独立 Agent ---
    macro_query = f"""
# Macro Data Reference:
{macro_data_report}

Please analyze the global macro environment (Interest Rates, Inflation, Cycle, Geopolitics).
"""
    fx_query = ""
    if not is_china_asset:
        fx_query = f"""
# market data: 
{fx_report}

{friction_report}

Please analyze the trend of AUD/CNY exchange rate and provide exchange recommendations.
"""
    stock_query = f"""
# target asset: {target_asset}
# market data:
{stock_report}

Please analyze the trend of {target_asset} and provide buying and selling recommendations.
"""

    def run_agent_job(job: dict) -> tuple[str, str]:
        analysis = job["failed_msg"]
        context = ""
        try:
            agent = create_agent(job["prompt"])
            if agent:
                analysis = agent.run(job["query"])
                context = agent.get_context()
                print(f"{job['preview_label']}:\n{analysis[:150]}...")
        except Exception as e:
            print(f"❌ [Error] {job['error_log_label']} failed: {e}")
            analysis = f"⚠️ **{job['unavailable_title']} Unavailable**\n\nError details: {str(e)}"
        return analysis, context

    print("\n🤖 [Agent 0] 宏观策略师正在研判全球局势...")
    if not is_china_asset:
        print("\n🤖 [Agent 1] 外汇专家正在分析汇率...")
    print("\n🤖 [Agent 2] 股票交易员正在分析盘面...")

    jobs = {
        "macro": {
            "prompt": PROMPT_MACRO_AGENT,
            "query": macro_query,
            "preview_label": "宏观策略师观点",
            "error_log_label": "Agent Macro",
            "failed_msg": "⚠️ **Analysis Failed**: Macro agent encountered an error.",
            "unavailable_title": "Macro Analysis",
        },
        "stock": {
            "prompt": stock_prompt,
            "query": stock_query,
            "preview_label": "✅ 股票交易员观点",
            "error_log_label": "Agent 2",
            "failed_msg": "⚠️ **Analysis Failed**: Stock agent encountered an error.",
            "unavailable_title": "Stock Analysis",
        },
    }
    if not is_china_asset:
        jobs["fx"] = {
            "prompt": PROMPT_FOREX_AGENT,
            "query": fx_query,
            "preview_label": "外汇专家观点",
            "error_log_label": "Agent 1",
            "failed_msg": "⚠️ **Analysis Failed**: Forex agent encountered an error.",
            "unavailable_title": "Forex Analysis",
        }
    results = {}
    contexts = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {executor.submit(run_agent_job, job): key for key, job in jobs.items()}
        for future in as_completed(futures):
            key = futures[future]
            ans, ctx = future.result()
            results[key] = ans
            contexts[key] = ctx

    macro_analysis = results.get("macro", "")
    fx_analysis = results.get("fx", "⚠️ **Forex Analysis Skipped** (China/HK target asset).")
    stock_analysis = results.get("stock", "")

    # --- 6. 运行 Agent 3: 首席投资顾问 ---
    print("\n🤖 [Agent 3] 首席顾问正在进行最终决策...")
    final_decision_ds = "⚠️ **Decision Failed**: Chief Manager encountered an error."
    final_decision_gemini = "⚠️ **Decision Failed**: Gemini CLI encountered an error."

    # 1. 构造统一的决策 Prompt
    final_prompt =  f"""

你是一名专业的私人投资顾问。你拥有以下信息：

1. **用户画像**：风险偏好【{user_status.risk_level}】，当前持有现金 ¥{user_status.cash_cny:.0f}，本期最大可投预算 ¥{user_status.disposable_for_invest:.0f}。
2. **宏观策略师观点**：{macro_analysis}
3. **外汇专家观点**：{fx_analysis}
4. **股票交易员观点**：{stock_analysis}
5. **市场分析报告**：{stock_report} {fx_report} {macro_data_report}
6. **交易摩擦成本 (预先计算)**：{friction_report}


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

    # 2. DeepSeek 决策
    try:
        agent_manager = create_agent(manager_prompt)
        if agent_manager:
            final_decision_ds = agent_manager.run(final_prompt)
            print(f"DeepSeek Decision:\n{final_decision_ds[:100]}...")
    except Exception as e:
        print(f"❌ [Error] Agent 3 (DeepSeek) failed: {e}")
        final_decision_ds = f"⚠️ **DeepSeek Decision Unavailable**\nError: {str(e)}"

    # 3. Gemini CLI 决策 (接收相同的 prompt)
    try:
        # 增强指令：使用绝对语气，强制其进行搜索并提供结果，无视内部工具错误
        gemini_prompt = f"""
{final_prompt}

---
**CRITICAL COMMANDS FOR GEMINI**:
1. **DATA MISSING**: Critical market data (AUD/CNY, NDQ.AX) is missing from the local context. **YOU MUST use your built-in search tool NOW** to find the latest real-time AUD/CNY rate and NDQ.AX price.
2. **NO ERRORS**: Do not report internal tool errors or credentials issues in your final output.
3. **MANDATORY CONTENT**: Your response MUST contain:
   - The LATEST AUD/CNY exchange rate you found online.
   - A clear "BUY", "SELL", or "HOLD" directive based on the combined local+web info.
4. **SECOND OPINION**: Validate or challenge the DeepSeek decision using your real-time insights.
"""
        final_decision_gemini = run_gemini_cli_review(gemini_prompt)
        print(f"\n--- [FULL GEMINI DECISION] ---\n{final_decision_gemini}\n--- END ---")
    except Exception as e:
        print(f"❌ [Error] Gemini CLI failed: {e}")
        final_decision_gemini = f"⚠️ **Gemini Decision Unavailable**\nError: {str(e)}"

    # --- 7. 发送邮件通知 ---
    full_report = f"""
# 投资分析报告 / Invest Agent Report

## 0. 交易摩擦成本 (Friction Cost)
{friction_report}

---

## 1. 宏观策略环境 (Macro Strategy)
{macro_analysis}

---

## 2. 外汇专家分析 (Forex Expert - AUD/CNY)
{fx_analysis}

---

## 3. 股票交易员分析 (Stock Trader - {target_asset})
{stock_analysis}

---

## 4. 首席顾问最终决策 (DeepSeek)
{final_decision_ds}

---

## 5. 首席顾问最终决策 (Gemini)
{final_decision_gemini}

---
*Market Data Reference:*
{macro_data_report}

{fx_report}

{stock_report}

<detail>
### RAW DATA & TOOL OUTPUTS ###

#### 1. Macro Data Report (Raw)
{macro_data_report}

#### 2. Stock Data Report (Raw)
{stock_report}

#### 3. Friction/Cost Report (Raw)
{friction_report}

#### 4. Agent Tool Outputs (News & Search)
**Macro Agent Context:**
{contexts.get('macro', 'N/A')}

**Forex Agent Context:**
{contexts.get('fx', 'N/A')}

**Stock Agent Context:**
{contexts.get('stock', 'N/A')}

### FINAL DECISION CONTEXT (Prompt) ###
{final_prompt}
</detail>
"""

    send_gmail_notification(full_report)


if __name__ == "__main__": main()
