# Jobs Index

仿 OpenClaw 的 cron 任务索引。每个任务一份 YAML 配置 + 一个 Python entry。

## 调度时间总览（Asia/Shanghai）

| Job | 频率 | Cron | Entry |
|-----|------|------|-------|
| `daily_report` | 每天 10:00 | `0 10 * * *` | `jobs.daily_report:run` |
| `commsec_sync` | 每 2 小时 | `0 */2 * * *` | `jobs.commsec_sync:run` |
| `payday_check` | 每月 1 号 09:00 | `0 9 1 * *` | `jobs.payday_check:run` |
| `weekly_review` | 周日 11:00 | `0 11 * * 0` | `jobs.weekly_review:run` |
| `dreaming` | 每天 03:00 | `0 3 * * *` | `jobs.dreaming:run` |

## 启动调度器

```bash
.venv/bin/python -m scheduler.runner          # 后台调度
.venv/bin/python -m scheduler.runner --once daily_report   # 一次性跑某个 job
.venv/bin/python -m scheduler.runner --list   # 列出所有 job
```

## 持久化

- `db/jobs.sqlite` — APScheduler jobstore（崩了重启状态保留）
- `db/jobs.sqlite` 内的 `job_runs` 表 — 每次执行的 run log（用于 weekly_review 复盘）
