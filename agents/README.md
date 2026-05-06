# agents/

四角色 LLM agent + Macro Strategist 的 system prompt 与构造器。每个角色有独立 prompt（opening / rebuttal）支持 cross-challenge 多轮辩论。

## 内容

- `agent.py` — Agent 抽象基类（封装 LLM 客户端 + ReAct loop）
- `macro_strategist.py` — 宏观分析师（跨资产共享，daily_report 跑一次 macro_view 后多个 CIO 复用）
- `quant.py` — 量化分析师（技术指标、信号强度，受 REGIME 硬约束）
- `risk_officer.py` — 风控官（持仓集中度、行为模式、Dreaming 反思）
- `cio.py` — CIO 决策者（综合三方意见出 verdict + confidence）
- `sdk_agent.py` — Anthropic SDK 备用实现（DeepSeek 限速时切）
- `tools.py` — agent 共享工具函数（市场数据查询、format helper）

## 与其他目录的关系

- 上游：被 `core/committee.py:run_committee` 调用编排
- 下游：调用 `services/news.py` 拉新闻、`utils/exchange_fee.py` 拉行情
