# capabilities/

openInvest 的能力域。每个 capability 是一个自包含的领域包，包含 prompt 模板 + Python 实现 + 文档。

## 结构

```
capabilities/
├── committee/               ← 4 角色 AI 投资委员会辩论
│   ├── prompts/             ← 各角色的 SKILL.md prompt 模板
│   ├── cio.py               ← CIO 决策者（综合各方意见出 verdict）
│   ├── macro_strategist.py  ← 宏观分析师（跨资产共享）
│   ├── quant.py             ← 量化分析师（技术指标、信号强度）
│   ├── risk_officer.py      ← 风控官（持仓集中度、行为模式）
│   └── wealth_context_officer.py ← 财富背景官（流动性评估）
├── loader.py                ← prompt 模板加载器（load_skill + 占位符渲染）
├── tools.py                 ← agent 共享工具函数（市场数据查询等）
├── sdk_agent.py             ← Anthropic SDK agent 实现
├── dspy_few_shot_loader.py  ← DSPy few-shot 训练数据加载
└── README.md
```

## 与其他目录的关系

- 上游：被 `core/committee/debate.py:run_committee` 调用编排
- 下游：调用 `services/news.py` 拉新闻、`utils/exchange_fee.py` 拉行情
