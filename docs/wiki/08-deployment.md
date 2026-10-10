---
type: wiki-chapter
title: 生产部署
tags: [deployment, docker, systemd, caddy, cloudflare]
intent: 部署
documents:
  endpoints:
    - GET /api/health
  config_keys: []
  symbols: []
---

# 生产部署

> ⚠️ **2026-07-05 更新**：Web GUI 已退役——后端不再 serve 静态文件，没有网页面板；
> `sync_gui_dist` / Caddy `file_server` 前端段已删（重做时走独立前端连 MCP）。
> Web API 本身也已 **deprecated**：存量端点只服务 remote hub 模式（`INVEST_API_BASE`
> 转发）与内部触发，不再新增端点。
>
> **多数用户不需要这一章**——单机用户直接 `pip install openinvest` / `uvx openinvest`
> 即可（见 [QUICK_START](../QUICK_START.md)）。这一章只服务"我要跑一个 remote hub
> 给多设备共享账本"的场景。

[← 07-extending](07-extending.md) · [Wiki 索引](README.md) · [09-troubleshooting →](09-troubleshooting.md)

---

## 拓扑总览

```
客户端（笔记本 CLI / agent skill，INVEST_API_BASE 转发）
  ↓ HTTPS
Cloudflare（DNS 橙色云 + Access JWT 鉴权）
  ↓ HTTP（Flexible 模式，源站不需要 cert）
你的服务器：80 / 443 端口
  ↓
Caddy 反代 (caddy-gateway 容器)
  └─ /api/*  → reverse_proxy 127.0.0.1:8765   (FastAPI, openinvest-web)
```

**为什么这套**：
- 没有 SSR daemon → 一台 1G VPS 就能跑（参考 mc-website 教训：`next start` 整机三次挂死）
- CF Access 在边缘鉴权 → 后端不写 auth 代码
- `127.0.0.1` 绑定 → 公网扫不到 8765

---

## 0. 容器一键自托管（Docker Compose / GHCR）

> 不想手装 uv / 配 systemd？用容器。镜像 `ghcr.io/longsizhuo/openinvest` 每个后端版本
> tag（`v*`）由 `publish-image.yml` 自动发布。**GUI 已退役**——容器只跑 API + scheduler，
> 没有网页面板。要 CF Access 保护，仍可在前面挂下面第 1–2 节的 Caddy（反代容器的
> `127.0.0.1:8765`）。

前置：Docker + Docker Compose v2。compose 文件在仓库里，所以这条路径要 clone
（这是 hub 部署，不是普通用户安装——普通用户走 `uvx openinvest`）：

```bash
git clone https://github.com/longsizhuo/openInvest.git && cd openInvest
cp .env.example .env && $EDITOR .env       # 至少填 DEEPSEEK_API_KEY（没 .env 也能起，但委员会跑不动）
```

**onboarding（建 `memory/`）**——`invest-agent`（scheduler）缺 `memory/user.md` 会拒启。
一次性命令走 `invest-web`（`invest-agent` 的 `entrypoint: ["/bin/sh","-c"]` 会吞掉追加参数）：

```bash
docker compose run --rm invest-web openinvest init
# 或在 Claude Code 里说"帮我初始化 invest"走 5 个问题
```

起服务：

```bash
docker compose up -d --build                 # 本地构建（首次几分钟：uv sync）
# —— 或拉预构建镜像（更快，需该 package 已 Public 或先 docker login ghcr.io）——
docker compose pull && docker compose up -d
```

验证：`curl http://localhost:8765/api/health`。

| 服务 | 作用 | 端口 |
|------|------|------|
| `invest-web` | FastAPI（deprecated，仅 hub 转发 + 内部触发）| 宿主 `127.0.0.1:8765`（默认只绑 loopback）|
| `invest-agent` | scheduler：跑 `jobs/*.yml`（daily_report / pnl_snapshot…）| 无 |

- **暴露到 LAN/公网**：把 `invest-web` 的 `ports` 改成 `"8765:8765"` 并设 `INVEST_API_TOKEN`；或保持 loopback、前面挂 Caddy + CF Access（见下文第 1–2 节）。
- **数据持久化**：`memory/`（账本，必挂）/ `db/` / `cache_data/` / `logs/` 都 bind-mount 到宿主，容器重建不丢。
- **镜像可见性**：GHCR 包首发是 private——要 `docker compose pull` 匿名拉，须在 GitHub Packages 把它设为 Public（或 `docker login ghcr.io`）。

---

## 1. 服务器一次性配置

### 1.1 装后端（PyPI）+ 数据目录

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install openinvest      # 或 pip install openinvest
mkdir -p ~/openInvest           # INVEST_HOME 数据目录（memory/ db/ .env）
cp .env.example ~/openInvest/.env   # 或手写；填 DEEPSEEK_API_KEY / EMAIL_* / 等
openinvest init                 # onboarding 建 memory/
```

> git clone 只用于开发后端本身。hub 服务器上装 wheel 即可；要 Docker compose /
> 自定义 systemd unit 模板才需要 clone 仓库拿那几个文件。

### 1.2 ~~拉前端 dist~~（已退役）

> 2026-07-05 起 GUI 退役：`scripts.sync_gui_dist` 已删，FastAPI 不再 mount
> `static/`，Caddy 也不再需要 file_server 段。invest-gui 仓库封存待重做
> （重做走独立前端连 MCP）。

### 1.3 systemd unit

仓库自带 unit 模板（`systemd/invest-web.service`），核心就是起 `openinvest-web`：

```ini
Environment=INVEST_HOME=%h/openInvest
EnvironmentFile=%h/openInvest/.env
ExecStart=%h/.local/bin/openinvest-web
# host/port 走 env：INVEST_WEB_HOST（默认 127.0.0.1）/ INVEST_WEB_PORT（默认 8765）
Restart=on-failure
ProtectSystem=strict
ReadWritePaths=%h/openInvest
```

```bash
sudo cp systemd/invest-web.service /etc/systemd/system/   # 按需改路径
sudo systemctl daemon-reload
sudo systemctl enable --now invest-web
sudo systemctl status invest-web
```

→ 仅写 INVEST_HOME 目录（systemd 加固）。
→ Restart=on-failure：进程挂了自动起，但**升级包后必须手动 restart 拉新**。

### 1.4 Caddy 站点配置

`caddy-gateway/Caddyfile` 加（只剩 API 反代，静态文件段已随 GUI 退役删除）：

```caddyfile
http://invest.your-domain.com {
    encode gzip

    handle /api/* {
        reverse_proxy 127.0.0.1:8765
    }
}
```

**改完 Caddyfile 必须 restart**（不是 reload）—— bind mount 模式 reload 不读新 inode：

```bash
docker restart caddy
```

详见 memory `feedback_caddy_bind_mount_reload`。

---

## 2. Cloudflare Access 配置

### 2.1 DNS

CF Dashboard → DNS → 加 `invest` A 记录指向服务器 IP，**橙色云开启**（走 CF proxy）。

### 2.2 Access Application

CF Zero Trust → Access → Applications → Add a self-hosted application：

| 字段 | 值 |
|------|------|
| Application name | invest |
| Application domain | `invest.your-domain.com` |
| Session duration | 30 days（或 24h，按你偏好）|
| Identity providers | One-time PIN（邮箱）|

### 2.3 Access Policy

```
Action: Allow
Include: Emails → your-email@gmail.com
```

→ 仅你的邮箱能进。

### 2.4 验证

- 退出所有 CF 邮箱会话
- `curl -H "CF-Access-Client-Id: ..." -H "CF-Access-Client-Secret: ..." https://invest.your-domain.com/api/health`
- 无 Service Token 的裸请求应被 CF 挡在边缘

---

## 3. 升级流程

```bash
uv tool upgrade openinvest      # 或 pip install -U openinvest；uvx 用户 uvx --refresh openinvest doctor
sudo systemctl restart invest-web
```

**重要**：systemd unit 不会自动 reload 新代码。每次升级完必须 `restart`。

**stdio MCP 子进程也要重启**：agent 宿主（Hermes / OpenClaw / Claude Code 等）按
config 自己 spawn 的 `openinvest-mcp` stdio 子进程不归 systemd 管，升级包、重启
invest-web/scheduler 都碰不到它——它会一直跑旧代码，直到宿主重启该 MCP client
（重启宿主 gateway/会话，或宿主自带的 MCP reload）。凡是改了 MCP 工具背后代码的
版本（例：2026-10 `ingest_event` 接上委员会触发闸），升级后要同时重启 agent 宿主的
MCP client，否则投喂门还是旧行为、爬虫门已是新行为。

（前端升级流程已随 GUI 退役删除——invest-gui 仓库封存待重做。）

---

## 4. 环境变量

### 必填

```bash
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
INVEST_WEB_HOST=127.0.0.1
INVEST_WEB_PORT=8765
```

### 可选

```bash
# CommSec 邮件导入（澳股用户）
EMAIL_SENDER=you@gmail.com
EMAIL_PASSWORD=app-password-16-chars

# 委员会跑完发邮件
SMTP_HOST=smtp.gmail.com
SMTP_USER=you@gmail.com
SMTP_PASS=app-password
SMTP_TO=you@gmail.com

# Discord DM 实时报警（可选）：event_watch 事件 / 委员会 verdict 先推 Discord
# 再发邮件。invest 不直接持有 Discord token——POST 给同宿主机 Discord bot 的
# alert server（X-Internal-Key 内网鉴权），由它代发 DM。两个都不填 = 完全禁用，
# 行为与从前一致（邮件仍是保底归档通道）。
CHATBOT_ALERT_URL=http://127.0.0.1:6200/alert/invest
CHATBOT_INTERNAL_KEY=shared-secret-with-your-bot

# 事件触发的委员会 verdict 邮件/DM 里"详情"链接的前缀。不设默认退化成
# http://localhost:8765——在邮件/DM 里点开等于打自己电脑，读信设备打不开。
# 设成你自己 invest-web 的公网可达地址（第 1-2 节配好 Caddy + CF Access 后
# 就有）。就算不设，每个资产也会带一条 explain_decision(decision_id) 的
# agent 调用提示当零配置兜底，但能点开的链接体验更好。
INVEST_API_BASE_URL=https://invest.your-domain.com

# 委员会行为开关（也可运行时经 API/CLI 改，ADR-017；env 仅部署期默认）
# 集中度 lens：false=单资产/刻意集中/全可投资金池不因持仓集中度被建议减仓（ADR-019）
INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=true
```

> NapCat QQ bot connector 已于 2026-07-05 删除，相关 `NAPCAT_*` / `INVEST_WHITELIST_QQ`
> env 不再生效。

### .env.example 是 source of truth

新增 env 必须更新 `.env.example`，否则 fork 用户不知道。

---

## 5. 加固清单（Server hardening 2026-05）

参考 memory `project_server_2026_05_02_hardening`：

- ✅ rpcbind / portmapper 关
- ✅ unattended-upgrades 自动安全补丁
- ✅ 5 个服务全部绑 127.0.0.1（外网扫不到端口）
- ✅ wcpp 停服（不用了）
- ✅ Caddy 仅放行 invest.* longsizhuo.com 和 mc.involutionhell.com
- ✅ ufw 仅开 22 / 80 / 443
- ⏳ TODO：Caddy CF IP 白名单（防绕 CF 直连源站）

---

## 6. systemd 服务清单

```bash
sudo systemctl list-units --type=service | grep invest
# invest-web.service          uvicorn FastAPI :8765
# invest-scheduler.service    APScheduler 跑所有 jobs/*.yml
```

`invest-scheduler.service`（如果你跑 cron）unit 类似：

```ini
ExecStart=%h/.local/bin/uv tool run --from openinvest python -m openinvest.scheduler.runner
# 或 pip 安装环境里直接 python -m openinvest.scheduler.runner
```

详见 `systemd/README.md` 和 `scheduler/README.md`。

---

## 7. 备份策略

### 权威状态散在两个 store——别只看 memory/

"钱"不是只在 `memory/`。无法重建的权威状态有两块，**必须一起备份**：

| 权威状态 | 是什么 | 丢了后果 |
|---|---|---|
| `memory/portfolio.md` | 当前持仓 + 现金 | 丢掉"我现在持有什么" |
| `db/trades.db` | 交易账本（planned→executed） | 丢掉 9 个月交易史 |
| `memory/{.committee,insights,daily,.dreams}` | 历史决议 / 洞察 | 理论可重生，但要烧大量 LLM token |
| `.env` | 凭据 + 配置 | 重新申请 / 填写 |

> ⚠️ **常见误区**：以为 `db/` 整个都是可丢的行情缓存。**`db/trades.db` 是账本不是缓存**——
> 照"db/ 不需备份"去迁移会丢掉整个交易历史。可丢的只是下面"不需要备份"列的那几个 db。

### 一键快照 / 迁移（推荐）

`scripts/snapshot.py`（开发仓脚本，不进 wheel——hub 上 clone 一份仓库或单拷这个
文件）把上面权威状态打成单个 tar.gz（WAL 安全的 sqlite online backup + sha256
校验），新机一条命令拉起。**迁移 hub 到新机器就用它**：

```bash
# 旧机：打包（不含 .env 密钥值，只在 manifest 列出要填哪些 key）
INVEST_HOME=~/openInvest uv run python -m scripts.snapshot snapshot --out ~/invest-snapshot.tar.gz

# 新机：装好 openinvest 后还原（默认拒绝覆盖已有账本，--force 才覆盖）
INVEST_HOME=~/openInvest uv run python -m scripts.snapshot restore --in ~/invest-snapshot.tar.gz
```

### 异地备份（每周自动）

`~/openinvest-research-archive/refresh.sh`（cron `0 3 * * 0`）每周把决议/洞察
**和账本（`portfolio.md` + `trades.db`，落 `invest_ledger/`）**推到私有 repo
`openinvest-research-archive`。这是当前唯一的机外副本——别误删那条 cron。

### 每天本地冷备（可选，建议加）

```bash
# 每天 cron——直接复用 snapshot.py，一份 tar 含全部权威状态
0 4 * * *  cd $HOME/repos/openInvest && INVEST_HOME=$HOME/openInvest $HOME/.local/bin/uv run python -m scripts.snapshot snapshot --out /backup/invest-$(date +\%F).tar.gz
```

### Cloudflare Access 配置

CF Dashboard 端的 Access policy 没有 git 备份，建议手动截图保存策略 JSON。

### 不需要备份（新机首跑自动重建）

- `db/market_data.db` / `db/events.db` （行情 + 新闻缓存，可重新拉）
- `db/chroma.sqlite3` / `db/jobs.sqlite` （向量库 / 调度器 job store，可重建）
- `cache_data/` （HTTP cache）
- `memory/.backtest*` （回测研究产物，非账本）

---

## 8. 监控（可选）

后端日志：
```bash
sudo journalctl -u invest-web -f --since "1 hour ago"
```

CF Access 日志：CF Zero Trust → Logs → Access。

数据源健康：`uvx openinvest doctor`（或 hub 上 `curl 127.0.0.1:8765/api/health`）。

---

## 9. 多设备：hub-and-spoke 远端模式（2026-06）

一台机器（hub）持有唯一的 `memory/` 并跑 web_api；其他设备（笔记本/另一台
开发机）的 CLI / Claude Code skill 设 `INVEST_API_BASE` 后所有子命令转发到
hub，读写都走 HTTP——**锁仍是 hub 单机 fcntl，零分布式复杂度**。中央调参
自动成立：strategy / 角色 prompt 都在 hub，改一处全设备生效。

### 推荐新路径：remote MCP（**BETA**，2026-07，REST 退役路线 A）

> ⚠️ **BETA**：作者本人未在真实多设备环境测试过（仅自动化测试 + hub 本机 curl
> 端到端）。已知问题与求助清单见 issue（HELP WANTED）。

hub 常驻 `openinvest-mcp --http`（streamable-HTTP，绑 127.0.0.1:8766，
`systemd/invest-mcp.service`），spoke 机器的 **agent 直连 MCP**，不再经
CLI→REST 转发。全部工具（读/写/委员会 Direct）均可用，鉴权复用同一
`INVEST_API_TOKEN`（bearer，`/health` 豁免）：

```bash
# spoke 侧注册（Claude Code；其他 MCP client 同理配 url + header）
claude mcp add --transport http openinvest https://invest.your-domain.com/mcp \
    --header "Authorization: Bearer $INVEST_API_TOKEN"
# CF Access Service Token 场景再加：
#   --header "CF-Access-Client-Id: xxx.access" --header "CF-Access-Client-Secret: yyy"
```

hub 的 Caddy 加一条路由（后端 Host 校验：token 模式默认关闭——反代下游 Host
是公网域名，token 才是信任边界；要收紧可设
`INVEST_MCP_ALLOWED_HOSTS=invest.your-domain.com` 白名单）：

```caddy
handle /mcp {
    reverse_proxy 127.0.0.1:8766 {
        flush_interval -1
    }
}
```

nginx 等反代需要关闭响应缓冲，让 SSE 进度和心跳及时到达客户端：

```nginx
location = /mcp {
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

**只靠 CF Access、不设 `INVEST_API_TOKEN`（Access-only）时必须设
`INVEST_MCP_ALLOWED_HOSTS`**：无 token 时后端保留 MCP SDK 默认的 loopback Host
白名单（`127.0.0.1` / `localhost` / `[::1]`），反代转发来的公网域名 Host 会直接
被回 **421 Invalid Host header**。在 hub 的 `.env` 写上对外域名（逗号分隔，
条目支持 `host:*` 通配端口）：

```bash
INVEST_MCP_ALLOWED_HOSTS=invest.your-domain.com
```

Access Service Token 和应用 Bearer token 是两层独立鉴权，组合使用时客户端必须
同时发送上文的三项 header。

**与 REST 转发的关系**：CLI→REST 转发（下文 `INVEST_API_BASE`）进入维护模式，
仍支持但不再演进——它还覆盖 remote MCP 没有的 Coordinator 协议
（prepare/save_committee）与 doctor/event_check；日常读写/Direct 委员会请优先
remote MCP。HTTP 使用 stateless SSE：`run_committee` 在同一请求中返回最终结果，
期间发送 SDK 心跳，并在客户端提供 progress token 时推送阶段进度。同步工具的
阻塞 IO 在线程池执行，慢查询不会占住事件循环、阻塞其他客户端或心跳。
自写脚本直连 `/mcp` 时，POST 的 `Accept` 头必须同时列出 `application/json` 和 `text/event-stream`，只写 `application/json` 会得到 406（官方 SDK、Claude Code、Codex 都已这样发）。

已知限制：心跳不能延长客户端设置的总调用期限，也不能保证所有代理配置都允许
长请求；仍需按真实部署验证 Cloudflare/Access 链路。Cloudflare 的实际限额以
[官方 524 文档](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
为准。stateless 模式不提供断线续传；调用中断后应先查状态/当日委员会缓存，
不要自动重放买卖、入金等非幂等写操作。长任务也可继续由 hub 侧 cron/REST 轮询路径跑。
自动部署（invest-deploy.sh）的 restart 行记得加 `invest-mcp.service`。

---

## 10. 顾问模式（INVEST_ADVISORY_MODE）

> **适用场景**：你想在群聊里部署一个\"投资顾问版艾露猫\"，让其他人可以问
> \"XXX 能不能买\"、\"怎么看黄金\"，但**不暴露你的真实持仓**，也不能买卖操作。

设置 `INVEST_ADVISORY_MODE=1` 环境变量即可开启：

```bash
# 启动 MCP 时注入
INVEST_ADVISORY_MODE=1 uvx openinvest mcp
```

### 顾问模式行为变化

顾问模式白名单只放行委员会分析必需的工具 + 新闻源管理（只动本实例自己的
`INVEST_HOME/rss_feeds.yml`），其余一律拒绝（包括事件入库 `ingest_event`）（`mcp_server.py`
的 `ADVISORY_ALLOWED_TOOLS`；改动需求见
[test_mcp_server.py::test_advisory_mode_gate_is_closed_set](../../tests/test_mcp_server.py)
的机器强制契约，白名单外新增工具漏加闸会直接 CI 红）：

| 工具 | 正常模式 | 顾问模式 |
|------|:--------:|:--------:|
| `run_committee` | ✅ 任意标的 | ✅ 任意标的 |
| `explain_decision` | ✅ | ✅ |
| `live_prices` | ✅ | ✅ |
| `ingest_event` | ✅ 入库 + 命中持仓时按频控触发委员会 | ❌ 不可用（入库内容会被后续顾问分析召回） |
| `news_sources` / `add_news_source` / `remove_news_source` | ✅ | ✅ 管理本实例自己的额外源清单（仅公网 http(s) 地址、直连不走 `HTTPS_PROXY` + probe 校验 + 上限 30，群聊自助喂源用） |
| `what_if` | ✅ | ❌ 不可用（本质是读真实持仓做假设推演，会泄露仓位/浮盈） |
| `record_execution` | ✅ | ❌ 不可用（写真实决策账本，顾问模式下无合法用途） |
| `status` / `strategy` / `history` / `discipline` / `decisions` | ✅ | ❌ 不可用 |
| `buy` / `sell` / `record_existing_position` | ✅ 记账 | ❌ 不可用 |
| `deposit` / `withdraw` | ✅ 记账 | ❌ 不可用 |
| `set_allocations` | ✅ | ❌ 不可用 |
| `track_asset` / `untrack_asset` | ✅ | ❌ 不可用 |

委员会分析仍然完整运行（Macro / Quant / Risk / CIO 四角色），但：

- `portfolio_summary` 显示为顾问模式占位文案，不含任何真实持仓数据；
- Dreaming 长期洞察（`prior_insights`）不注入 prompt；
- 委员会跑完**不落盘**到你的真实 memory / 决策账本（`persist_to_memory=False`），
  群聊查询不会污染你自己的 `history` / `decisions` / path_review 命中率统计。

**已知残留风险**：`explain_decision` 允许查看历史委员会 verdict——如果
`decision_id` 对应的分析是在**开启顾问模式之前**（也就是正常模式、真实持仓上下文
下）跑出来的，那份历史 transcript 本身就含真实 `portfolio_summary`。顾问模式没有
（也无法）事后过滤已落盘的历史文件。真正的隔离需要下面这条部署建议里的独立
`INVEST_HOME`。

另外，如果你给顾问实例的 `INVEST_HOME` 配了定时抓取，群聊里加的额外新闻源内容会
进入之后的顾问分析；所以顾问实例永远不要和你自己的真实实例共用 `INVEST_HOME`。

### 部署建议

**强烈建议顾问实例用独立 `INVEST_HOME`**（独立 `memory/` 目录，跑
[invest-setup skill](../../skills/invest-setup/) 或手动建一份空持仓即可），
而不是复用你自己账户的 `INVEST_HOME`——这样即使 `explain_decision` 被问到某个
`decision_id`，读到的也是顾问实例自己积累的历史，不会是你的真实持仓分析。

日常聊天场景（Hermes 等按 config 自己 spawn 子进程的 client）直接声明 stdio
server，不需要手动起终端：

```yaml
# ~/.hermes/config.yaml —— 与主实例并行注册一个顾问命名空间
mcp_servers:
  openinvest-advisor:
    command: uvx
    args: ["openinvest", "mcp"]
    env:
      INVEST_HOME: ~/openinvest-advisor   # 独立顾问实例的 memory 目录
      INVEST_ADVISORY_MODE: "1"
```

如果是给群聊 bot 后端复用（多个用户共享同一个常驻顾问实例，或跨机器连），按第
9 节的 remote MCP 方式起 HTTP 常驻服务再注册：

```bash
INVEST_HOME=~/openinvest-advisor INVEST_ADVISORY_MODE=1 \
    uvx openinvest mcp --http --port 8767

# client 侧按 remote MCP 一节的方式注册（以 Claude Code 为例）：
claude mcp add --transport http openinvest-advisor http://127.0.0.1:8767/mcp
```

对应的 agent prompt 模板见 [20-agent-usage-tutorial.md](20-agent-usage-tutorial.md)。

```
笔记本 (client)                         hub（本机/VPS）
  run.sh status ──HTTP──┐                invest-web.service :8765
  run.sh buy ...        ├──────────────▶   ├─ /api/skill/*      （CLI 等价端点）
  prepare_committee ────┘                  ├─ /api/committee/*  （prepare/save/run）
  （本机零 memory/）                        └─ memory/  ← 唯一账本，fcntl 锁
```

### hub 侧（已有部署零必改）

走既有 Caddy + CF Access 的部署什么都不用动——客户端用 CF Access Service
Token 过边缘即可（推荐，见下）。没有 CF 的局域网场景才需要：

```bash
# .env
INVEST_API_TOKEN=<openssl rand -hex 24>   # 开应用层鉴权
INVEST_WEB_HOST=0.0.0.0                   # 绑出 loopback（局域网直连时）
```

token 语义：**所有来源（含 loopback）**访问 `/api/*`（`/api/health` 豁免）
及 `/docs` `/openapi.json` `/redoc` 都要求 `Authorization: Bearer`；不设 token
行为完全不变。（原 loopback 豁免已删——反代下连接源恒为 127.0.0.1，豁免即裸奔；
token 模式下浏览器裸开 /docs 会 401，用 `curl -H` 或反代注入 header。）

### 客户端侧（2 分钟）

```bash
# 装 skill（run.sh 首跑自动从 PyPI 拉 openinvest），然后 $INVEST_HOME/.env 只要：
INVEST_API_BASE=https://invest.your-domain.com   # 或 http://10.0.0.x:8765
INVEST_API_TOKEN=...                             # hub 开了才需要
~/.claude/skills/invest/scripts/run.sh doctor    # 验证：status ready + remote 段
```

客户端**没有** `memory/`、不需要 DeepSeek key / Gmail 凭据。`init` 在远端
模式下被禁用；`run_committee` 在 hub 上跑（CLI 自动轮询）；`live_prices` /
`correlate` 仍本地跑。写操作落 hub 账本，history 记 `source: skill_remote`。
`buy --existing-position`（补录已持有仓位、不扣现金）不走 REST 转发——`/api/skill/buy`
没有这个字段，客户端直接报错而不是让 hub 扣现金；去 hub 上跑，或用 remote MCP 的
`record_existing_position` 工具（hub 版本没有这个工具时回 `Unknown tool`，不会扣现金）。

### 推荐：Cloudflare Tunnel + Access Service Token（hub 不开公网端口）

1. CF Zero Trust → Access → Service Auth → 创建 Service Token，拿到
   Client ID / Secret
2. Access Application（invest 域名）的 Policy 加一条 `Service Auth` include
3. 客户端 .env：
   ```bash
   INVEST_API_BASE=https://invest.your-domain.com
   CF_ACCESS_CLIENT_ID=xxx.access
   CF_ACCESS_CLIENT_SECRET=yyy
   ```
   remote dispatch 会自动带上 `CF-Access-Client-Id/Secret` 头；浏览器用户
   照旧走 SSO。hub 继续只绑 127.0.0.1，后端零改动。

### 已知限制

- 委员会跑在独立 daemon 线程（#105），其余阻塞端点（event_watch 扫描、
  committee prepare、yfinance / IMAP 读、sqlite）由 FastAPI 线程池执行
  （#233-3），不再冻结事件循环，单 worker 即可；委员会长任务走"触发 + 轮询"，
  不会撞 CF ~100s 代理超时。
- 客户端与 hub 的"同日 cache"以 **hub 的日期**为准（取自 `/api/health`
  时间戳），跨时区设备不会错位。

---

## 11. 无裁决模式（INVEST_NO_VERDICT_MODE）

> **适用场景**：对公众开放的演示实例。委员会只给正反理由，不给买卖结论——未经许可
> 向公众提供金融领域的确定性结论有合规风险，很多平台规则也明令禁止。

`INVEST_NO_VERDICT_MODE=1` 时（一般和顾问模式一起开，用独立 `INVEST_HOME`）：

- Macro / Quant / Risk 照常辩论，**CIO 换成书记员**（`capabilities/committee/scribe`），
  只整理“支持的理由 / 反对的理由 / 分歧最大的地方”，末尾固定附免责声明；
- 服务端逐字闸（`core/committee/debate.py:find_verdict_language`）：纪要里出现裁决词、
  “建议买/卖/加仓……”、金额、仓位、目标价/支撑阻力、概率数字 → 带着命中片段重写一次，
  再命中就整份拦下返回错误（fail closed，不做局部删改）；
- 出口白名单：MCP `run_committee` 和 CLI `run_committee` 只返回 `debate_summary`，
  不读当天缓存（缓存是带 verdict 的 transcript），不落盘；
- `explain_decision` / `decisions` 直接拒绝；
- **web_api 拒绝启动**：它的委员会/历史/SSE 端点处处带 verdict 和角色发言预览，没有逐个加闸。
  无裁决部署只起 `openinvest-mcp`。

```bash
INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 INVEST_NO_VERDICT_MODE=1 \
    uvx openinvest mcp --http --port 8768
```

不覆盖的：Coordinator 路径（`prepare_committee` + 宿主 agent 自己 spawn 的 CIO）和
daily_report 定时任务，它们不是公开出口，演示实例也不该跑。书记员 prompt 和逐字闸目前
只覆盖中文。

## 下一步

→ [09-troubleshooting.md](09-troubleshooting.md) — 部署后跑挂了去哪查

→ [adr/](adr/) — 为什么这套部署模式（不上 SSR / 不上 K8s）
