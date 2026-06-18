---
type: adr
title: "ADR-016: 账本写入幂等性（可重放触发必须有原子幂等闸）"
tags: [idempotency, ledger, portfolio, state-machine, adr]
intent: 账本写入幂等性规则
documents:
  endpoints:
    - POST /api/holdings
    - PUT /api/holdings/{symbol}
    - DELETE /api/holdings/{symbol}
    - POST /api/deposit
    - POST /api/withdraw
    - POST /api/gold/buy
    - POST /api/gold/sell
    - POST /api/trades/record
    - PATCH /api/trades/{trade_id}/status
    - POST /api/skill/buy
    - POST /api/skill/sell
  config_keys: []
  symbols:
    - state_claim
    - state_unclaim
    - record_external_trade
    - _sync_trade_to_portfolio
status: proposed
date: "2026-06-16"
supersedes: []
superseded_by: []
---

# ADR-016: 账本写入幂等性（可重放触发必须有原子幂等闸）

**状态**: 提议中
**日期**: 2026-06-16
**作者**: Code Review

---

## 背景

"钱"散在两个 gitignore 的 store：`memory/portfolio.md`（cash + holdings）+
`db/trades.db`（交易账本）。任何**把一笔逻辑事件应用进账本**的写路径，如果该事件
**可被重放/重触发**（HTTP 重试、邮件轮询、cron 重跑、agent 重发、双击），而代码用
"累加"语义（`cur_units + units`、`cash -= amount`）且**没有按事件去重**，就会重复入账——
记的是真金白银。

**这一类 bug 已经咬了两次：**

| # | 路径 | 触发 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | `PortfolioManager.record_external_trade`（CommSec 邮件导入）| 邮件轮询重跑 / 并发 `commsec_apply` | `processed_emails` 在 portfolio-tx 外标记，check→mark 有 TOCTOU | #62：动账本前原子 `state_claim(email_id)`，失败 `state_unclaim` 重试 |
| 2 | `web_api patch_trade_status → _sync_trade_to_portfolio`（web/GUI/agent 手动 PATCH executed）| 双击 / 客户端超时重试 / agent 重发 | endpoint 只看目标 status、不看当前 status；`_sync` 是累加的，docstring 却谎称"幂等" | 本 PR：用 `trade_before["status"]` 做转移判定，已 executed 再 PATCH executed 直接幂等返回 |

#1 修完，2026-06-16 全量审计所有账本写路径，又揪出第三处（payday TOCTOU）。本 ADR 把
**这一类不变量**正式落档，并附完整审计表，避免第四次。

## 决策

### 规则：可重放的账本写入，必须有"按逻辑事件去重"的幂等保证

一个写路径只要满足"**累加语义 + 触发可重放**"，就**必须**具备下列之一：

1. **原子幂等闸**——动 cash/holdings *之前* 用 `MemoryStore.state_claim(name, key)`
   原子 claim 该事件的去重键（email_id / `YYYY-MM` / trade 状态跃迁）；claim 失败=已处理=早退；
   应用失败再 `state_unclaim` 让下次能重试。（check+append 同一把 fcntl 锁，杜绝 TOCTOU。）
2. **状态机守卫**——应用前读当前持久状态，只在"真实状态跃迁"时应用一次
   （`patch_trade_status`：仅非-executed → executed 才同步）。
3. **SET / reject-duplicate 语义**——写成幂等的覆盖（`PUT holdings` = SET）或拒绝重复
   （`POST holdings` symbol 已存在 → 409），天然安全，无需去重键。

**`docstring` 不许谎称"幂等"**——除非上面三条之一在代码里真的存在。累加函数本身永远不幂等。

### 残余窗口（接受的权衡）

原子 claim 写盘后、portfolio-tx 提交前若被 SIGKILL/OOM → 键已 claim 但账本未改，该事件被
静默跳过，需人工 `state_unclaim` 重放。这比"崩在提交之后 = 双重记账真金白银"好，故接受。
（见 `record_external_trade` 注释。）

## 2026-06-16 审计结果（全部账本写路径）

| 路径 | 触发 | 语义 | 幂等保证 | 结论 |
|---|---|---|---|---|
| `record_external_trade` | 邮件轮询 / `commsec_apply` | 累加 | `state_claim(email_id)` | ✅ #62 |
| `commsec_apply` 端点 | HTTP POST | — | 透传上面 | ✅ |
| `patch_trade_status → _sync_trade_to_portfolio` | PATCH 重试 / 双击 | 累加 | 状态机守卫（本 PR）| ✅ 本 PR |
| `payday_check.run → add_income` | cron + 手动 `/payday` 并发 | 累加 | 原子 `state_claim(YYYY-MM)`（本 PR）| ✅ 本 PR |
| `POST /api/holdings` | HTTP POST | append | symbol 已存在 → 409 | ✅ |
| `PUT /api/holdings/{symbol}` | HTTP PUT | SET | 覆盖 | ✅ |
| `DELETE /api/holdings/{symbol}` | HTTP DELETE | remove | 第二次 → 404 | ✅ |
| napcat `/deposit /withdraw /gold*` | QQ 私聊 | 累加 | 单 WS 连接内事件串行处理、无重放 | ✅（直接用户动作）|
| `deposit_cash / withdraw_cash / buy / sell / delete_holding` | CLI / `/api/skill/*` / `/api/deposit` 等 | 累加 | 无去重键 | ⚠ 见下"未决" |
| `db.trades_db.record_trade` | `/api/trades/record` 重试 | INSERT | 无去重键 | ⚠ agent 重试产生重复 *planned* 行，但每行各执行一次（apply 端幂等已守）|

## 未决（systemic，需用户拍板，不在本 PR 范围）

**所有"累加型 POST 写端点"没有幂等键**（`/api/deposit` `/api/withdraw` `/api/gold/buy|sell`
`/api/skill/buy|sell`）。本地同进程 GUI/CLI 下，单次点击=单次意图，问题不大；但
**hub-and-spoke 远端模式（`INVEST_API_BASE`）下，HTTP 客户端超时重发会双重应用**——这是
REST "POST 非幂等"的通病。彻底关闭需要给写端点引入 `Idempotency-Key` header 方案
（客户端生成 UUID，服务端按 key 去重 N 分钟）。这是设计决策，留给用户决定是否做、何时做。

`record_trade` 的重复 *planned* 行同理可加 `(verdict_id, symbol, intended_date)` 唯一约束，
但优先级低（每行各执行一次，真正的钱由 apply 端幂等守住）。

## 不做的事

- **不合并两条 sync 路径**：`record_external_trade`（CommSec=真实已结算，sold-无持仓仍记 cash）
  与 `_sync_trade_to_portfolio`（web 手工，sold-无持仓 `_SkipSync` 回滚）语义**有意不同**，
  勿抽成共用底层函数。见两处注释。
- **本 PR 不做** `executed → cancelled` 的反向冲销（撤销已执行单子时回滚 cash/holdings）——
  独立改动，单独 PR。
- **本 PR 不引入** `Idempotency-Key` 端点方案（见"未决"）。

## 参考

- CLAUDE.md "公开数据红线 #4"（账本一致性）——已链接本 ADR
- #62 `fix(portfolio): make CommSec record_external_trade idempotent`
- `core/memory_store.py:state_claim / state_unclaim`（原子去重原语）
- `tests/test_trades_portfolio_sync.py::TestPatchStatusIdempotency`（#1 回归守卫）
