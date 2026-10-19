# Jobs Index

仿 OpenClaw 的 cron 任务索引。每个任务一份 YAML 配置 + 一个 Python entry。
**以 `jobs/*.yml` 为准**（`python -m openinvest.scheduler.runner --list` 列出的就是这 13 个）；
本表与 yml 不一致时改本表。

## 调度时间总览（Asia/Shanghai）

| Job | enabled | 频率 | Cron | Entry |
|-----|---------|------|------|-------|
| `backup` | **是** | 每天 03:30 | `30 3 * * *` | `jobs.backup:run` |
| `verdict_review` | **是** | 每天 02:00 | `0 2 * * *` | `jobs.verdict_review:run` |
| `event_watch` | **是** | 北京 0-2、8-23 点每 30 分钟 | `*/30 0-2,8-23 * * *`（config `event.watch_schedule` 可覆盖） | `jobs.event_watch:run` |
| `price_sentinel` | **是** | 同窗口每 5 分钟 | `*/5 0-2,8-23 * * *`（config `event.sentinel_schedule` 可覆盖） | `jobs.price_sentinel:run` |
| `job_watchdog` | **是** | 每小时第 7 分钟 | `7 * * * *` | `jobs.job_watchdog:run` |
| `fund_confirm` | **是**（没有在途单时几毫秒返回，不联网） | 工作日 21:00-23:30 每 30 分钟 | `0,30 21-23 * * mon-fri` | `jobs.fund_confirm:run` |
| `pnl_snapshot` | **是** | 工作日 9-23 点每 2h | `0 9-23/2 * * mon-fri` | `jobs.pnl_snapshot:run` |
| `dca_daily` | **是**（真正开关是 config `dca.auto_dca_enabled`，默认 no-op） | 工作日 15:30 / 18:30 / 21:30 | `30 15,18,21 * * mon-fri` | `jobs.dca_daily:run` |
| `paper_autopilot` | **是**（真正开关是 config `autopilot.enabled`，默认 no-op；只给独立模拟 INVEST_HOME 用） | 工作日 16:35 | `35 16 * * mon-fri` | `jobs.paper_autopilot:run` |
| `ibkr_autopilot` | 否（不进 scheduler：独立盈透 INVEST_HOME 的系统 cron 跑 `openinvest ibkr_autopilot daily` / `reconcile`；开关 config `ibkr.enabled`，**下真单**） | 工作日 UTC 12:45 / 21:30 | — | `jobs.ibkr_autopilot:pick` / `:decide` / `:submit` / `:reconcile` |
| `path_review` | **是** | 周日 11:30 | `30 11 * * sun` | `jobs.path_review:run` |
| `daily_report` | 否（2026-07-12 停用，改由宿主 agent 侧 cron 经 MCP 触发） | 每天 10:00 | `0 10 * * *` | `jobs.daily_report:run` |
| `dreaming` | 否（P3 完成后再启用） | 每天 03:00 | `0 3 * * *` | `jobs.dreaming:run` |
| `commsec_sync` | 否（改手动导入，避免与真实成交对不上） | 每 2 小时 | `0 */2 * * *` | `jobs.commsec_sync:run` |

## 启动调度器

```bash
.venv/bin/python -m openinvest.scheduler.runner          # 后台调度
.venv/bin/python -m openinvest.scheduler.runner --once daily_report   # 一次性跑某个 job
.venv/bin/python -m openinvest.scheduler.runner --list   # 列出所有 job
```

## 持久化

- `db/jobs.sqlite` — 现已不再存 APScheduler jobstore（YAML 是唯一事实来源，启动时全量重建），只剩下面的 run log
- `db/jobs.sqlite` 内的 `job_runs` 表 — 每次执行的 run log（开跑即写 `running` 行，收尾 UPDATE；job_watchdog 巡检用）
- `.backups/auto_<UTC时间戳>.zip` — `backup` job 的产物（memory/ + trades.db / insights.db / jobs.sqlite，留最近 14 份；平日不含 `memory/.backtest*`，周日带上并加 `_full` 后缀）
