# 🤖 AI Investment Agent (AI 投资助理)

An intelligent multi-agent system that analyzes financial markets (Forex & Stocks) and delivers actionable investment reports via email.

Powered by **DeepSeek (LLM)**, **LangChain**, and **Yahoo Finance**.

## ✨ Features

*   **Multi-Agent Architecture**:
    *   **Forex Expert**: Analyzes AUD/CNY trends using macro news (RBA, China PMI).
    *   **Stock Trader**: Analyzes Nasdaq 100 (via NDQ.AX) using technical indicators and US tech news.
    *   **Portfolio Manager**: Synthesizes conflicting views into a final decision with specific budget allocation.
*   **Deep Search**: Automatically translates keywords and searches for factual drivers (not just opinions).
*   **Email Reports**: Sends beautiful, HTML-formatted analysis reports to your inbox.
*   **Automated Scheduler**: Runs daily at 10:00 AM (Beijing Time).
*   **Robustness**: Auto-retry mechanisms and fault-tolerant execution.

## 🚀 Quick Start (Docker) - Recommended

The easiest way to run this tool is using Docker.

### Prerequisites
*   Docker & Docker Compose

### 1. Clone the repository
```bash
git clone <your-repo-url>
cd invest
```

### 2. Configure Environment
Copy the example config and edit it with your API keys:
```bash
cp .env.example .env
nano .env
```
*   `DEEPSEEK_API_KEY`: Your DeepSeek API Key.
*   `EMAIL_SENDER` & `EMAIL_PASSWORD`: Your Gmail address and App Password.

### 3. Run!
```bash
docker-compose up -d
```
That's it! The agent runs in the background and will email you every day at 10:00 AM.

To check logs:
```bash
docker-compose logs -f
```

---

## 🛠 Manual Installation (Python)

If you prefer running it directly on your machine.

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

## 📂 Project Structure

*   `agent.py`: Core LLM agent wrapper (LangChain).
*   `main.py`: Main execution flow and multi-agent orchestration.
*   `scheduler.py`: Robust daily scheduler.
*   `notifier.py`: HTML email generation and sending.
*   `exchange_fee.py`: Financial data fetching (Yahoo Finance).
*   `db/`: Vector database storage (Chroma).

## ⚠️ Disclaimer
This tool is for **educational and informational purposes only**. Do not use it as the sole basis for financial decisions. The AI can hallucinate or misinterpret data. Always do your own research.
