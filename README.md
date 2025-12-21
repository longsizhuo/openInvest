# 🤖 多智能体投资助手

本项目是一个多智能体系统，用于分析金融市场（外汇与股票），并通过邮件输出可执行的投资报告。

本助手不构成任何投资建议，且存在以下限制：
1. 大量上下文会影响 LLM 生成质量，转移注意力，因此只针对单只股票进行分析；
2. 现阶段的 `Prompt` 中写死了澳洲、纳指的逻辑，请根据需要修改；
3. 本系统没有考虑短期交易的卖出行为，只会建议当前是否入场、加仓；
4. 交易行为请自己决定，手动执行。

技术栈：**DeepSeek (LLM)**、**LangChain**、**DDGS**、**Yahoo Finance**。

## 🛠 手动安装（Python）
### 前置条件
*   Python 3.13+
*   `uv`（推荐）或 `pip`

### 1. 安装依赖
```bash
# 使用 uv（更快）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

**安装 uv（如未安装）：**
```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows（PowerShell）
irm https://astral.sh/uv/install.ps1 | iex
```

### 2. 配置
按照 Docker 章节说明创建 `.env` 文件。

运行前，请在 `user_profile.json` 中设置**目标股票/ETF 代码**：
1. 将 `user_profile_sample.txt` 复制为 `user_profile.json`，并删除所有注释（JSON 不允许注释）。
2. 修改 `investment_strategy.target_asset`（例如：`NDQ.AX`、`QQQ`、`AAPL`）。
3. 股票代码可以在以下网站查询：
   - Yahoo Finance（支持公司名或代码搜索）
   - ASX（用于 `.AX` 代码）
   - NASDAQ（美股上市公司）

**SMTP 邮箱配置（发送报告邮件）：**
1. 在 `.env` 中设置以下字段：
   - `EMAIL_SENDER=你的邮箱地址`
   - `EMAIL_PASSWORD=邮箱应用专用密码`
2. 如果使用 Gmail：
   - 需要开启两步验证（2FA）。
   - 在 Google 账号中创建“应用专用密码”，将生成的 16 位密码填入 `EMAIL_PASSWORD`。
3. 其他邮箱（如 Outlook/QQ/163）：
   - 请使用“SMTP 授权码/应用密码”，并确认已开启 SMTP 服务。
4. 未配置邮箱时会跳过发送（控制台会提示缺少凭据）。

### 3. 运行
*   **单次运行：**
    ```bash
    python main.py
    ```
*   **启动定时任务：**
    ```bash
    python scheduler.py
    ```
