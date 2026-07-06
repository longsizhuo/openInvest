---
name: invest
version: 0.18.0 # x-release-please-version
description: openInvest 多资产 AI 投资委员会 **日常使用**。读取持仓 / 实时行情 / 策略 / 历史决议 / 加减仓 / 跑 4 角色 LLM 委员会给投资 verdict。支持任意 yfinance symbol（A 股 / 港股 / 美股 / ETF / 加密 / 商品）和任意币种。**两条路径**：(1) Coordinator — Claude Code spawn 4 个 subagent，省 DeepSeek token；(2) Direct — 任何 agent（Cursor / Cline / Codex / 普通脚本）跑 `run.sh run_committee <SYM>` 一键拿 verdict。**触发场景**："show portfolio / 看看我的持仓"、"我现在涨了多少 / how is my P&L"、"该不该买/卖 X / should I buy X"、"分析一下 X / analyze X"、"跑委员会 / run committee on X"、"track AAPL / 跟踪苹果"、"加仓 / 减仓 / 记一笔交易"。**首次安装走另一个 skill `invest-setup`**（doctor 返回 needs_setup 时切过去）。后端 longsizhuo/openInvest。
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, portfolio, committee, stocks, gold, etf, crypto, 投资, 持仓, 委员会, 行情]
---

# Invest Skill

**新手 fork 用户**：先跑 `invest-setup` skill 初始化。主流程走本 skill（AI agent 调 CLI/MCP 看持仓 / 跑委员会 / 查决策回放）。
Web GUI 已退役（2026-07）——所有能力经 CLI 子命令 / MCP 工具暴露。
后端从 PyPI 分发（uvx 按需拉），更新跑 `run.sh update` 即可。

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
| `live_prices` / `correlate` | 仍**本地**跑（纯 yfinance，不碰数据）|
| `run_committee` | 在 **hub** 上跑（DeepSeek key 在 hub），CLI 自动轮询到完成；同日 cache 用 hub 日期口径 |
| `prepare/save_committee` | 经 hub RPC——Coordinator 协议（spawn 4 subagent）**完全不变** |
| `buy/sell/deposit/...` 写操作 | 落 hub 账本（history `source: skill_remote`）|
| `event_check` | 转发 hub 手动扫描；`--live` / `--recall` 禁用（hub cron 已覆盖）|

**纪律**：远端模式下本机没有 `memory/`，更不存在"直接读写 memory 文件"——
一切经 `run.sh` 或 hub API。下面 Web API 表里的 `:8765` 在远端模式下替换为
`$INVEST_API_BASE`，curl 时带 `Authorization: Bearer $INVEST_API_TOKEN`。

## 工具怎么查（MCP 优先，长尾看 references/tools.md）

**MCP 用户**（plugin 装完自动注册，Claude Code / Codex 同）：status / strategy /
history / live_prices / what_if / discipline / decisions / explain_decision /
record_execution / buy / sell / deposit / withdraw / run_committee 共 14 个工具，
schema 自动发现，直接调，不需要查表。

**CLI/REST agent** 或 MCP 没覆盖的长尾操作（trades 意向流 / config 白名单 /
events / holdings import / gold 专用端点）→ 读 `references/tools.md`（完整
子命令表 + 端点表）。完整 OpenAPI：`http://127.0.0.1:8765/openapi.json`。

**子命令名是封闭集合**——表之外的命令都不存在。想调 `get_committee_context` /
`analyze_asset` 这种名字时，停下对照表——你大概率在脑补，应选 `prepare_committee`
或 `run_committee`。输出都是 JSON，**始终从 JSON 引用数字**，不从 memory/*.md 读。

### 从券商 App **截图**导入持仓（你来 OCR，后端零依赖）

用户发券商持仓**截图**时：**你自己读图**（你有视觉能力），把每行转成
`symbol/数量/成本/币种/渠道` 的文字，再走 `import --text "..."`（或 POST
/api/holdings/import）——后端 LLM 只解析文字，不收图。先不带 `--commit` 预览，
用户核对后再 `--commit` 非破坏写入。

## 新闻投喂（你的搜索 > 任何爬虫）

你有比后端爬虫强得多的搜索能力（含中文源）。**浏览/搜索中看到与用户持仓相关的
财经新闻时，主动调 `ingest_event` 喂进事件账本**（MCP 工具或 CLI `ingest_event
--title --url [--snippet --source]`）——后端负责归一化/判级/去重/RAG 召回，
重发同一条不会重复入账。尤其 A 股/区域市场新闻：那是爬虫盲区，你是唯一来源。
若宿主装有行情/新闻类 skill（如 Longbridge），其新闻同样值得喂——账本只认信息不认出身。

## 决策闭环 workflow（Decision Review + Reflection）

openInvest 记账，**你负责采集**——这是宿主 agent 的本职（issue #133 Decision 2）。

**用户问"为什么今天 HOLD / 为什么让我卖"**（Decision Review）：
1. `explain_decision <decision_id>` 拿完整 4 角色辩论 transcript + CIO memo + 路径快照
2. 结合 `status`（当前持仓）+ 必要时 `GET /api/user`（wealth_context）
3. 用 transcript 里的证据回答，别自己编理由

**用户对建议表态"我没买 / 我买了 / 我不同意"**（Reflection）：
1. 先问一句原因（估值太高？资金不足？不同意委员会？忘了？）——别跳过，
   原因是系统唯一拿不到的信息
2. `record_execution <decision_id> [--rejected] --reason "..."` 回写（幂等，随时改口）
3. 用户真的成交了 → 引导记 trade（7 天内同标的同向成交也会被自动匹配兜底）

**用户问"我听了几次建议 / 委员会靠谱吗"**：
`decisions --days 90` → 采纳率 + 每条决议的 决议↔干预↔执行↔结果 全链；
配 `discipline` 看规则拦截的反事实损益。连续多次拒绝同一类建议时，
主动指出"你和委员会的分歧模式"——这不是坏事，是值得记录的信号。

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
- **永远不直接写 `memory/`**。所有状态变更走 CLI 子命令 / MCP 工具（atomic write +
  fcntl 锁 + 审计 trail）。直接编辑会导致 schema drift + 并发写损坏。
- **同一资产同一天不重复跑委员会**——`run_committee` 默认会读 cache；
  Coordinator 路径要先 `ls memory/.committee/<today>/<SYM>.md` 检查。
- **不要编 CIO confidence**。worker 之间分歧严重时老实写 `confidence: 0.4-0.5`。
- **不要泄露用户的 email 等个人身份信息**，永远不在输出里写死。

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
| `references/tools.md` | 完整子命令表 + Web API 端点表（MCP 没覆盖的长尾操作）|
| `references/committee-protocol.md` | Coordinator 路径跑委员会（Claude Code 专用）|
| `references/two-paths.md` | 想懂 Coordinator vs Direct 区别 / DeepSeek cron 触发 |
| `references/adding-assets.md` | 用户想跟踪新 symbol |
| `references/troubleshooting.md` | doctor 全绿但还是出错 |
| `references/onboarding.md` | **首次安装去 `invest-setup` skill**；此文件保留作详细参考 |

更深的架构上下文看项目 Wiki：
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
