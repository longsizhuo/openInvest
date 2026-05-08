---
name: invest
version: 0.7.0
description: openInvest 多资产 AI 投资委员会。读取持仓 / 实时行情 / 策略 / 历史决议；调 4 角色 LLM 委员会（Macro + Quant + Risk Officer + CIO）做多轮 cross-challenge 辩论。支持任意 yfinance symbol（A 股 / 港股 / 美股 / ETF / 加密 / 商品）和任意币种（CNY / AUD / USD / ...）。后端自带 Web GUI，首次调用自动 git clone + uv sync + 拉前端 dist。**触发场景**：用户问 "show portfolio / 看看我的持仓"、"我现在涨了多少 / how is my P&L"、"该不该买/卖 X / should I buy X"、"分析一下 X / analyze X"、"跑委员会 / run committee on X"、"track AAPL / 跟踪苹果"、"现在 NDQ 多少钱 / current NDQ price"。后端仓库 longsizhuo/openInvest，前端仓库 longsizhuo/invest-gui。
---

# Invest Skill

openInvest 多资产 AI 投资委员会。Skill 模式下**你（Claude）扮演 coordinator**：
通过 `Agent({...})` 工具召唤 4 个 specialist subagent，最后自己合成 CIO memo。
成本走用户的 Claude 订阅，**项目不烧 API token**。

## 决策树

> 下面 `run.sh` 是 `~/.claude/skills/invest/scripts/run.sh` 的简写。
> 实际调用时写完整路径。

每次用户请求都是这 3 步：

```
1. 跑 `run.sh doctor`                                       ← 必跑第一步
   ├─ status: "ready"        → 进 step 2
   └─ status: "needs_setup"  → 读 references/onboarding.md，问用户 6 个问题

2. 按用户意图选子命令：

   "看持仓 / 我现在多少钱"          → run.sh status
   "我的策略是什么"                  → run.sh strategy
   "最近交易 / 流水"                 → run.sh history
   "现在 VIX / NDQ 多少"             → run.sh live_prices
   "如果黄金跌 5% 我亏多少"          → run.sh what_if --gold-pct -5
   "该不该买/卖 X / 分析一下 X"      → 委员会协议 ↓
   "跟踪 AAPL / 我想看看 TSLA"       → 见 references/adding-assets.md

3. 执行环节：告诉用户 NapCat 命令。**永远不要**直接写 memory/（见 Constraints）。
```

## 提醒用户 Web GUI 存在

`doctor` 输出里 `web_gui` check 的 `hint` 字段会告诉你 GUI 是否在跑。

- 如果 `gui_running: true`：在你回答的末尾顺带一句：
  > "顺便：Web GUI 在 http://127.0.0.1:8765 跑着，可以打开看完整面板（持仓 / 委员会直播 / 历史决议 / LLM 用量）。"

- 如果 `gui_dist_ready: true` 但 `gui_running: false`：用户**有可能**还不知道 GUI 存在。
  在第一次回答时提一次：
  > "顺便：项目自带 Web GUI，想用的话另开终端跑 `~/.claude/skills/invest/scripts/run.sh gui`，
  > 然后浏览器开 http://127.0.0.1:8765。"

  之后同一会话不要重复提（一次就够，避免 noise）。

- 如果 `gui_dist_ready: false`：bootstrap 时应该已经自动装了。如果没装上，是网络
  问题，告诉用户手动跑 `cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist`。

## 委员会协议（用户问"该不该买/卖 X"时走这条）

不要按这里的简介执行——读 `references/committee-protocol.md` 严格按它走。
那里覆盖完整 6 个 stage（Stage 0 同日检查 → Stage 5 CIO 综合 → Stage 6 落盘）+
精确的 `Agent({...})` payload。

**关键警告**：`prepare_committee` 输出的 brief 里有 `regime_brief` 字段，**必须**原样
传给 Quant Round 1 / Round 2 worker，否则 REGIME 硬约束会失效，Quant 在震荡市
底部会乱喊 bearish。

## 子命令一览（只读，不调 LLM）

| 命令 | 用在 | 返回 |
|------|------|------|
| `doctor` | 必跑第一步 | JSON，`status: "ready"` 或 `"needs_setup"` |
| `status` | 看持仓 | 现金 + holdings + 实时价 + P&L |
| `strategy` | 看策略 | target_assets + Dreaming insights |
| `history [-n N]` | 看流水 | 最近 N 笔交易 + 委员会决议 |
| `live_prices` | 背景行情 | VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F |
| `what_if [...]` | "X 跌 Y% 我亏多少" | 算术情景，无 LLM |
| `gui` | 启动 Web GUI | uvicorn 在 :8765，Ctrl+C 退出 |

输出都是 JSON。**始终从 JSON 引用数字**，不从 `memory/*.md` markdown 读
（markdown body 是 frontmatter 的渲染产物，可能略滞后）。

## Constraints（守好别破坏）

- **不要主动跑 `daily_report` cron**——除非用户明说 "跑深度分析" / "run full report"。
  那条路烧 DeepSeek token。
- **不要编实时价**。永远走 `run.sh status` 或 `live_prices`。yfinance 可能返回
  陈旧数据，注意 `is_stale` flag。
- **永远不直接写 `memory/`**。所有状态变更走 NapCat 或 Web API（atomic write +
  fcntl 锁 + 审计 trail）。直接编辑会导致 schema drift + 并发写损坏。
- **同一资产同一天不重复跑委员会**——先看 `memory/.committee/<today>/<asset>.md`
  存不存在，存在就直接复用。
- **不要编 CIO confidence**。worker 之间分歧严重时老实写 `confidence: 0.4-0.5`，
  别假装 0.85。
- **不要泄露用户的 QQ / email**。NapCat 白名单是 per-user env var
  （`INVEST_WHITELIST_QQ`），永远不在输出里写死。

## 双执行路径（Skill vs Web/Cron）

你现在在 **Skill** 路径。同一套 prompt 还有 **Web/Cron** 路径用 DeepSeek-Chat 跑。
两边 verdict **可能不同**（不同模型，cross-validation 用）。**不要主动调 Web 路径**——
只在用户问 "无人值守 cron" / "GUI 直播" 时提一下它存在。

详见 `references/two-paths.md`。

## 出问题先看哪

仔细读 `doctor` JSON 输出。每一个 check 都有 `hint` 字段告诉你怎么修。
如果 doctor 全绿但子命令还出错，读 `references/troubleshooting.md`。

## References 索引

| 文件 | 何时读 |
|------|--------|
| `references/onboarding.md` | doctor 返回 `needs_setup` |
| `references/committee-protocol.md` | 用户问 "该不该买/卖 X" / "分析一下 X" |
| `references/two-paths.md` | 用户问 cron / GUI / DeepSeek 相关 |
| `references/adding-assets.md` | 用户想跟踪新 symbol |
| `references/troubleshooting.md` | doctor 全绿但还是出错 |

更深的架构上下文看项目 Wiki：
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
