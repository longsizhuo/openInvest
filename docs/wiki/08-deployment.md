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

> Caddy + Cloudflare Access + systemd + GitHub Releases dist 分发。
> 这一章是"我要把它跑在自己服务器上"的完整链路。

[← 07-extending](07-extending.md) · [Wiki 索引](README.md) · [09-troubleshooting →](09-troubleshooting.md)

---

## 拓扑总览

```
浏览器
  ↓ HTTPS
Cloudflare（DNS 橙色云 + Access JWT 鉴权）
  ↓ HTTP（Flexible 模式，源站不需要 cert）
你的服务器：80 / 443 端口
  ↓
Caddy 反代 (caddy-gateway 容器)
  ├─ /api/*  → reverse_proxy 127.0.0.1:8765   (FastAPI invest-web.service)
  └─ /*      → file_server /srv/invest-gui/   (静态文件)
```

**为什么这套**：
- 没有 SSR daemon → 一台 1G VPS 就能跑（参考 mc-website 教训：`next start` 整机三次挂死）
- CF Access 在边缘鉴权 → 后端不写 auth 代码
- `127.0.0.1` 绑定 → 公网扫不到 8765
- Caddy 静态文件直 serve → GUI 升级 = rsync `dist/` 到 `/srv/invest-gui/`，无需 reload

---

## 0. 容器一键自托管（Docker Compose / GHCR）

> 不想手装 uv / 配 systemd？用容器。镜像 `ghcr.io/longsizhuo/openinvest` 每个后端版本
> tag（`v*`）由 `publish-image.yml` 自动发布，**GUI 已在 build 期烤进 `static/`**——
> `docker compose up` 起来浏览器直接看完整看板。要 CF Access 保护，仍可在前面挂下面
> 第 1–2 节的 Caddy（反代容器的 `127.0.0.1:8765`）。

前置：Docker + Docker Compose v2。

```bash
git clone https://github.com/longsizhuo/openInvest.git && cd openInvest
cp .env.example .env && $EDITOR .env       # 至少填 DEEPSEEK_API_KEY（没 .env 也能起，但委员会跑不动）
```

**onboarding（建 `memory/`）**——`invest-agent`（scheduler）缺 `memory/user.md` 会拒启。
一次性命令走 `invest-web`（`invest-agent` 的 `entrypoint: ["/bin/sh","-c"]` 会吞掉追加参数）：

```bash
docker compose run --rm invest-web python -m scripts.skill init
# 或在 Claude Code 里说"帮我初始化 invest"走 5 个问题
```

起服务：

```bash
docker compose up -d --build                 # 本地构建（首次几分钟：uv sync + 烤 GUI）
# —— 或拉预构建镜像（更快，需该 package 已 Public 或先 docker login ghcr.io）——
docker compose pull && docker compose up -d
```

浏览器开 <http://localhost:8765> → 完整 GUI。

| 服务 | 作用 | 端口 |
|------|------|------|
| `invest-web` | FastAPI + GUI（uvicorn 绑 `0.0.0.0:8765`）| 宿主 `127.0.0.1:8765`（默认只绑 loopback）|
| `invest-agent` | scheduler：跑 `jobs/*.yml`（daily_report / pnl_snapshot…）| 无 |

- **暴露到 LAN/公网**：把 `invest-web` 的 `ports` 改成 `"8765:8765"` 并设 `INVEST_API_TOKEN`；或保持 loopback、前面挂 Caddy + CF Access（见下文第 1–2 节）。
- **数据持久化**：`memory/`（账本，必挂）/ `db/` / `cache_data/` / `logs/` 都 bind-mount 到宿主，容器重建不丢。
- **镜像可见性**：GHCR 包首发是 private——要 `docker compose pull` 匿名拉，须在 GitHub Packages 把它设为 Public（或 `docker login ghcr.io`）。

---

## 1. 服务器一次性配置

### 1.1 装 uv + clone 仓库

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/longsizhuo/openInvest.git ~/openInvest
cd ~/openInvest
uv sync --frozen --python 3.13
cp .env.example .env
# 编辑 .env 填 DEEPSEEK_API_KEY / EMAIL_* / 等
```

### 1.2 拉前端 dist

```bash
uv run python -m scripts.sync_gui_dist
# = 从 invest-gui dist-latest release 拉 invest-gui-dist.tar.gz
# = 解压到 static/（FastAPI 自带 GUI mount 用）
```

GUI 部署有两套，并存无冲突：

| 路径 | 谁 serve | 升级方式 |
|------|---------|----------|
| `<repo>/static/` | FastAPI mount | `python -m scripts.sync_gui_dist`（拉 release）|
| `/srv/invest-gui/` | Caddy file_server | `cd invest-gui && pnpm deploy`（rsync 本机 build）|

**生产 Caddy 优先后者**，FastAPI mount 是单机一键场景用的。

### 1.3 systemd unit

复制仓库自带的 unit：

```bash
sudo cp systemd/invest-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now invest-web
sudo systemctl status invest-web
```

unit 关键字段：
```ini
WorkingDirectory=%h/openInvest
EnvironmentFile=%h/openInvest/.env
ExecStart=%h/.local/bin/uv run --no-sync uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
Restart=on-failure
ProtectSystem=strict
ReadWritePaths=%h/openInvest
```

→ 仅写 invest 目录（systemd 加固）。
→ Restart=on-failure：进程挂了自动起，但**改代码后必须手动 restart 拉新**。

### 1.4 Caddy 站点配置

`caddy-gateway/Caddyfile` 加：

```caddyfile
http://invest.your-domain.com {
    encode gzip

    handle /api/* {
        reverse_proxy 127.0.0.1:8765
    }

    handle {
        root * /srv/invest-gui

        @viteAssets path /assets/*
        header @viteAssets Cache-Control "public, max-age=31536000, immutable"
        @html path *.html /
        header @html Cache-Control "no-cache"

        file_server
        try_files {path} /index.html   # SPA 路由 fallback
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
- 浏览器访问 `https://invest.your-domain.com`
- 应跳到 CF 邮箱验证页 → 输 OTP → 进入 GUI

---

## 3. 升级流程

### 升级后端

```bash
cd ~/openInvest
git pull origin main
uv sync   # 如果有依赖变化
sudo systemctl restart invest-web
```

**重要**：systemd unit 不会自动 reload 新代码。每次 push 完必须 `restart`。

### 升级前端

```bash
# 方案 A：从 GitHub Releases 拉
cd ~/openInvest
uv run python -m scripts.sync_gui_dist
# 注意：只更新 static/ 不动 /srv/invest-gui/

# 方案 B：本机构建 + rsync 到 Caddy serve 目录（生产用）
cd ~/invest-gui
git pull origin main
pnpm install
pnpm deploy   # = scripts/deploy.sh，rsync dist/ → /srv/invest-gui/
```

**Caddy 不需要 reload**（静态文件变化）。
**浏览器可能 cache HTML**（虽然 Caddyfile 设 no-cache）→ 用户硬刷一次。

### 同时升级前后端

最稳的顺序：

1. push 前后端代码
2. 服务器拉前端先（`pnpm deploy`）—— 用户拿到老 API 调老前端，正常
3. `git pull` 后端
4. `sudo systemctl restart invest-web` —— 切新 API
5. 用户硬刷 → 前端拿新 hash 化 JS → 调新 API ✓

**为什么前端先**：新前端调老 API 不会崩（向下兼容字段），老前端调新 API 也不会崩。
反过来：老前端调新前端依赖的 endpoint 会 404。

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

# NapCat QQ bot
NAPCAT_WS_URL=ws://localhost:6101
NAPCAT_HTTP_URL=http://localhost:6100
INVEST_WHITELIST_QQ=12345678   # 必填，否则 napcat 拒绝所有

# 委员会行为开关（也可运行时经 GUI/API/CLI 改，ADR-017；env 仅部署期默认）
# 集中度 lens：false=单资产/刻意集中/全可投资金池不因持仓集中度被建议减仓（ADR-019）
INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=true

# 开发环境（Vite 跨域调）
INVEST_WEB_DEV_CORS=1   # 仅本机 dev 时用，生产别开
```

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
ExecStart=%h/.local/bin/uv run --no-sync python -m scheduler.runner
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

`scripts/snapshot.py` 把上面权威状态打成单个 tar.gz（WAL 安全的 sqlite online
backup + sha256 校验），新机一条命令拉起。**迁移 hub 到新机器就用它**：

```bash
# 旧机：打包（不含 .env 密钥值，只在 manifest 列出要填哪些 key）
uv run python -m scripts.snapshot snapshot --out ~/invest-snapshot.tar.gz

# 新机：clone + uv sync 后还原（默认拒绝覆盖已有账本，--force 才覆盖）
uv run python -m scripts.snapshot restore --in ~/invest-snapshot.tar.gz
```

### 异地备份（每周自动）

`~/openinvest-research-archive/refresh.sh`（cron `0 3 * * 0`）每周把决议/洞察
**和账本（`portfolio.md` + `trades.db`，落 `invest_ledger/`）**推到私有 repo
`openinvest-research-archive`。这是当前唯一的机外副本——别误删那条 cron。

### 每天本地冷备（可选，建议加）

```bash
# 每天 cron——直接复用 snapshot.py，一份 tar 含全部权威状态
0 4 * * *  cd $HOME/openInvest && $HOME/.local/bin/uv run python -m scripts.snapshot snapshot --out /backup/invest-$(date +\%F).tar.gz
```

### Cloudflare Access 配置

CF Dashboard 端的 Access policy 没有 git 备份，建议手动截图保存策略 JSON。

### 不需要备份（新机首跑自动重建）

- `db/market_data.db` / `db/events.db` （行情 + 新闻缓存，可重新拉）
- `db/chroma.sqlite3` / `db/jobs.sqlite` （向量库 / 调度器 job store，可重建）
- `cache_data/` （HTTP cache）
- `static/` （前端 dist，`scripts.sync_gui_dist` 重新拉）
- `memory/.backtest*` （回测研究产物，非账本）

---

## 8. 监控（可选）

后端日志：
```bash
sudo journalctl -u invest-web -f --since "1 hour ago"
```

CF Access 日志：CF Zero Trust → Logs → Access。

GUI 数据源健康：`https://invest.your-domain.com/system` → "数据源" tab。

---

## 9. 多设备：hub-and-spoke 远端模式（2026-06）

一台机器（hub）持有唯一的 `memory/` 并跑 web_api；其他设备（笔记本/另一台
开发机）的 CLI / Claude Code skill 设 `INVEST_API_BASE` 后所有子命令转发到
hub，读写都走 HTTP——**锁仍是 hub 单机 fcntl，零分布式复杂度**。中央调参
自动成立：strategy / 角色 prompt 都在 hub，改一处全设备生效。

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

token 语义：非 loopback 来源访问 `/api/*`（`/api/health` 豁免）要求
`Authorization: Bearer`；不设 token 行为完全不变。

### 客户端侧（2 分钟）

```bash
# clone + uv sync 照常（run.sh 首跑自动），然后 .env 只要：
INVEST_API_BASE=https://invest.your-domain.com   # 或 http://10.0.0.x:8765
INVEST_API_TOKEN=...                             # hub 开了才需要
~/.claude/skills/invest/scripts/run.sh doctor    # 验证：status ready + remote 段
```

客户端**没有** `memory/`、不需要 DeepSeek key / Gmail 凭据。`init` 在远端
模式下被禁用；`run_committee` 在 hub 上跑（CLI 自动轮询）；`live_prices` /
`correlate` 仍本地跑。写操作落 hub 账本，history 记 `source: skill_remote`。

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

- 委员会同步跑会阻塞 uvicorn 事件循环 5-10 分钟（既有限制，远端化放大）：
  hub 建议 `--workers 2` 多进程部署；长任务全部走"触发 + 轮询"，不会撞 CF
  ~100s 代理超时。
- 客户端与 hub 的"同日 cache"以 **hub 的日期**为准（取自 `/api/health`
  时间戳），跨时区设备不会错位。

---

## 下一步

→ [09-troubleshooting.md](09-troubleshooting.md) — 部署后跑挂了去哪查

→ [adr/](adr/) — 为什么这套部署模式（不上 SSR / 不上 K8s）
