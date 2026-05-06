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

## 1. 服务器一次性配置

### 1.1 装 uv + clone 仓库

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/longsizhuo/openInvest.git ~/projects-review/invest
cd ~/projects-review/invest
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
WorkingDirectory=/home/ubuntu/projects-review/invest
EnvironmentFile=/home/ubuntu/projects-review/invest/.env
ExecStart=/home/ubuntu/.local/bin/uv run --no-sync uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
Restart=on-failure
ProtectSystem=strict
ReadWritePaths=/home/ubuntu/projects-review/invest
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
cd ~/projects-review/invest
git pull origin main
uv sync   # 如果有依赖变化
sudo systemctl restart invest-web
```

**重要**：systemd unit 不会自动 reload 新代码。每次 push 完必须 `restart`。

### 升级前端

```bash
# 方案 A：从 GitHub Releases 拉
cd ~/projects-review/invest
uv run python -m scripts.sync_gui_dist
# 注意：只更新 static/ 不动 /srv/invest-gui/

# 方案 B：本机构建 + rsync 到 Caddy serve 目录（生产用）
cd ~/projects-review/invest-gui
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
ExecStart=/home/ubuntu/.local/bin/uv run --no-sync python -m scheduler.runner
```

详见 `systemd/README.md` 和 `scheduler/README.md`。

---

## 7. 备份策略

### memory/ 是最关键的

```bash
# 每天 cron
0 4 * * *  rsync -a /home/ubuntu/projects-review/invest/memory/ /backup/invest-memory-$(date +\%F)/
```

`memory/` 含个人持仓 + 9 个月交易历史 + 长期 insights，丢了重建很难。

### Cloudflare Access 配置

CF Dashboard 端的 Access policy 没有 git 备份，建议手动截图保存策略 JSON。

### 不需要备份

- `db/` （行情缓存，可重新拉）
- `cache_data/` （HTTP cache）
- `static/` （前端 dist，可重新 sync）

---

## 8. 监控（可选）

后端日志：
```bash
sudo journalctl -u invest-web -f --since "1 hour ago"
```

CF Access 日志：CF Zero Trust → Logs → Access。

GUI 数据源健康：`https://invest.your-domain.com/system` → "数据源" tab。

---

## 下一步

→ [09-troubleshooting.md](09-troubleshooting.md) — 部署后跑挂了去哪查

→ [adr/](adr/) — 为什么这套部署模式（不上 SSR / 不上 K8s）
