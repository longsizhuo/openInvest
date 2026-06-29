# 零成本自托管教程（GitHub Actions）

把 openInvest 跑成一个**全自动、零服务器、零运维**的每日投资委员会：你 fork 一份，
填几个密钥，GitHub 每天定时帮你跑 4 角色 LLM 委员会，把报告发到你邮箱。不需要买云主机，
不需要挂机器，不需要会运维。

> 这份是**手把手详细版**。只想看 3 步速览的回 [README「零成本自托管」](../README.md#3-零成本自托管github-actions无需服务器)。

---

## 0. 它到底怎么跑的（先看懂，再动手）

```
你的私有 fork (含 memory/ 持仓状态)
        │
        ▼
GitHub Actions 每天 02:00 UTC (= 北京 10:00) 唤醒
        │
        ├─ 1. checkout 你的 fork（连带 memory/ 持仓）
        ├─ 2. uv sync 装依赖
        ├─ 3. 用你填的 secrets 生成 .env
        ├─ 4. 跑 `python -m jobs.daily_report`
        │       → 4 角色委员会辩论 → CIO 综合 verdict → 发邮件给你
        └─ 5. 把更新后的 memory/ commit 回你的 fork（决策历史进 git，可回溯）
```

整个过程在 GitHub 免费的 Actions 额度里跑完（公开仓库无限分钟、私有仓库每月 2000 分钟，
单次运行 2–5 分钟，一天一次绰绰有余）。你**唯一**的花费是 LLM API 的 token 钱
（DeepSeek 跑一次单资产委员会约几分钱）。

---

## 1. 前置准备（3 样东西）

动手前先备齐，省得中途卡壳：

| 需要 | 怎么拿 | 备注 |
|---|---|---|
| **GitHub 账号** | github.com 注册 | fork + Actions 都在这 |
| **一个 LLM API Key** | 见 [步骤 3](#3-拿一个-llm-api-key) | 委员会的"大脑"。推荐 DeepSeek（便宜） |
| **一个 Gmail + 应用专用密码** | 见 [步骤 4](#4-拿-gmail-应用专用密码) | 用来发报告邮件。**不是**你的登录密码 |

---

## 2. Fork 本仓库并设为 Private ⚠️

> **为什么必须 private**：运行时你的**真实持仓 / 现金 / 委员会决议**会被写进 `memory/`
> 并 commit 回这个 fork。public fork = 把你的钱包公开在互联网上。**这一步不能省。**

1. 打开 [github.com/longsizhuo/openInvest](https://github.com/longsizhuo/openInvest)，右上角点 **Fork**。
2. fork 完成后，进入**你的** fork → **Settings**（仓库设置，不是账号设置）。
3. 拉到最底部 **Danger Zone** → **Change repository visibility** → **Make private** → 按提示确认。

> GitHub 默认会**禁用 fork 的 Actions**。下一次进 **Actions** 标签页时会看到一个绿色按钮
> "I understand my workflows, go ahead and enable them" —— 点它启用（[步骤 7](#7-启用并首次运行) 还会再说）。

---

## 3. 拿一个 LLM API Key

以 **DeepSeek**（架构默认、最便宜）为例：

1. 打开 [platform.deepseek.com](https://platform.deepseek.com) 注册登录。
2. 左侧 **API Keys** → **Create new API key** → 复制（形如 `sk-xxxxxxxx...`，**只显示一次**，先存好）。
3. 充值几块钱（跑日报一天几分钱，10 块能用很久）。

> 用别的供应商（通义千问 / 智谱 GLM / 任何 OpenAI 兼容端点）也行 —— 把 Key 填进
> `LLM_API_KEY`，并额外加 `LLM_BASE_URL` secret 指向对应端点即可（见
> [README「底层 LLM Provider 配置契约」](../README.md#底层-llm-provider-配置契约)）。

---

## 4. 拿 Gmail 应用专用密码

报告通过 Gmail SMTP 发出。Gmail **不允许**用账号登录密码做 SMTP，必须用「应用专用密码」：

1. 先确保你的 Google 账号开了 **两步验证**（应用专用密码的前提）：
   [myaccount.google.com/security](https://myaccount.google.com/security) → **两步验证** → 开启。
2. 然后打开 [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)。
3. 应用名随便填（如 `openInvest`）→ **生成** → 得到一串 **16 位**密码（形如 `abcd efgh ijkl mnop`）。
4. **粘贴时去掉空格**，连成 `abcdefghijklmnop`。这就是 `EMAIL_PASSWORD`。

> `EMAIL_SENDER` = 你这个 Gmail 地址本身（如 `you@gmail.com`）。
> `DIGEST_EMAIL_TO` = 收报告的邮箱（可以和 sender 一样，自己发给自己）。

---

## 5. 生成你的持仓（memory/）并推到 fork

Actions 在云端跑，它得能读到**你的**持仓。所以要先在本地 init 一次，把生成的 `memory/`
推进你的私有 fork。

```bash
# 1) 把你的私有 fork clone 下来（注意是 YOUR_NAME，不是 longsizhuo）
git clone https://github.com/YOUR_NAME/openInvest.git ~/openInvest
cd ~/openInvest
uv sync

# 2) 初始化你的画像 + 持仓（交互式问答：合规名 / 风险 / 持仓 / 关注的资产 等）
uv run python scripts/skill.py init
#   ——或者如果你用 Claude Code / 支持 Skill 的终端，直接说「帮我初始化 invest」更顺
```

init 会写出 `memory/`（持仓 `portfolio.md`、策略 `strategy.md` 等）和 `user_profile.json`。

> **关键**：onboarding 里「你关注哪些资产」那一步决定了委员会每天分析谁
> （写进 `strategy.md` 的 `target_assets`）。**如果留空，daily_report 会直接跳过、邮件里啥也没有**
> （见[排错](#no_target_assets)）。

```bash
# 3) memory/ 默认在 .gitignore 里，必须 -f 强制加，推到你的私有 fork
git add -f memory/ user_profile.json
git commit -m "init: 我的持仓与画像"
git push
```

> 没装本地环境、跑不了 Python？退而求其次：在 GitHub 网页上手动建 `memory/portfolio.md`
> 和 `memory/strategy.md`（结构参考 [docs/memory_layout.md](memory_layout.md)）。但**强烈建议**
> 用本地 `init`，它有 schema 校验和事故防护，手写容易写错。

---

## 6. 在 fork 里填 Secrets

进入**你的 fork** → **Settings** → 左侧 **Secrets and variables** → **Actions** →
**New repository secret**，逐个添加：

| Secret 名（**区分大小写，照抄**） | 值 | 必填 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 步骤 3 的 `sk-...`（用 DeepSeek 时）| 二选一 |
| `LLM_API_KEY` | 步骤 3 的 Key（用别的供应商时）| 二选一 |
| `EMAIL_SENDER` | 你的 Gmail 地址 | ✅ |
| `EMAIL_PASSWORD` | 步骤 4 的 16 位应用密码（**去空格**）| ✅ |
| `DIGEST_EMAIL_TO` | 收报告的邮箱 | ✅ |

> Secrets 一旦保存就**看不到原文**（GitHub 会在日志里自动打码），改错了删掉重建即可。
> 用非 DeepSeek 供应商时，再加一个 `LLM_BASE_URL`。

---

## 7. 启用并首次运行

1. 进**你的 fork** → **Actions** 标签页。
2. 第一次会让你确认启用 workflow（绿色按钮 **"I understand my workflows, go ahead and enable them"**）→ 点它。
3. 左侧 workflow 列表点 **`daily-report`**。
4. 右侧 **Run workflow** 下拉 → **Run workflow** 绿色按钮 —— **立即手动触发一次**，不用等到明早 10 点。
5. 刷新页面，点进正在跑的那次运行，看 **`报告` → 跑每日委员会报告** 这步的实时日志。

跑完（2–5 分钟）后：
- ✅ 你的收件箱应该收到一封委员会报告邮件。
- ✅ Actions 日志最后会打印 `{'status': 'success', ...}`。
- ✅ fork 里多一条 `github-actions[bot]` 的 commit（更新后的 `memory/`）。

之后**全自动**：每天北京时间 10:00 自跑一次，不用你管。

---

## 8. 排错（Troubleshooting）

### `no_target_assets`
日志出现 `{'status': 'skipped', 'reason': 'no_target_assets'}`、邮件没内容。
→ 你的 `strategy.md` 里没有 `target_assets`（onboarding「关注哪些资产」留空了）。
本地 `uv run python scripts/skill.py status` 确认持仓，用 GUI 或
`POST /api/strategy/asset` 加上关注资产，再 `git add -f memory/ && git commit && git push`。

### 邮件没收到 / SMTP 报错
日志里 `跑每日委员会报告` 这步报 `SMTPAuthenticationError` 或连接失败：
- `EMAIL_PASSWORD` 用成了 Gmail **登录密码** → 必须用[应用专用密码](#4-拿-gmail-应用专用密码)。
- 应用密码**没去掉空格** → 去空格重存。
- Google 账号**没开两步验证** → 开了才能生成应用密码。
- 也检查垃圾邮件箱 / `DIGEST_EMAIL_TO` 有没有拼错。

### LLM 报 401 / 余额不足
`DEEPSEEK_API_KEY`（或 `LLM_API_KEY`）填错，或供应商账户没钱 → 重新生成 Key 重存、充值。

### `memory/` 没被 push 回来
`持久化 memory/ 回 fork` 这步报权限错 → 确认 workflow 里 `permissions: contents: write`
存在（本仓自带，别删）。fork 太老导致 Actions 被禁也会这样，进 Actions 重新启用。

### 时间不对（没在 10 点跑）
workflow 用的是 `cron: "0 2 * * *"` —— GitHub cron **只认 UTC**，`02:00 UTC = 北京 10:00`。
想改时间见[下一节](#9-自定义)。另外 GitHub 的 scheduled 触发**经常延迟几分钟到几十分钟**，属正常。

### 仓库长期没动，定时任务自己停了
GitHub 会在仓库 **60 天无活动**后自动禁用 scheduled workflow。本方案每天 commit 一次
`memory/` 正好让仓库保持活跃，通常不会触发；万一停了，进 Actions 点一下重新启用即可。

---

## 9. 自定义

**改运行时间**：编辑 `.github/workflows/daily-report.yml` 的 `cron`。记住是 UTC。
例：想北京 08:00 跑 → `0 0 * * *`；想每个工作日跑 → `0 2 * * 1-5`。
[crontab.guru](https://crontab.guru) 可以帮你算。

**改分析哪些资产**：改本地 `memory/strategy.md` 的 `target_assets`（或用 GUI / CLI），
然后 `git add -f memory/ && git commit && git push`。下次运行就生效。

**临时停用**：Actions → `daily-report` → 右上 **`···` → Disable workflow**。想恢复再 Enable。
彻底不用就删掉 `.github/workflows/daily-report.yml`。

**手动随时跑一次**：Actions → `daily-report` → **Run workflow**（`daily_report` 是只读出报告，
重跑安全，最多重发一封邮件，不会重复记账）。

---

## 10. 安全与隐私须知

- **fork 必须 private** —— `memory/` 含真实持仓，会进 git 历史。再强调一次。
- **Secrets 不会泄露**：GitHub 加密存储，运行日志里自动打码，commit 回来的 `memory/` 里
  **不含** API Key / 邮箱密码（那些只在 `.env`，`.env` 在 `.gitignore` 里，不会被 commit）。
- **push 用的是 `GITHUB_TOKEN`**（GitHub 自动注入的临时令牌，只对**这个仓库**有写权限，
  运行结束即失效），不需要你额外配 PAT。
- 想多人/多设备共享一份数据、而不是各自 fork？那是另一套架构（hub-and-spoke 远端模式），
  见 [skills/invest/SKILL.md「远端模式」](../skills/invest/SKILL.md)。

---

完事。从此每天早上 10 点，一封 4 角色投资委员会的报告自己躺进你邮箱 —— 零服务器，零运维。
