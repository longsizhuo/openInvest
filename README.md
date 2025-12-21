# 🤖 Multiple Agent Invest Agent

An intelligent multi-agent system that analyzes financial markets (Forex & Stocks) and delivers actionable investment reports via email.

本助手不构成任何投资建议，且存在以下限制：
1. 大量上下文会影响LLM生成质量，转移注意力，因此只针对单只股票进行分析；
2. 现阶段的`Prompt`中写死了澳洲、纳指的逻辑，请根据需要修改；
3. 本系统没有考虑短期交易的卖出行为，只会建议当前是否入场、加仓；
4. 交易行为请自己决定，手动执行

Powered by **DeepSeek (LLM)**, **LangChain**, **DDGS**, and **Yahoo Finance**.

## 🛠 Manual Installation (Python)
### Prerequisites
*   Python 3.13+
*   `uv` (Recommended) or `pip`

### 1. Install Dependencies
```bash
# Using uv (fast)
uv sync

# OR using pip
pip install -r requirements.txt
```

### 2. Configure
Set up your `.env` file as shown in the Docker section.

### 3. Run
*   **One-time run:**
    ```bash
    python main.py
    ```
*   **Start Scheduler:**
    ```bash
    python scheduler.py
    ```
