# scheduler/

APScheduler 守护进程入口。把 `jobs/` 下的 `.yml` 定义注册成 cron，所有 job 状态持久化到 `db/jobs.sqlite`，重启不丢任务。

## 内容

- `runner.py` — main entry：扫描 `jobs/*.yml` → 注册 BackgroundScheduler → 阻塞循环。支持 `--once <job_name>` 单跑某个 job。

## 启动方式

```bash
# Daemon 模式（生产）
python -m scheduler.runner

# 单跑一个 job（debug）
python -m scheduler.runner --once daily_report
```

## 与其他目录的关系

- 上游：`Dockerfile` `CMD ["python", "-m", "scheduler.runner"]`；systemd 也调用此入口
- 下游：动态加载 `jobs/*.py` 的 `run()` 函数；写 `db/jobs.sqlite` 持久化 job 状态
