---
type: wiki-chapter
title: 盈透实盘 autopilot（IBKR）
tags: [ibkr-autopilot, execution, broker, real-money, ledger, idempotency, opt-in]
intent: opt-in 执行层：在独立 INVEST_HOME 里由 LLM 每周选标的、委员会每天定买卖，把裁决下成盈透真实限价单并按真实成交记账
schema_source:
  - src/openinvest/jobs/ibkr_autopilot.py:submit
  - src/openinvest/jobs/ibkr_autopilot.py:reconcile
  - src/openinvest/jobs/ibkr_autopilot.py:pick
  - src/openinvest/core/config/tunable.py:IbkrConfig
  - src/openinvest/services/strategy_write.py:replace_target_assets
documents:
  endpoints: []
  config_keys:
    - ibkr.enabled
    - ibkr.dry_run
    - ibkr.account
    - ibkr.max_order_usd
    - ibkr.max_day_usd
    - ibkr.min_order_usd
  symbols:
    - ibkr_autopilot
    - record_execution
    - record_fee
---

# 盈透实盘 autopilot（IBKR）

**这一层下真单。** 开启后，这个 INVEST_HOME 的委员会裁决会变成盈透（Interactive Brokers）账户里的真实限价单。
`ibkr.enabled` 默认关（全部 no-op，fork 用户零影响），打开后 `ibkr.dry_run` 默认仍为 true（只 `whatIfOrder` 试算）。
**只应在独立的盈透 INVEST_HOME 开**，target_assets 只放该账户里交易的 USD 标的。和 [23 模拟账户](23-paper-autopilot.md) 是两条线：
那边在本地账本按收盘价模拟成交，这边成交以券商回报为准。

分工：**LLM 选标的（每周一）→ 委员会定买卖（每个交易日）→ 代码硬闸兜底 → 券商成交回来再记账。**

## 三段

| 段 | 时机（UTC，cron） | 做什么 |
|---|---|---|
| `pick` | 周一（或 target_assets 为空；`--force` 立刻） | SDKAgent（带行情 / 宏观工具）在硬限制内选 ≤5 只美股 / 美股 ETF，输出 JSON；每只过 `_check_tradable` 后**整表替换** target_assets（`replace_target_assets`）。全部不合格 → 名单不动（防清空触发全仓清仓）。记录写 `memory/.ibkr/picks/<date>.json` |
| `submit` | 美股开盘前（12:45） | ① 券商里有、但已不在 target_assets 的持仓 → 清仓；② 当日 ACCUMULATE / BUY / TRIM / SELL 裁决 → DAY 限价单（买 = 参考价 ×1.01，卖 = ×0.99；开盘跳空超过 1% 不成交、收盘作废），先卖后买 |
| `reconcile` | 美股收盘后（21:30） | 拉当日成交，按 `execId` 幂等记账（`buy` / `sell` + 佣金 `record_fee`）；持仓与券商比对，不一致 → 写 `STOP` + 邮件；USD 现金差 ≥ 1 只提醒（分红 / 换汇 / 入金要手记） |

`openinvest ibkr_autopilot daily` = pick → `daily_report`（不发邮件）→ submit → `verdict_review`，各段隔离失败。参考价用 yfinance 最近收盘（≤5 个自然日，不能是兜底陈旧价）。
下单成功即 `record_execution(decision_id, True, reason="ibkr:<status>:<orderId>")`——程序照做了；成没成交以 reconcile 记的成交为准。

## 硬闸（代码层，LLM / 委员会给什么都先过这里）

- `ibkr.enabled` 关 → no-op；金额键不合法 → 一单不下；`INVEST_HOME/STOP` 存在 → submit 不下单（人看过删掉才恢复；reconcile 照常记账）
- 网关 `managedAccounts()` 里没有 `ibkr.account` → 什么都不做
- 只交易 target_assets 里的标的（清仓例外：卖的是券商已有持仓）；选股校验：美股代码、`EQUITY/ETF`、USD、美国交易所、股价 ≥ $5、3 个月日均成交额 ≥ $2000 万、名字不含杠杆 / 反向关键词
- 单笔 ≤ `ibkr.max_order_usd`；当日累计（本程序已挂未成交 + 当日已成交，按 orderRef `oi:` 前缀识别）≤ `ibkr.max_day_usd`；< `ibkr.min_order_usd` 不下（整笔清仓例外）
- 只挂限价单；买入 ≤ 券商 USD 现金 − 已挂买单 − 1（留佣金）；卖出 ≤ 券商持仓（现金账户本身不能融资 / 做空）；碎股按 4 位小数向下取整
- 合约查不到 / 有歧义 → `not_tradable_at_ibkr`；被券商拒（`Inactive` / `Cancelled`）→ 记 `executed=false` 带拒绝原因

## 幂等（ADR-016）

submit：每个 `(date, symbol, side)` 一把 `state_claim("ibkr_autopilot", …)`，下单后不 unclaim；盈透侧已有同 orderRef（`oi:<decision_id>` / `oi:exit:<date>/<sym>`）的挂单或成交也跳过。
只有什么都没下的前置失败（取不到价 / 陈旧行情 / 汇率缺失 / 异常）才 unclaim。dry_run 不占 claim、不记 execution。
reconcile：`idempotency_key=ibkr-<execId>` / `ibkr-fee-<execId>`，重跑只会 duplicate。

## 部署

- 网关：`gnzsnz/ib-gateway`（arm64 / amd64，内置 IBC 自动登录），`TRADING_MODE=live` 必须显式写（compose 默认值因版本而异），API 只映射 `127.0.0.1:4001`。实盘账户每周日要在 IB Key 上确认一次 2FA；建议给程序单独建一个「只交易、无出入金权限」的用户——同一用户名同时只能有一个会话，手机一登录会把网关踢下线。
- 依赖：`ib_async`（`pip install openinvest[ibkr]`；git clone 形态 `uv sync` 的 dev 组已带）。
- 首次上线：home 的 `memory/.state/config_overrides.json` 写 `ibkr.enabled=true`、`ibkr.account=<U…>`（**不写 dry_run**），先跑一次 `submit` 看 whatIf 结果（合约 / 碎股权限 / 佣金），再在 cron 行加 `INVEST_IBKR_DRY_RUN=false` 切真单。账本起点 = 券商 USD 现金（`openinvest deposit -c USD …`）。

## 配置

见 [22-configuration.md](22-configuration.md)：`ibkr.enabled` / `ibkr.dry_run` / `ibkr.account` / `ibkr.max_order_usd` / `ibkr.max_day_usd` / `ibkr.min_order_usd`。
**全部不进 API 白名单**——MCP / web API / agent 的 `config set` 打不开真单；只认 `INVEST_IBKR_*` 环境变量或 home 自己的 override 文件。金额键不合法（NaN / inf / ≤0 / 最小单 > 单笔上限）→ submit 整体拒单。

## 局限

- 当日成交只能当天拉（`reqExecutions` 只回当日）：某天 reconcile 没跑 → 第二天持仓对不上 → STOP，要人手补记。
- 分红 / 预扣税 / 换汇 / 入金不自动入账，只在现金差 ≥ USD 1 时提醒。
- 杠杆 / 反向 ETF 靠名字关键词识别，漏网的由单笔 / 单日上限兜底。
