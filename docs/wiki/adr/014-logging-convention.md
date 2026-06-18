---
type: adr
title: "ADR-014: 生产代码 logging 规范"
tags: [logging, convention, production, observability]
intent: 日志规范
documents:
  endpoints: []
  config_keys: []
  symbols: []
status: proposed
date: "2026-05-28"
supersedes: []
superseded_by: []
---

# ADR-014: 生产代码 logging 规范

**状态**: 提议中
**日期**: 2026-05-28
**作者**: Code Review

---

## 背景

系统是自循环的：`scheduler/runner.py` 以 APScheduler `BackgroundScheduler` 驱动所有 job，
运行在 Docker 容器中，无人交互。

**当前问题：**

1. **print → 黑洞**：16 个生产文件中有 51 处 `print()`，输出到 stdout。
   Docker 默认不持久化 stdout，scheduler 也不 capture stdout。这些诊断信息
   实际上从未被任何人看到过。

2. **log → stderr only**：27 个模块声明了 `log = logging.getLogger(__name__)`，
   但 `logging.basicConfig()` 只配了 StreamHandler(stderr)。Docker 能通过
   `docker logs` 临时查看 stderr，但容器重启后丢失。

3. **无 FileHandler**：`docker-compose.yml` 有 `# - ./logs:/app/logs` 注释行
   但从未启用。没有持久化日志文件。

4. **风格不一致**：`core/committee_runner.py` 全用 `log.warning()`（15 处），
   而同层的 `jobs/daily_report.py` 全用 `print()`（12 处）。无统一规范。

**影响：**
- Sanity check 4 强制 HOLD（print）→ 无记录，无法复盘
- 价格拉取失败（print）→ 无记录，无法排查数据问题
- backup_cny 提取失败（刚加的 print）→ 无记录

## 决策

### 1. 禁止生产代码使用 `print()` 做诊断

`print()` 仅允许两种场景：
- **CLI 脚本的最终输出**：`scripts/skill_cmds/*` 的 `cmd_*()` 函数（原 `scripts/skill.py`，重构后实现拆到 `skill_cmds/` 子包）直接给用户看的 JSON/text
- **`if __name__ == "__main__"` 的交互式入口**：如 `print(run())`

所有诊断、警告、错误信息必须用 `log.*()`.

### 2. 统一 logging 配置

在 `scheduler/runner.py` 的 `logging.basicConfig()` 中加入 `FileHandler`：

```python
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "invest.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
```

同时取消 `docker-compose.yml` 中 `./logs:/app/logs` 的注释。

### 3. 各 log level 使用规范

| Level | 场景 | 示例 |
|-------|------|------|
| `log.info()` | 正常流程里程碑 | "SOLVENCY=strong + TRIM → HOLD"、job 启动/成功 |
| `log.warning()` | 降级但可恢复 | 价格拉取失败用历史值、backup_cny 提取失败默认 0 |
| `log.error()` | 单资产/单功能失败 | 某 symbol committee 失败（被跳过）|
| `log.exception()` | 意外异常 + 需要 stacktrace | 邮件发送失败、数据库连接断开 |

### 4. 模块 logger 命名

```python
import logging
log = logging.getLogger(__name__)
```

沿用现有 27 个模块的惯例。每个模块顶部声明一次，全文件复用。

## 不做的事

- **不引入 structlog / loguru**：项目已有 27 个模块用 stdlib logging，迁移成本大、收益小
- **不改 experiments/ 目录**：实验脚本是交互式的，print 合理
- **不改 sandbox 脚本**：`run_sandbox_v*.py` 等评估脚本面向终端用户，print 可读性更好

## 迁移范围

需迁移的文件（print → log）：

| 文件 | print 数 | 优先级 |
|------|---------|--------|
| `jobs/daily_report.py` | 12 | P0 — 每日核心链路 |
| `core/committee.py`（现 `core/committee/` 包，print/log 分散到 6 子模块，总数不变）| 7 | P0 — 决策引擎 |
| `scripts/skill_cmds/*` | 7 | P1 — skill 入口（重构后从 `scripts/skill.py` 拆到 `skill_cmds/` 包：`analysis_cmds.py` 2 处 / `lifecycle_cmds.py` 余下；部分 print 是 CLI 输出，保留）|
| `jobs/verdict_review.py` | 5 | P1 — 复盘链路 |
| `services/news.py` | 6 | P1 — 新闻采集 |
| `services/commsec_reader.py` | 5 | P2 — CommSec 同步 |
| `jobs/dreaming.py` | 1 | P2 |
| `jobs/pnl_snapshot.py` | 2 | P2 |
| `jobs/payday_check.py` | 1 | P2 |
| `jobs/commsec_sync.py` | 1 | P2 |
| `jobs/weekly_review.py` | 1 | P2 |
| `scheduler/runner.py` | 3 | P2 — 已大部分用 log |

## 后续步骤

1. 本 ADR 审批通过
2. PR-1：`scheduler/runner.py` 加 FileHandler + `docker-compose.yml` 启用 logs volume
3. PR-2：P0 文件（daily_report + committee）print → log
4. PR-3：P1 + P2 文件

## 参考

- `core/committee_runner.py` — 已全量 log 化的参考实现
- [Python logging HOWTO](https://docs.python.org/3/howto/logging.html)
