# scripts/

CLI 脚本入口 + 一次性维护工具。**不在生产 cron 里跑**（生产 cron 走 `jobs/`）。

## 内容

| 脚本 | 用途 | 频率 |
|------|------|------|
| `skill.py` | Claude Skill 入口（`run.sh` 调它）— 给外部 agent 暴露 status/strategy/history/what_if/prepare_committee 命令 | 按需 |
| `migrate_portfolio_to_holdings.py` | v1 → v2 portfolio.md 一次性迁移（cash_cny 等扁平字段 → cash dict + holdings list） | 一次 |
| `migrate_profile.py` | 旧 user_profile.json → memory/user.md (历史一次性) | 一次 |
| `init_market_db.py` | 首次启动时初始化 `db/market_data.db` schema | 一次 |
| `import_gold_trades.py` | 从用户截图记录的浙商交易批量导入 history.jsonl | 按需 |
| `backfill_pnl_history.py` | 重跑历史 PnL 快照（pnl_history.jsonl 损坏时） | 按需 |
| `clean_pnl_history.py` `check_benchmark_freshness.py` | PnL 数据维护工具 | 按需 |
| `backtest_committee.py` | 历史窗口内回测委员会决策（offline） | 研究用 |
| `diagnose.py` | 健康检查（memory / .env / DB / yfinance 全链路 ping） | 排错用 |
| `test_gemini_cli.py` | Gemini CLI 集成 smoke test | 偶尔 |

## 与其他目录的关系

- 上游：`~/.claude/skills/invest/run.sh` 调用 `scripts/skill.py`；用户手动跑迁移
- 下游：调用 `core/` 业务层、`db/` 行情库
