---
type: wiki-chapter
title: 故障排查
tags: [troubleshooting, ops, deployment, debugging]
intent: 故障排查
documents:
  endpoints:
    - GET /api/health
    - GET /api/portfolio/total_value
    - GET /api/committee/live/{task_id}
    - GET /api/data_sources/health
    - GET /api/pnl_chart.svg
    - POST /api/cash/{currency}/deposit
    - POST /api/cash/{currency}/withdraw
  config_keys: []
  symbols: []
---

# 故障排查

> 线上跑挂了去哪查、SSE 断了怎么 debug、sync_gui_dist 失败怎么办。
> 按"症状 → 诊断步骤 → 修复"组织。

[← 08-deployment](08-deployment.md) · [Wiki 索引](README.md) · [10-design-system →](10-design-system.md)

---

## 总原则

发现问题 → **先看日志，不要先猜**：

```bash
# invest-web 后端
sudo journalctl -u invest-web -f --since "1 hour ago"

# Caddy（容器化）
docker logs caddy --tail 100 -f

# scheduler / cron jobs
sudo journalctl -u invest-scheduler -f

# CF Access
# 浏览器开 CF Zero Trust → Logs → Access
```

→ 90% 的问题日志直接告诉你原因。

---

## 1. GUI 完全打不开

### 症状

- 浏览器访问 `invest.your-domain.com` 转圈、白屏、502

### 诊断顺序

```bash
# 1. CF 能 reach 源站吗
curl -I https://invest.your-domain.com
# → 期望 302（CF Access 重定向到登录）

# 2. Caddy 在跑吗
docker ps | grep caddy
# → 期望 caddy 进程 healthy

# 3. Caddy 配置对吗
docker exec caddy cat /etc/caddy/Caddyfile | grep invest

# 4. 静态文件存在吗
ls -la /srv/invest-gui/index.html
# → 期望 .html 存在 + 文件最近的修改时间合理

# 5. 后端在监听吗
curl -I http://127.0.0.1:8765/api/health
# → 期望 HTTP 200

# 6. systemd 状态
sudo systemctl status invest-web
```

### 常见原因 + 修复

| 症状 | 原因 | 修复 |
|------|------|------|
| `curl localhost:8765 Connection refused` | invest-web 没起 | `sudo systemctl restart invest-web` |
| `502 Bad Gateway` | Caddy 反代到的端口没人监听 | 同上 |
| Caddy 配置看着对但不生效 | bind mount + reload 不读新 inode | `docker restart caddy`（不是 reload）|
| `/srv/invest-gui/index.html` 不存在 | 没部署 GUI | `cd invest-gui && pnpm deploy` |
| CF Access 无限重定向 | Access policy 未包含你的邮箱 | CF Dashboard 加邮箱 |

---

## 2. 端点 404 但代码里有

### 症状

```bash
curl http://127.0.0.1:8765/api/portfolio/total_value?base=CNY
# → {"detail":"Not Found"}
```

但代码里 `connectors/web_api.py` 明明有这个 endpoint。

### 原因

**99% 是 systemd 进程跑的是旧代码**：systemd `Restart=on-failure` 只在 crash 时 restart，git pull 后不会自动 reload。

### 修复

```bash
sudo systemctl restart invest-web
sleep 2
curl http://127.0.0.1:8765/api/portfolio/total_value?base=CNY
# → {"base_currency":"CNY","grand_total":...}
```

→ **每次 push 后端必须手动 restart**。详见 [08-deployment.md#升级流程](08-deployment.md#升级流程)。

### 长期方案

考虑加 git post-receive hook 或 deploy 脚本里固化 restart 步骤。

---

## 3. 委员会触发后没反应 / SSE 不直播

### 症状

GUI 「触发委员会」按钮按了，task_id 出来了，但状态一直 `queued` 或 `running` 不动。

### 诊断

```bash
# 1. 任务状态文件存在吗
ls ~/openInvest/memory/.committee/<task_id>/

# 2. status.json 在变吗（每个 stage 完成会写）
watch -n 1 cat ~/openInvest/memory/.committee/<task_id>/status.json

# 3. journal 里 LLM 调用在跑吗
sudo journalctl -u invest-web --since "5 minutes ago" | grep -i "deepseek\|httpx"

# 4. SSE 端点能直接 curl 吗
curl -N http://127.0.0.1:8765/api/committee/live/<task_id>
# 期望持续输出 event: progress / data: ...
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| status.json 一直 queued | asyncio.create_task 没起 | 检查 journal 是否有 traceback；restart 后端 |
| LLM 一直没 200 | DeepSeek API key 错 / 限速 / 余额不足 | `.env` 检查 / `journal` 看 401 / 429 |
| status.json 跳到 done 但前端没收到 | SSE 连接被 CF Access 5min idle 切了 | 检查代码 keepalive 是否在跑（25s 一次）|
| running 但 LLM 一直没调 | 被锁住（其他 task 占了 fcntl 锁）| `lsof memory/portfolio.md.lock` 找出占用方 |

### v3 收敛检测看着像 bug

看到 `final_round: 3, converged: true, max_debate_rounds: 4` 不要慌——
这是收敛提前退出，不是 round 4 没跑完。详见 [02-agents.md#收敛检测](02-agents.md#收敛检测)。

---

## 4. yfinance 拉不到价 / 行情陈旧

### 症状

GUI HoldingCard 显示"⚠ 陈旧"标记，或行情字段是 `null`。

### 诊断

```bash
# 用 Python 直接试
cd ~/openInvest
uv run python -c "from utils.exchange_fee import get_history_data; df = get_history_data('NDQ.AX', '5d'); print(df.tail())"
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| `403 Forbidden` 反复出现 | yfinance 被 IP 限速 | 等 5-10min；或换 yfinance proxy 设置 |
| df 是 None | symbol 错 / yfinance 不覆盖 | 在 `https://finance.yahoo.com` 验证 ticker 是否存在 |
| Close 列为空 | 周末 / 假期没新数据 | 这是正常的，DB 兜底应该接管 |
| BetaShares 403 NDQ.AX 拉不到 | NDQ 走自家 scraper 失败 | yfinance fallback 自动接（journal 里看到 `🔄 yfinance fallback`）|

### 数据源全景诊断

GUI `/system` → "数据源" tab 一眼看所有数据源最后成功时间 + is_stale。
对应 endpoint：`GET /api/data_sources/health`。

---

## 5. CommSec 邮件没拉到成交

### 症状

下单后过几小时，portfolio.md 里没有新 holding。

### 诊断

```bash
# v3 改了：CommSec 默认 disable cron，必须手动触发
uv run python -m scripts.import_commsec --lookback 30
# 期望显示拉到的成交列表（dry-run）

# 真正写入
uv run python -m scripts.import_commsec --lookback 30 --apply
```

或 GUI 「Import CommSec」按钮（如果加了）。

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| `IMAP 连接失败` | EMAIL_PASSWORD 不是应用密码 | Gmail 必须用 [应用密码](https://myaccount.google.com/apppasswords)，不是登录密码 |
| 拉到 0 条 | lookback 太短或邮件已被 processed | `--lookback 365` 重试 |
| 拉到了但写入失败 | symbol 不在 strategy.target_assets | 先去 strategy 加；或先 add holding |

### 为什么默认 disable cron

cron 自动同步在 IMAP 临时失败时会**静默漏成交**，导致 portfolio 和真实账户对不上。
改手动模式让用户先 dry-run 看清楚再 apply。详见 [05-data-model.md](05-data-model.md)。

---

## 6. PnL 图表数据不对 / 跳楼

### 症状

`/api/pnl_chart.svg` 折线断崖、有未来日期。

### 诊断

```bash
# pnl_history.jsonl 直接看
tail -20 ~/openInvest/memory/pnl_history.jsonl
# 找时间戳异常的行（凌晨非交易时段、未来时间）
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| 凌晨有数据点 | jobs/pnl_snapshot 在工作时段外被触发 | `python -m scripts.clean_pnl_history --dry-run` 然后真删 |
| 未来日期 | 时区配置错（UTC vs Asia/Shanghai）| `.env` `TZ=Asia/Shanghai` 同步 |
| 某天垂直跳变 | 用户没记账，scheduler 仍在跑 | 用 `/api/cash/CNY/deposit|withdraw` 补记 |

清理脚本：
```bash
python -m scripts.clean_pnl_history --dry-run   # 预览
python -m scripts.clean_pnl_history             # 实际执行（自动备份）
python -m jobs.pnl_snapshot --render-only       # 重渲染 SVG
```

---

## 7. sync_gui_dist 失败

### 症状

```bash
uv run python -m scripts.sync_gui_dist
# ❌ 下载失败
```

### 诊断

```bash
# 直接试拉
curl -I https://github.com/longsizhuo/invest-gui/releases/download/dist-latest/invest-gui-dist.tar.gz
# 期望 302 → S3 → 200
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| `404 dist-latest tag 不存在` | 仓库刚 push 但 GHA 还没跑完 | 等 1-2 min，或去 [Actions](https://github.com/longsizhuo/invest-gui/actions) 看跑完没 |
| `Connection timeout` | 服务器到 github.com 网络挡 | 用代理或 raw.githubusercontent.com 镜像 |
| `403` | tag 不公开（私有仓库）| `gh auth login` 或用 access token |

---

## 8. 测试挂了

### 症状

```bash
uv run pytest tests/
# FAILED tests/test_xxx.py
```

### 诊断

```bash
# 单独跑挂的那个 + verbose
uv run pytest tests/test_xxx.py::test_yyy -v --tb=long
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| `yfinance 拉数据失败` | 网络 / 限速 | 重跑几次；或加 mock |
| `ImportError 不能 import _render_portfolio_body` | v2 改名 → `_render_portfolio_body_v2` | CI 修过；本地代码可能旧 |
| `ValidationError schema_version` | portfolio.md 不符 v2 schema | 跑 `python -m scripts.migrate_portfolio_to_holdings` |

166 测试是底线，加新功能必须保持全绿。

---

## 9. NapCat QQ bot 不响应命令

### 症状

QQ 私聊发 `/balance`，bot 不回。

### 诊断

```bash
# 1. NapCat 在跑吗
ps aux | grep -i napcat

# 2. invest-napcat connector 在跑吗
ps aux | grep "connectors.napcat_bot"

# 3. journal 看接收事件
sudo journalctl --since "5 min ago" | grep napcat
```

### 常见原因

| 症状 | 原因 | 修复 |
|------|------|------|
| 收到消息但拒绝 | `INVEST_WHITELIST_QQ` 没设 / 不是你的 QQ 号 | `.env` 加 `INVEST_WHITELIST_QQ=你的QQ` |
| 收不到事件 | NAPCAT_WS_URL 错 / NapCat 没开 WS | 检查 NapCat 启动参数 |
| 收到但 reply 失败 | NAPCAT_HTTP_URL 错 | 检查 NapCat HTTP API 端口 |

详见 `connectors/README.md`。

---

## 10. 应急联系 / 找历史

- 历史决议：`memory/daily/<date>/<symbol>.md`
- LLM 调用 telemetry：`memory/llm_usage.jsonl`
- Tool 调用审计：`memory/.committee/<task_id>/tool_calls.jsonl`
- Dreaming 事件：`memory/.dreams/events.jsonl`
- 委员会任务状态：`memory/.committee/<task_id>/status.json`

→ 几乎所有"为什么 AI 这次说 BUY"的问题都能从这些文件复盘。

---

## 下一步

→ [08-deployment.md](08-deployment.md) — 部署相关问题

→ [QUICK_START.md](../QUICK_START.md) Troubleshooting 章节 — fork 用户安装期的问题

→ 还有问题没解决？开 [GitHub Issue](https://github.com/longsizhuo/openInvest/issues)
