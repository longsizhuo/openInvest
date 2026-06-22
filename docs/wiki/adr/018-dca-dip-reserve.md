---
type: adr
title: ADR-018 — 自动定投与子弹池现金语义（DCA + dip-reserve）
status: accepted
date: 2026-06-23
tags: [portfolio, dca, cash, ledger, adr]
intent: 决策参数
schema_source:
  - core/portfolio_manager.py:PortfolioManager
  - core/config/tunable.py:DCAConfig
  - core/config/_loader.py:API_SETTABLE
  - jobs/dca_daily.py:run
documents:
  endpoints:
    - GET /api/config
    - PUT /api/config
  config_keys:
    - dca.auto_dca_enabled
    - dca.auto_dca_amount_cny
  symbols: []
supersedes: []
superseded_by: []
---

# ADR-018 — 自动定投与子弹池现金语义（DCA + dip-reserve）

**日期**: 2026-06-23
**状态**: Accepted
**关联**: [016-ledger-mutation-idempotency](016-ledger-mutation-idempotency.md) ·
        [017-config-via-api](017-config-via-api.md)

## 背景

用户用第三方平台（如京东金融）做**每日自动定投**，扣款来自工资 / 银行卡。但 openInvest
的 `buy()` 一律从 `portfolio.cash` 扣钱（账本一致：买 X 元 = 扣 X 元现金）。若把日定投
也按这套记账，会把 portfolio cash **错误消耗**——而 portfolio cash 的真实语义是"现在能立刻
拿去抄底的子弹（dip-reserve）"，不是日定投的资金来源。实测：¥30k 子弹按 ¥100/天定投扣，
约 10 个月归零，之后委员会逢跌想加仓却显示"没子弹"。

根因：`buy()` 把"资金来源"写死成"扣 portfolio cash"，无法表达"外部新钱进场建仓、子弹池
不动"这一情形。

附带发现的数据 bug：新加 `target_asset` 时取价/指标只触发 5d 增量刷新 → DB 仅 5 天历史 →
RSI/MA120/MA250/regime 全 N/A、`REGIME=unknown`（510500.SS / SPY 复现）。

## 决策

1. **`buy(source_type=...)` 资金来源参数**：
   - `cash_deduct`（默认）：扣 portfolio cash，现有行为不变；
   - `external_funding`：外部新钱建仓，**portfolio cash 不动**。`history` 记 `funding_source` 审计字段。
   - 由此明确 **portfolio cash 语义 = 抄底子弹池**：只被手动抄底 / 委员会加仓动用，不被日定投消耗。

2. **`DCAConfig` + `jobs/dca_daily`**：
   - 配置 `dca.auto_dca_enabled` / `auto_dca_amount_cny` / `auto_dca_symbols`，dataclass 默认**禁用**（安全模式）；
   - 经 `/api/config`（白名单，ADR-017）或 `INVEST_DCA_*` env 开启；
   - 每日对每个 symbol 用 `get_quote` 取价 → `units = amount_cny / price` → `buy(source_type="external_funding")`；
   - 每个 `(date, symbol)` 一把 `state_claim` 幂等闸（ADR-016），买入失败 `unclaim` 可重试。

3. **数据深度自愈**：`get_history_data` 在 DB 历史 < `_MIN_HISTORY_ROWS`（60 根）时首次拉 2y 全量回填，
   保证长周期指标 / regime 算得出；已有足够历史才 5d 增量。

## 后果 / 约束

- **对账口径**：系统看不到第三方平台真实成交，按 `amount_cny` 估算每日买入量记账，**月度用真实
  基金余额对账校准**。智慧定投实际金额浮动，`amount_cny` 是估算基准。
- **币种**：CNY 标的直接 `amount_cny / price`；非 CNY 标的经 `utils.fx.to_base` 折算后再除价。
- **代理跟踪**：场外联接基金（yfinance 取不到净值）按同指数的场内 ETF symbol 做价值代理记账
  （如京东 110020 → openInvest 记 510300.SS），份额是价值代理、市值/PnL 同指数跟得准。
- **开口池模型（已实现）**：`wealth_context.monthly_contribution_cny` —— 用户每月从外部补充进
  投资池的额度。WealthContextOfficer 据此把 portfolio cash 当**流量**而非封闭快照：低现金更不算
  流动性风险、30% 现金目标软化（铁律不变：本次加仓上限仍 = **当前** portfolio cash）。
