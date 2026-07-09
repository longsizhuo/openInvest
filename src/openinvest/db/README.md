# db/

SQLite 持久化（行情缓存 + APScheduler job 状态 + Chroma 向量库）。**所有 .sqlite 文件都 git ignored**，仅 `market_store.py` 入库。

## 内容

- `market_store.py` — 行情 DB 抽象（`get_latest_price`、`store_snapshot`），yfinance 失败时的兜底数据源
- `market_data.db` — yfinance 历史价格 + 实时快照缓存（gitignore）
- `jobs.sqlite` — APScheduler 持久化 job 状态，重启不丢任务（gitignore）
- `chroma.sqlite3` — Chroma 向量库（langchain-chroma 用）（gitignore）

## 与其他目录的关系

- 上游：`utils/gold_price.py` `utils/exchange_fee.py` 在 yfinance 挂时回落到这里读历史
- 下游：被 `jobs/pnl_snapshot.py` 写入；`scripts/archive/init_market_db.py` 初始化
- 数据完整性：DB schema 简单（symbol, ts, close），重建成本低
