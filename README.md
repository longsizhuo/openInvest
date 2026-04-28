# 🤖 多智能体投资助手

本项目是一个多智能体系统，用于分析金融市场（外汇与股票），并通过邮件输出可执行的投资报告。

本助手不构成任何投资建议，且存在以下限制：
1. 大量上下文会影响 LLM 生成质量，转移注意力，因此只针对单只股票进行分析；
2. 现阶段的 `Prompt` 中写死了澳洲、纳指的逻辑，请根据需要修改；
3. 本系统没有考虑短期交易的卖出行为，只会建议当前是否入场、加仓；
4. 交易行为请自己决定，手动执行。

技术栈：**DeepSeek (LLM)**、**LangChain**、**DDGS**、**Yahoo Finance**。

## 🛠 手动安装（Python）
### 前置条件
*   Python 3.13+
*   `uv`（推荐）或 `pip`

### 1. 安装依赖
```bash
# 使用 uv（更快）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

**安装 uv（如未安装）：**

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows（PowerShell）
irm https://astral.sh/uv/install.ps1 | iex
```

### 2. 配置

**(a) 持仓 / 策略 (memory/) —— 多资产 schema**

旧的单文件 `user_profile.json` 已在 P4 重构里替换为 `memory/` 下的 markdown
+ frontmatter 文档（user.md / strategy.md / portfolio.md）。首次部署：

```bash
cp user_profile.example.json user_profile.json
# 编辑 user_profile.json：填邮箱、风险偏好、初始现金等
python scripts/migrate_profile.py
# 此脚本会把 JSON 内容拆成 memory/{user,strategy,portfolio}.md
```

`strategy.md` 的 `target_assets` 是**资产数组**，每个资产独立配 cap：

```yaml
target_assets:
  - symbol: NDQ.AX
    currency: AUD
    max_single_invest_cny: 10000
    ...
  - symbol: GC=F
    currency: CNY
    max_single_invest_cny: 5000
    ...
```

详细 memory schema 见 [`docs/memory_layout.md`](docs/memory_layout.md)。

**(b) `.env` —— 凭据 + 可调参数**

```bash
cp .env.example .env
# 编辑 .env，至少填：
#   DEEPSEEK_API_KEY=sk-...
#   DEEPSEEK_BASE_URL=https://api.deepseek.com   (可选)
#   EMAIL_SENDER=...@gmail.com
#   EMAIL_PASSWORD=<Gmail App Password 16 位>
```

可选生产化 env（默认值已经合理，按需调）：

| Env | 默认 | 作用 |
|---|---|---|
| `INVEST_LLM_MAX_ATTEMPTS` | 3 | LLM 调用最大尝试次数（含首次） |
| `INVEST_LLM_BASE_DELAY` | 2.0 | LLM 重试初始延迟 (秒)，指数退避 + jitter |
| `INVEST_LLM_MAX_DELAY` | 20.0 | LLM 重试单次延迟上限 (秒) |
| `INVEST_PRICE_STALE_DAYS` | 3 | 价格陈旧阈值（DB 最新日期距今超此值时给 LLM 加告警） |
| `INVEST_WHITELIST_QQ` | — | NapCat 命令白名单 QQ 号（多用户场景必填） |
| `DIGEST_EMAIL_TO` | — | 兜底收件人（user.md 的 email 字段缺失时用） |

**Gmail 凭据要求**：必须使用 [App Password](https://myaccount.google.com/apppasswords)
（需要先开 2FA），不能用账号登录密码。其他 SMTP 邮箱（Outlook/QQ/163）使用各
自的"授权码/应用密码"，并确认已开 SMTP 服务。未配置邮箱时整个 daily_report
仍会跑完并把结果写进 `memory/.committee/<date>/`，只是不发邮件。

### 3. 运行

| 模式 | 命令 |
|---|---|
| 跑一次 daily report (含 4 角色委员会、邮件、落盘) | `python -m jobs.daily_report` |
| 跑全套 cron (推荐生产) | `python -m scheduler.runner` |
| Claude Code 交互式委员会 | `~/.claude/skills/invest/run.sh prepare_committee NDQ.AX` |

旧的 `python main.py` 入口已经在 P4 简化为薄 wrapper，仍然能跑但不再推荐。
旧的 `python scheduler.py` 文件已删除，被 `scheduler.runner` (APScheduler + YAML
job 发现) 取代。

## 🐳 Docker 部署

```bash
cp .env.example .env  # 填好凭据
docker compose up -d
docker compose logs -f invest-agent
```

容器启动 `python -m scheduler.runner`（APScheduler 入口；旧的 `scheduler.py`
已在 P4 删除）。`docker-compose.yml` 已配 `restart: always` + `TZ=Asia/Shanghai`，
持久化挂载 `./db` 和 `./cache_data`。生产环境建议**额外挂载 `./memory`**，
确保 portfolio 状态在容器重建后不丢。

## 🛡️ 生产化与可靠性

针对 audit 暴露的几个硬伤已做的硬化：

- **原子写入**：`MemoryStore.write()` / `state_set()` / `write_dream_state()`
  全部走 `tmp + fsync + os.replace`，进程被 kill / OOM / 断电不会把
  `portfolio.md` 截成半截文件。仍配合 `fcntl.flock` 序列化并发写。
  *见 `core/memory_store.py:_atomic_write_text`。*

- **LLM 调用重试**：`core/committee._ask` 自带 3 次指数退避 + 抖动重试，区分
  transient (timeout/ratelimit/5xx) 和 permanent (auth/permission)，后者
  立即放弃。重试参数可通过 `INVEST_LLM_*` env 覆盖。
  *DeepSeek 偶发 429 / 超时不会再让 CIO 在 garbage 上面编 memo。*

- **邮件失败显式抛异常**：`services/notifier.send_gmail_notification` 重试 5
  次后仍失败抛 `EmailDeliveryError`，让 scheduler runner 自动记 `job_runs`
  表。`jobs/daily_report` catch 后把状态写进 return value 的 `email`
  字段并往 `.dreams/events.jsonl` 落 `email_delivery_failed` 审计事件 ——
  committee 落盘结果不会因邮件失败而被覆盖。

- **多资产估值不依赖列表顺序**：`jobs/daily_report` 显式按 `symbol == NDQ.AX`
  查找估值价格，未来重排 `target_assets` 不会把克价当股价导致总资产爆炸。

- **行情数据失败/陈旧不再幻觉**（4 件套）：
  1. `_get_last_close()` 返回 `(price, age_days)`，`price=None` 不再用 `0.0` 兜底，
     上层显式跳过该资产委员会；总资产估算也剔除被跳过的资产，避免"集中度爆表"假信号
  2. NDQ.AX 走 BetaShares 网页 scraper 容易被反爬 403 —— 现已 fallback 到
     `yfinance.Ticker("NDQ.AX")`，保证 close 价仍能拉到（仅丢 holdings/sectors）
  3. 价格陈旧 ≥ `INVEST_PRICE_STALE_DAYS` 天时往 portfolio_summary 注入"⚠️ 数据陈旧 N 天"
     段，让 LLM 看到后停止"在过期数据上面编今天的策略"
  4. 所有失败 / 陈旧事件落 `.dreams/events.jsonl`：`price_fetch_failed` /
     `price_stale` / `email_delivery_failed`，外部监控可基于此告警
  - daily_report return 新增 `status` 可能值 `degraded` 和 `skipped_assets` /
    `data_warnings` 字段，scheduler runner 能感知到"job 跑了但有数据问题"

剩余还在 backlog 的 audit 硬伤（按工作量排）：

- [ ] NapCat ↔ scheduler **TOCTOU 窗口**（read-modify-write 不在单一锁内）
- [ ] **测试 + CI**（仅有 1 个 `tests/test_commsec.py`，无 GitHub Actions）
- [ ] 单用户假设硬编码（`MemoryStore.path_of` 无 user_id 维度，做 SaaS 需要重构）

## 🧠 Claude Code Skill 集成（可选）

把 invest 当作 Claude Code 的本地 skill 用（`status` / `prepare_committee` /
`save_committee` 等子命令直接在 Claude 对话里调用）：

```bash
cd $INVEST_HOME       # 默认 ~/projects-review/invest
bash skill/install.sh
```

`install.sh` 会在 `~/.claude/skills/invest/` 建立两个 symlink 指向仓库里的
`skill/SKILL.md` 和 `skill/run.sh`。这样以后改 skill 协议（`prepare_committee`
返回字段、`save_committee` 路径布局等）只需要在仓库里改 → commit → 其他设备
`git pull` 立即生效，不用手动同步。

详见 [`skill/README.md`](skill/README.md)。
