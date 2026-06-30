---
name: invest
version: 0.14.0 # x-release-please-version
description: openInvest 多资产 AI 投资委员会 **日常使用**。读取持仓 / 实时行情 / 策略 / 历史决议 / 加减仓 / 跑 4 角色 LLM 委员会给投资 verdict。支持任意 yfinance symbol（A 股 / 港股 / 美股 / ETF / 加密 / 商品）和任意币种。**两条路径**：(1) Coordinator — Claude Code spawn 4 个 subagent，省 DeepSeek token；(2) Direct — 任何 agent（Cursor / Cline / Codex / 普通脚本）跑 `run.sh run_committee <SYM>` 一键拿 verdict。**触发场景**："show portfolio / 看看我的持仓"、"我现在涨了多少 / how is my P&L"、"该不该买/卖 X / should I buy X"、"分析一下 X / analyze X"、"跑委员会 / run committee on X"、"track AAPL / 跟踪苹果"、"加仓 / 减仓 / 记一笔交易"。**首次安装走另一个 skill `invest-setup`**（doctor 返回 needs_setup 时切过去）。后端 longsizhuo/openInvest，前端 longsizhuo/invest-gui。
---

# Invest Skill

**新手 fork 用户**：先跑 `invest-setup` skill 初始化。Web GUI 是 beta，
主流程走本 skill（通过 AI agent 调 CLI 看持仓 / 跑委员会 / 查决策回放）。
代码迭代频繁，定期 `cd ~/openInvest && git pull` 拉最新。

openInvest 多资产 AI 投资委员会。**这个 skill 不是 Claude 专属**——任何能跑
shell 命令的 agent 都能用，看下面 "选路径"。

## 选路径

| 你是谁 | 走哪条路 | 跑什么 | 凭据 |
|--------|----------|--------|------|
| Claude Code（有 `Agent({...})` 工具）| **Coordinator** | `prepare_committee` → spawn 4 subagent → `save_committee` | 不需要 DeepSeek key |
| 任何其他 agent（Cursor / Cline / Codex / DeepSeek 本地 / 普通 Python）| **Direct** | `run_committee <SYMBOL>` 一键 | 需要 `DEEPSEEK_API_KEY` |

两条路径**底座一样**——同一份 prompt，同一份数据准备（regime 分类 + 概率口径 +
确定性事实块），同一份落盘格式
（`memory/.committee/<date>/<asset>.md`）。区别只在"4 个 LLM 角色谁来扮演"：
Coordinator 由 Claude（用户订阅）扮演，Direct 由 DeepSeek-Chat（按 token 计）扮演。
verdict 可能不同（不同模型，cross-validation 用）。

## 决策树（不论哪条路径，前面都一样）

```
1. 跑 `run.sh doctor`                                ← 必跑第一步
   ├─ status: "ready"        → 进 step 2（继续用本 skill）
   └─ status: "needs_setup"  → **切到 `invest-setup` skill**（不在本 skill 处理）

2. 按用户意图选子命令：

   "看持仓 / 我现在多少钱"          → run.sh status
   "分析战况 / 风险 / 集中度"       → run.sh status + **`curl /api/user`** 拿
                                       wealth_context（**必读**，避免 PWM 老逻辑误判）
   "我的策略是什么"                  → run.sh strategy
   "最近交易 / 流水"                 → run.sh history
   "现在大盘 / VIX 多少"             → run.sh live_prices
   "如果 X 跌 5% 我亏多少"           → run.sh what_if --symbol X --pct -5
                                       （X 是用户持仓里的 yfinance symbol；
                                        --gold-pct / --ndq-pct 兼容旧用法）
   "该不该买/卖 X / 分析一下 X"      → 委员会协议 ↓
   "跟踪 AAPL / 我想看看 TSLA"       → 见 references/adding-assets.md

3. 委员会按你的路径走：
   - Coordinator → 读 references/committee-protocol.md（spawn 4 subagent）
   - Direct      → 直接 `run.sh run_committee <SYMBOL>` 拿 JSON verdict

4. 拿到 verdict / cio_memo 后：
   - **`cio_memo` 是 Markdown 字符串**（包含 `# 标题 ## verdict` 等结构）。
     直接把它**作为 Markdown 渲染给用户看**，不要打印原始 JSON 让用户自己解析
   - 执行环节：检查 next_step 字段，按里面写的引导用户。**永远不要**直接写
     memory/（见 Constraints）
```

## Coordinator 路径详情（Claude Code 专用）

读 `references/committee-protocol.md` 严格按它走。完整 6 个 stage：

- Stage 0：同日检查（`memory/.committee/<today>/<asset>.md` 存在直接复用）
- Stage 1：`prepare_committee` 拿 brief
- Stage 2：Round 1 — 3 个 `Agent({...})` 并行（Macro + Quant + Risk）
- Stage 3：Round 2 — Cross-challenge（2 个 Agent）
- Stage 4（可选）：未收敛跑 Round 3+
- Stage 5：CIO 综合（**你**自己写，不 delegate）
- Stage 6：`save_committee` 落盘

**关键警告**：`prepare_committee` 输出的 `regime_brief` / `sentiment_brief` /
`valuation_brief` / `reentry_reference` **必须**按 instructions 原样粘进对应
worker 的 prompt：regime+估值+情绪进 Quant，三块+路径参考进 CIO。缺了的后果：
Quant 失去概率口径与防御哨兵背景；CIO 的 EXPECTED_PATH 凭空编；INDEP_DEFENSE_FLAG
不进 transcript → `save_committee` 的确定性防御降级（快崩哨兵）整条失效。

## Direct 路径详情（任意 agent）

```bash
# 一条命令搞定
~/.claude/skills/invest/scripts/run.sh run_committee NDQ.AX
```

输出 JSON：
```json
{
  "status": "ok",
  "asset": {...},
  "verdict": {"verdict": "ACCUMULATE", "confidence": 0.72, ...},
  "cio_memo": "<完整 CIO 备忘 markdown>",
  "transcript_path": "memory/.committee/2026-05-09/NDQ.AX.md",
  "next_step": "..."
}
```

参数：
- `--force`：今天已经跑过也重跑（默认读 cache 省 token）
- `--max-rounds N`：cross-challenge 轮数上限（默认 1）

**前置条件**：`.env` 有 `DEEPSEEK_API_KEY`。如果调用 agent 在用户机器上跑
但没有 key，提示用户先 `run.sh init` 把 key 配好。
（**远端模式例外**：key 在 hub 上，本机不需要——见下节。）

## 远端模式（hub-and-spoke，多设备共享一份数据）

`.env` 里设了 `INVEST_API_BASE` = 本机是**客户端**：所有子命令自动转发到远端
hub（另一台跑着 invest web_api 的机器），**本机没有也不该有 `memory/`**。
数据（持仓/策略/决议/prompt）全在 hub，改一处全设备生效。

客户端 `.env` 最小配置（不需要 DeepSeek key / Gmail / memory）：

```bash
INVEST_API_BASE=https://your-hub.example.com   # 或 http://10.0.0.x:8765
INVEST_API_TOKEN=<hub 的同名 token>             # hub 开了鉴权才需要
# 走 Cloudflare Tunnel + Access 的话改用这对：
# CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=...
```

行为差异（其余命令全部透明转发，**输出与本地同形状**，决策树照常走）：

| 命令 | 远端模式行为 |
|------|--------------|
| `doctor` | 返回 **hub 视角**检查 + 多一个 `remote` 段（api_base / 鉴权方式 / 连通性）|
| `init` | **禁用**（数据在 hub；连接 hub 只需上面两行 .env）。报错带 hint |
| `gui` | 不本机起 uvicorn，直接输出 hub 的 `gui_url`（浏览器开它即可）|
| `live_prices` / `correlate` | 仍**本地**跑（纯 yfinance，不碰数据）|
| `run_committee` | 在 **hub** 上跑（DeepSeek key 在 hub），CLI 自动轮询到完成；同日 cache 用 hub 日期口径 |
| `prepare/save_committee` | 经 hub RPC——Coordinator 协议（spawn 4 subagent）**完全不变** |
| `buy/sell/deposit/...` 写操作 | 落 hub 账本（history `source: skill_remote`）|
| `event_check` | 转发 hub 手动扫描；`--live` / `--recall` 禁用（hub cron 已覆盖）|

**纪律**：远端模式下本机没有 `memory/`，更不存在"直接读写 memory 文件"——
一切经 `run.sh` 或 hub API。下面 Web API 表里的 `:8765` 在远端模式下替换为
`$INVEST_API_BASE`，curl 时带 `Authorization: Bearer $INVEST_API_TOKEN`。

## Web GUI 是小白的主入口（**第一次回答必须提一句**）

GUI 是设计给**小白用户**的——CLI/skill 是给 agent 和极客的。如果用户没听说
GUI，他根本用不上。所以：

**你的职责**：第一次回答用户时**必须**附一句 GUI 入口（按 doctor 给的 hint），
之后同会话别复读。这不是推销，是引导用户看到他主面板该有的东西。

按 `doctor` 输出里 `web_gui` check 的状态分别处理：

| 状态 | 怎么处理 |
|------|----------|
| `gui_running: true` | 第一次回答末尾顺带："顺便：浏览器开 http://127.0.0.1:8765 可以看完整面板" |
| `gui_dist_ready: true` 但没在跑 | 第一次回答末尾："项目自带 Web GUI，想看持仓/委员会面板的话另开终端跑 `~/.claude/skills/invest/scripts/run.sh gui`" |
| `status: "missing"`（dist 没装）| **直接帮用户跑** `cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist`——这是 bootstrap 失败的信号，不是用户该解的问题 |

**例外**：用户在 `.env` 设了 `INVEST_GUI_NUDGE=off` → `hint: null`，这种用户
明确不想看 GUI 字眼，绝对不提。

**关键纪律**：
- 第一次回答提**一次**就够，同会话别复读"还可以打开 GUI 哦"——那是噪音
- 用户主动问"GUI/dashboard/网页"时直接给 URL，不需要先扯铺垫
- 用户说"我不需要 GUI" / "只用 CLI"时记住，之后别再提

## 子命令一览

| 命令 | 路径 | 用在 | 返回 |
|------|------|------|------|
| `doctor` | 通用 | 必跑第一步 | JSON，`status: "ready"` 或 `"needs_setup"` |
| `init [--from-stdin] [--force]` | 通用 | **不在本 skill 用**，首次安装走 `invest-setup` skill | — |
| `status` | 通用 | 看持仓 | 现金 + holdings + 实时价 + P&L |
| `strategy` | 通用 | 看策略 | target_assets + Dreaming insights |
| `history [-n N]` | 通用 | 看流水 | 最近 N 笔交易 + 委员会决议 |
| `live_prices` | 通用 | 背景行情 | VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F |
| `discipline` | 通用 | "委员会拦了什么/纪律如何" | 不作为率(HOLD 占比) + 拦截冲动操作次数 + 反事实省/费钱(只读零 LLM，对齐 ADR-023)。等价 `GET /api/discipline` |
| `what_if [--symbol X --pct N \| --gold-pct N \| --ndq-pct N]` | 通用 | "X 跌 Y% 我亏多少" | 算术情景，无 LLM |
| `correlate --symbols A,B[,C...] [--period 6mo] [--with-llm]` | "btw" 附带 | 用户**顺嘴问**"A 跟 B 像不像"（不写入 memory/.committee，纯查询返回）| pairwise 相关矩阵 + sector + macro 关联 |
| `prepare_committee SYM` | Coordinator | 拿 brief 给 4 subagent | brief JSON + 6 段 prompts |
| `save_committee SYM` | Coordinator | 落盘 transcript | stdin 4 段输出 → markdown |
| `run_committee SYM [--force]` | Direct | 一键完整委员会 | verdict JSON + CIO memo |
| `gui` | 通用 | 启动 Web GUI | uvicorn :8765，Ctrl+C 退出 |
| `deposit -c CCY -a N` | 通用 写 | 存入现金（任意币种） | JSON 新余额 |
| `withdraw -c CCY -a N` | 通用 写 | 取出现金，余额不足报错 | JSON 新余额 |
| `buy --symbol S --units N --price P [-c CCY] [--kind etf/equity/...]` | 通用 写 | 加仓 / 建仓（加权平均成本） | JSON action + 估算成本 |
| `sell --symbol S --units N --price P` | 通用 写 | 减仓（按 holding cost_currency 还现金） | JSON 剩余 units |
| `delete_holding --symbol S [--force]` | 通用 写 | 删除持仓行（units 必须 0 或 --force） | JSON 已删 |
| `import [--file F \| --text T] [--commit]` | 通用 读/写 | 自由文本/CSV 持仓描述 → LLM 解析成结构化持仓（券商持仓粘贴、批量录入）。默认只预览；`--commit` 非破坏写入（只加新 symbol、cash 只填当前为 0 的币种，重复导入幂等）。等价 POST /api/holdings/import | JSON `{parsed, committed, summary?}` |
| `config [--set KEY VALUE] [--clear KEY]` | 通用 读/写 | 读/改可经 API 配置的白名单参数（concentration_lens / **cash_opportunity_cost_rule**（机会成本规则，默认 OFF，ADR-024）/ risk_profile / gold_defense_dca / dreaming.llm_verify / **dca.auto_dca_enabled / dca.auto_dca_amount_cny**——自动定投开关与金额，ADR-018）。无参=读全部。等价 GET/PUT /api/config（ADR-017）| JSON 全部生效值 |

**子命令名是封闭集合 —— 上表之外的命令都不存在**。看到自己想调
`get_committee_context` / `analyze_asset` / `pull_brief` 这种名字时，停下，
回上表对照 —— 你大概率是在脑补不存在的命令名，应该选 `prepare_committee` 或
`run_committee`。

输出都是 JSON。**始终从 JSON 引用数字**，不从 `memory/*.md` markdown 读
（markdown body 是 frontmatter 的渲染产物，可能略滞后）。

### 从券商 App **截图**导入持仓（你来 OCR，后端零依赖）

用户发券商持仓**截图**时：**你自己读图**（你有视觉能力），把每行转成
`symbol/数量/成本/币种/渠道` 的文字，再走 `import --text "..."`（或 POST
/api/holdings/import）。后端 `import` 的 LLM 只解析**文字**——不要把图片塞给它
（DeepSeek/多数 chat 模型不收图，会报错）。即：截图 → 你转文字 → import，
比让后端 OCR 更准、且不挑后端模型。先 `--text`（不带 `--commit`）让用户核对预览，
确认后再 `--commit` 非破坏写入。

## Web API 写操作端点（agent 也能调）

**产品哲学**：agent（你）拥有 openInvest 全部功能。CLI 不够时，直接调 Web API
（默认 :8765）。GUI 是给小白用户的展示层，agent 不需要走 GUI。

用户说"记一笔交易"/"我打算买 X"/"标记成交"/"加新资产"时调这些：

| 端点 | 用在 | body 简例 |
|------|------|-----------|
| `GET /api/user` | **分析战况前必读**，拿 wealth_context（家族 backup / 账户性质 / 应急金）—— 决定怎么解释集中度 + 低现金 | — |
| `PUT /api/user/wealth_context` | 用户改家族 backup / 账户性质 / **月度补充额（开口池）**等（GUI 在 /settings 页填，agent 一般不调）| `{emergency_buffer_cny?, family_backup_available?, account_purpose?, lifestyle_notes?, monthly_contribution_cny?}` |
| `POST /api/trades/record` | **记一笔意向交易**（不连真实支付，只内部账本）| `{symbol, direction: "BUY"\|"SELL", units, price?, intended_date?, note?}` |
| `GET /api/trades?limit=N` | 看最近 N 笔意向 / 已成交 | — |
| `PATCH /api/trades/{id}/status` | **标记成交**（status: "executed"）→ 自动同步 portfolio.md（更新 holdings + 扣 cash）| `{status: "executed"}` |
| `POST /api/holdings` | 新增 yfinance 跟踪资产（不下单，只录入持仓数据）| `{symbol, kind, units, avg_cost, cost_currency, channel?}` |
| `POST /api/holdings/import` | 自由文本/CSV 持仓描述 → LLM 解析（GUI 粘贴券商持仓、批量录入）。`commit:false` 只预览不落盘；`commit:true` 非破坏写入（只加新 symbol、cash 只填当前为 0 的币种）。需后端 LLM key | `{content, commit?}` |
| `PUT /api/holdings/{symbol}` | 改持仓字段 | `{units?, avg_cost?, channel?}` |
| `POST /api/deposit` / `/api/withdraw` | 调 cash 现金 | `{currency: "CNY"\|"AUD"\|..., amount}` |
| `POST /api/gold/buy` / `/sell` | 黄金买卖（含 sell_fee 自动算）| `{grams, price_per_gram}` |
| `POST /api/strategy/asset` | 加 target_assets 条目 | `{symbol, channel?, max_single_invest_cny}` |
| `GET /api/events/recent?hours=24&min_severity=low&limit=50` | 列最近 N 小时事件层感知的新闻（ADR-006）。debug / "系统现在感知到什么" | — |
| `GET /api/discipline` | 委员会纪律台账：不作为率(HOLD 占比) + 拦截冲动操作次数 + 反事实损益（对齐 ADR-023，GUI/agent 展示"它拦了什么"）| — |
| `POST /api/events/check` | 手动跑一次 event_watch（拉新闻 + 归一化 + 入库 + 命中触发委员会）。同步 30-90s | — |
| `GET /api/config` | 看可经 API 配置的白名单参数当前生效值（+ 是否被 override + 元信息）| — |
| `PUT /api/config` | 改一条白名单 override（落盘持久、跨进程共读，优先级高于 env；ADR-017）| `{key, value}`，如 `{"key":"verdict.concentration_lens_enabled","value":false}` |
| `DELETE /api/config/{key}` | 删一条 override 回退默认 | — |

**典型流程**：用户说"我打算..."/"刚买了 X"/"我的持仓多了 Y" → 用
`POST /api/trades/record`（带 intended_date 区分计划 vs 已成交）→ 真实成交后用
`PATCH .../status executed`，后端会**自动更新 portfolio.md**（加权均价 + cash 扣减），
不需要你再调别的接口。

**完整 OpenAPI**：`http://127.0.0.1:8765/openapi.json` 查所有端点 + Pydantic schema。

## Constraints（守好别破坏）

- **分析持仓 / 集中度 / 风险前必读 `GET /api/user` 拿 wealth_context**——
  忘了这条就会犯 **2026-05-12 那个错**：用户填了家族 ¥4M backup，agent 跑完
  `status` 没看 user，按 PWM 老逻辑喊"60% 集中度超配 → 建议 TRIM 减仓"。错。
  正确做法：
  - 没填 wealth_context → 按 portfolio cash 判流动性 + 25-35% 集中度警戒
  - 填了 → 用 WealthContextOfficer 视角：
    * 集中度 % 算 `portfolio_value / (portfolio + emergency_buffer_cny)` 不是仅 portfolio
    * `family_backup_available=true` → 低 portfolio cash **不是** liquidity risk
    * `account_purpose="零花钱账户"` → 容忍较大回撤；`"退休金"` → 倾向减仓
  - 加仓金额上限**永远**= portfolio cash（**不能动 backup**），这条不变
- **不要主动跑 `daily_report` cron**——除非用户明说 "跑深度分析" / "run full report"。
  那条路烧 DeepSeek token。Direct 路径单资产 `run_committee` 就够。
- **不要编实时价**。永远走 `run.sh status` 或 `live_prices`。yfinance 可能返回
  陈旧数据，注意 `is_stale` flag。
- **永远不直接写 `memory/`**。所有状态变更走 NapCat 或 Web API（atomic write +
  fcntl 锁 + 审计 trail）。直接编辑会导致 schema drift + 并发写损坏。
- **同一资产同一天不重复跑委员会**——`run_committee` 默认会读 cache；
  Coordinator 路径要先 `ls memory/.committee/<today>/<SYM>.md` 检查。
- **不要编 CIO confidence**。worker 之间分歧严重时老实写 `confidence: 0.4-0.5`。
- **不要泄露用户的 QQ / email**。NapCat 白名单是 per-user env var
  （`INVEST_WHITELIST_QQ`），永远不在输出里写死。

## 出问题先看哪

仔细读 `doctor` JSON 输出。每一个 check 都有 `hint` 字段告诉你怎么修。
如果 doctor 全绿但子命令还出错，读 `references/troubleshooting.md`。

Direct 路径的常见错误：
- `error: DEEPSEEK_API_KEY 未设` → `.env` 没 key，跑 `run.sh init` 配
- `error: asset X not in strategy.target_assets` → 先把 X 加进 strategy，
  见 `references/adding-assets.md`

## References 索引

| 文件 | 何时读 |
|------|--------|
| `references/committee-protocol.md` | Coordinator 路径跑委员会（Claude Code 专用）|
| `references/two-paths.md` | 想懂 Coordinator vs Direct 区别 / DeepSeek cron 触发 |
| `references/adding-assets.md` | 用户想跟踪新 symbol |
| `references/troubleshooting.md` | doctor 全绿但还是出错 |
| `references/onboarding.md` | **首次安装去 `invest-setup` skill**；此文件保留作详细参考 |

更深的架构上下文看项目 Wiki：
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
