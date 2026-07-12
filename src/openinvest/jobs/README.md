# jobs/

APScheduler 自动发现的定时任务。每个 `.py` 配套一个 `.yml` 描述 cron 触发器。`scheduler/runner.py` 启动时扫描本目录注册所有 job。

## 内容

| Job | 频率 | 职责 |
|-----|------|------|
| `daily_report.py` | ~~每天 10am~~ **默认停用**（2026-07-12）| 跑完整投资委员会，生成 markdown brief，发邮件——日报改由宿主 agent 侧 cron 经 MCP 触发，教程见 `docs/wiki/20-agent-usage-tutorial.md` §5 |
| `commsec_sync.py` | 每 2h | 拉 CommSec 成交回执邮件，更新 portfolio.holdings |
| `dreaming.py` | 每天 3am | 三阶段记忆整合（Light → REM → Deep Sleep），insights/ 沉淀长期模式 |
| `pnl_snapshot.py` | 工作日每 2h | 算 PnL 写 jsonl 历史，渲染 `docs/pnl_chart.svg` |
| `verdict_review.py` | 每天 2am（`enabled: false`，Phase 3 待开） | 委员会决策命中率刷进 verdict_review.jsonl（dreaming 上游训练源） |
| `event_watch.py` | 北京 8:00-次日 2:30 每 30min（config `event.watch_schedule` 可改）| 扫多源新闻 → LLM 归一化 → 命中持仓则邮件 + 触发委员会（ADR-006）|
| `price_sentinel.py` | 同窗口每 5min（config `event.sentinel_schedule` 可改）| 价格垂直线检测（10min vs 日ATR%，零 LLM）→ **先报警邮件后触发委员会**（ADR-025）|

> `verdict_review.yml`（cron `0 2 * * *`，`enabled: false`）：把 `.committee` live 委员会快照刷进 `.dreams/verdict_review.jsonl`，是 `dreaming` 的上游训练源。Phase 3 自学习开火时由用户确认频率后，与 `dreaming.yml` 一并改 `enabled: true`。

- `INDEX.md` — 所有 job 的输入/输出 spec（人类参考）
- `*.yml` — APScheduler cron 配置（声明式）

## 与其他目录的关系

- 上游：被 `scheduler/runner.py` 注册；也可单跑 `python -m jobs.daily_report`
- 下游：调用 `core/committee.py`、`agents/*`，写 `memory/` 和 `docs/`
- **生产关键路径**：cron 写错会污染真实持仓数据，所有写都走 `with_portfolio_tx`
