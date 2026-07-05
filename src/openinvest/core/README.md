# core/

业务核心。所有持久化、并发安全、agent 编排、数据校验都在这里。`connectors/` 与 `scripts/` 都是它的 wrapper。

## 内容

- `memory_store.py` — frontmatter + atomic write + fcntl 文件锁 + transaction 闭包（commit-on-success）
- `portfolio_manager.py` — 持仓门面（`with_portfolio_tx` 单锁 RMW、`record_external_trade`、`add_income`）
- `schemas.py` — Pydantic 数据层 schema（v2: cash dict + holdings list；写入前强校验）
- `committee.py` — Coordinator-Worker 投资委员会编排（macro → quant + risk → cio，三轮辩论）
- `consolidation_lock.py` — Dreaming 子系统的跨进程独占锁
- `regime.py` — 市场 regime 判定（牛市/熊市/震荡），喂给 quant agent 做硬约束
- `benchmarks.py` — 8 个基准定义（SPY/QQQ/...）用于 PnL 对比

## 与其他目录的关系

- 上游：所有 connectors / scripts / jobs / agents 都进 core
- 下游：调用 `utils/` 拉行情、`services/` 拉新闻、`db/` 缓存历史
- 数据流：core 写 `memory/` markdown，cron 定时跑 → 委员会决策 → 写回 memory
