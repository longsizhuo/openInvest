# AGENTS.md

项目完整指引见 [CLAUDE.md](CLAUDE.md)（分层契约 / 测试纪律 / 发版规则）。
本文件只放跨 agent 通用的**可复用操作路径**——Hermes / Codex / OpenClaw 等会自动读本文件。

## Smoke test：宿主 agent 侧日报链路（Hermes，2026-07-12 验证通过）

验证 `openinvest daily_report`（stdout = 与邮件正文同源的日报 markdown，不发邮件）
能被宿主 agent 的 cron 原样投递。服务器侧 `daily_report` cron 已默认停用
（`jobs/daily_report.yml` `enabled: false`），这条链路是它的替代。

本机（dev box）已装 Hermes：`~/.hermes/`，gateway 跑在 user systemd service，
CLI 在 `~/.local/bin/hermes`。

```bash
export PATH="$HOME/.local/bin:$PATH"

# 1) 任务脚本（已存在）：~/.hermes/scripts/daily_invest.sh
#    本机跑 repo 本地代码：INVEST_HOME=/home/ubuntu/projects-review/invest + uv run
#    普通用户版本：INVEST_HOME=~/openInvest exec uvx openinvest daily_report

# 2) cron 任务（已存在：daily-invest-report，UTC 02:00 工作日 = 北京 10:00）
hermes cron list

# 3) 手动触发一次并验收
hermes cron run daily-invest-report
ls -t ~/.hermes/cron/output/*/ | head -2   # 取最新 .md
```

验收标准：

- 最新输出 `.md` 正文以 `# 投资委员会日报 (YYYY-MM-DD)` 开头，含"风险提示与免责声明"节
- 全程无邮件发送日志（`run(send_email=False)` 契约，单测见 `tests/test_cli_daily_report.py`）
- 重跑会重新烧一轮 DeepSeek token（无当日缓存）——测试别连续触发

改动 `jobs/daily_report.py` / `skill_cmds/committee_cmds.py` / `cli.py` 的日报相关
代码后，先跑单测再跑这条链路：

```bash
uv run pytest tests/test_cli_daily_report.py tests/test_daily_report_abort.py tests/test_daily_report_exit.py -q
```
