---
name: invest
version: 0.8.0
description: openInvest 多资产 AI 投资委员会。读取持仓 / 实时行情 / 策略 / 历史决议；用 4 角色 LLM（Macro + Quant + Risk Officer + CIO）做多轮 cross-challenge 辩论给出投资 verdict。支持任意 yfinance symbol（A 股 / 港股 / 美股 / ETF / 加密 / 商品）和任意币种（CNY / AUD / USD / ...）。**两条路径**：(1) Coordinator — Claude Code spawn 4 个 subagent，省 DeepSeek token；(2) Direct — 任何 agent（Cursor / Cline / Codex / DeepSeek-based / 普通脚本）跑 `run.sh run_committee <SYM>` 一键拿 verdict。**触发场景**：用户问 "show portfolio / 看看我的持仓"、"我现在涨了多少 / how is my P&L"、"该不该买/卖 X / should I buy X"、"分析一下 X / analyze X"、"跑委员会 / run committee on X"、"track AAPL / 跟踪苹果"、"现在 NDQ 多少钱 / current NDQ price"。后端 longsizhuo/openInvest，前端 longsizhuo/invest-gui。
---

# Invest Skill

openInvest 多资产 AI 投资委员会。**这个 skill 不是 Claude 专属**——任何能跑
shell 命令的 agent 都能用，看下面 "选路径"。

## 选路径

| 你是谁 | 走哪条路 | 跑什么 | 凭据 |
|--------|----------|--------|------|
| Claude Code（有 `Agent({...})` 工具）| **Coordinator** | `prepare_committee` → spawn 4 subagent → `save_committee` | 不需要 DeepSeek key |
| 任何其他 agent（Cursor / Cline / Codex / DeepSeek 本地 / 普通 Python）| **Direct** | `run_committee <SYMBOL>` 一键 | 需要 `DEEPSEEK_API_KEY` |

两条路径**底座一样**——同一份 prompt，同一份 REGIME 硬约束，同一份落盘格式
（`memory/.committee/<date>/<asset>.md`）。区别只在"4 个 LLM 角色谁来扮演"：
Coordinator 由 Claude（用户订阅）扮演，Direct 由 DeepSeek-Chat（按 token 计）扮演。
verdict 可能不同（不同模型，cross-validation 用）。

## 决策树（不论哪条路径，前面都一样）

```
1. 跑 `run.sh doctor`                                ← 必跑第一步
   ├─ status: "ready"        → 进 step 2
   └─ status: "needs_setup"  → 读 references/onboarding.md，问用户 5 个问题

2. 按用户意图选子命令：

   "看持仓 / 我现在多少钱"          → run.sh status
   "我的策略是什么"                  → run.sh strategy
   "最近交易 / 流水"                 → run.sh history
   "现在 VIX / NDQ 多少"             → run.sh live_prices
   "如果黄金跌 5% 我亏多少"          → run.sh what_if --gold-pct -5
   "该不该买/卖 X / 分析一下 X"      → 委员会协议 ↓
   "跟踪 AAPL / 我想看看 TSLA"       → 见 references/adding-assets.md

3. 委员会按你的路径走：
   - Coordinator → 读 references/committee-protocol.md（spawn 4 subagent）
   - Direct      → 直接 `run.sh run_committee <SYMBOL>` 拿 JSON verdict

4. 执行环节：告诉用户 NapCat 命令。**永远不要**直接写 memory/（见 Constraints）。
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

**关键警告**：`prepare_committee` 输出的 `regime_brief` **必须**原样
传给 Quant Round 1 / Round 2 worker，否则 REGIME 硬约束会失效，Quant 在
震荡市底部会乱喊 bearish。

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

## 提醒用户 Web GUI 存在

`doctor` 输出里 `web_gui` check 的 `hint` 字段会告诉你 GUI 是否在跑。

- 如果 `gui_running: true`：在你回答的末尾顺带一句：
  > "顺便：Web GUI 在 http://127.0.0.1:8765 跑着，可以打开看完整面板（持仓 / 委员会直播 / 历史决议 / LLM 用量）。"

- 如果 `gui_dist_ready: true` 但 `gui_running: false`：用户**有可能**还不知道 GUI 存在。
  在第一次回答时提一次：
  > "顺便：项目自带 Web GUI，想用的话另开终端跑 `~/.claude/skills/invest/scripts/run.sh gui`，
  > 然后浏览器开 http://127.0.0.1:8765。"

  之后同一会话不要重复提。

- 如果 `gui_dist_ready: false`：bootstrap 时应该已经自动装了。如果没装上，是网络
  问题，告诉用户手动跑 `cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist`。

## 子命令一览

| 命令 | 路径 | 用在 | 返回 |
|------|------|------|------|
| `doctor` | 通用 | 必跑第一步 | JSON，`status: "ready"` 或 `"needs_setup"` |
| `init [--from-stdin] [--force]` | 通用 | 第一次配 / 重配 | JSON，`holdings_parse_note` 显示自然语言解析结果 |
| `status` | 通用 | 看持仓 | 现金 + holdings + 实时价 + P&L |
| `strategy` | 通用 | 看策略 | target_assets + Dreaming insights |
| `history [-n N]` | 通用 | 看流水 | 最近 N 笔交易 + 委员会决议 |
| `live_prices` | 通用 | 背景行情 | VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F |
| `what_if [...]` | 通用 | "X 跌 Y% 我亏多少" | 算术情景，无 LLM |
| `prepare_committee SYM` | Coordinator | 拿 brief 给 4 subagent | brief JSON + 6 段 prompts |
| `save_committee SYM` | Coordinator | 落盘 transcript | stdin 4 段输出 → markdown |
| `run_committee SYM [--force]` | Direct | 一键完整委员会 | verdict JSON + CIO memo |
| `gui` | 通用 | 启动 Web GUI | uvicorn :8765，Ctrl+C 退出 |

输出都是 JSON。**始终从 JSON 引用数字**，不从 `memory/*.md` markdown 读
（markdown body 是 frontmatter 的渲染产物，可能略滞后）。

## Constraints（守好别破坏）

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
| `references/onboarding.md` | doctor 返回 `needs_setup` |
| `references/committee-protocol.md` | Coordinator 路径跑委员会（Claude Code 专用）|
| `references/two-paths.md` | 想懂 Coordinator vs Direct 区别 / DeepSeek cron 触发 |
| `references/adding-assets.md` | 用户想跟踪新 symbol |
| `references/troubleshooting.md` | doctor 全绿但还是出错 |

更深的架构上下文看项目 Wiki：
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
