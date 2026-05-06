# utils/

静态计算工具——拉数据、做技术指标、给 LLM 客观数据。**不写 memory，不做决策**，只算。

## 内容

- `quotes.py` — **v2 通用行情接入**：`get_quote(holding)` 根据 `proxy_kind` 选择数据源（direct yfinance / gold 反推 / fx_pair），返回统一 `QuoteSnapshot`
- `gold_price.py` — 黄金克价快照（GC=F + USDCNY=X 反推 CNY/克）+ DB 兜底
- `exchange_fee.py` — yfinance 包装 `get_history_data(symbol, period)`，含 requests-cache
- `betashares_scraper.py` — BetaShares 官网 NDQ.AX 净值爬虫（yfinance 挂时备用）
- `market_calendar.py` — 交易日历（exchange-calendars 包装）
- `market_metrics.py` — 技术指标（RSI / 价格分位 / 波动率）

## 与其他目录的关系

- 上游：被 `agents/` `core/committee.py` `jobs/` `connectors/` 调用
- 下游：原生 yfinance / `db/market_store.py` 兜底
- 测试：`tests/test_gold_price.py` 覆盖兜底链路
