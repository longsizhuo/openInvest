---
type: wiki-chapter
title: 盈透实盘 autopilot（IBKR）
tags: [ibkr-autopilot, execution, broker, real-money, ledger, idempotency, opt-in]
intent: opt-in 执行层：在独立 INVEST_HOME 里由 LLM 每周选标的、每天定买卖（委员会裁决只当参考），下成盈透真实限价单并按真实成交记账
schema_source:
  - src/openinvest/jobs/ibkr_autopilot.py:submit
  - src/openinvest/jobs/ibkr_autopilot.py:reconcile
  - src/openinvest/jobs/ibkr_autopilot.py:pick
  - src/openinvest/jobs/ibkr_autopilot.py:decide
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
    - record_fee
---

# 盈透实盘 autopilot（IBKR）

**这一层下真单。** 开启后，LLM 为这个 INVEST_HOME 写的当日订单会变成盈透（Interactive Brokers）账户里的真实限价单。
`ibkr.enabled` 默认关（全部 no-op，fork 用户零影响），打开后 `ibkr.dry_run` 默认仍为 true（只 whatif 试算）。
**只应在独立的盈透 INVEST_HOME 开**，target_assets 只放该账户里交易的 USD 标的。和 [23 模拟账户](23-paper-autopilot.md) 是两条线：
那边在本地账本按收盘价模拟成交，这边成交以券商回报为准。

分工：**LLM 选标的（每周一）→ 委员会照常出裁决（只当参考）→ LLM 定当日订单 → 代码硬闸兜底 → 券商成交回来再记账。**
委员会和下单的 LLM 是同一个模型（`LLM_MODEL`）；区别是委员会套着为「给人提建议」写的保守规则（SELL 门槛高、多道 force-HOLD），
下单这一步只受代码硬闸约束。委员会裁决的 execution 一概不写——执行的是 LLM 的单，`verdict_review` 对委员会的打分不受影响。

## 三段

| 段 | 时机（UTC，cron） | 做什么 |
|---|---|---|
| `pick` | 周一（或 target_assets 为空；`--force` 立刻） | SDKAgent（带行情 / 宏观工具）在硬限制内选 ≤5 只美股 / 美股 ETF，输出 JSON；每只过 `_check_tradable` 后**整表替换** target_assets（`replace_target_assets`）。全部不合格 → 名单不动（防清空触发全仓清仓）。记录写 `memory/.ibkr/picks/<date>.json` |
| `decide` | 委员会日报之后 | SDKAgent（带行情 / 宏观工具）看账户、本周名单（含选股理由）、当日委员会裁决与 CIO 备忘录摘要，输出当日订单 JSON（`symbol` / `BUY\|SELL` / `usd` 或 SELL 的 `"all"` / `why`）；名单外 / 方向非法 / 金额非正有限数 / 同标的第二单 → 拒。写 `memory/.ibkr/decisions/<date>.json`；**文件已存在就不再问 LLM**（重跑不换一套单），LLM 报错不落盘（可重试） |
| `submit` | 紧接 decide（12:45 那条 cron 里） | ① 券商里有、但已不在 target_assets 的持仓 → 清仓；② 当日订单 → DAY 限价单（买 = 参考价 ×1.01，卖 = ×0.99；开盘跳空超过 1% 不成交、收盘作废），先卖后买 |
| `reconcile` | 美股收盘后（21:30） | 拉近 7 天成交，按 `execution_id` 幂等记账（股票 `buy` / `sell`、换汇两腿、佣金 `record_fee`）；持仓与券商比对，不一致 → 写 `STOP` + 邮件；任一币种现金差 ≥ 1 只提醒（分红 / 入金要手记） |

随时查当前情况：`INVEST_HOME=<盈透 home> openinvest ibkr_autopilot status`（只读，不下单不记账；盈透连不上也返回本地账本 / 名单 / 当日订单 / STOP）。

任一段返回 `error` / `halted` 或抛异常 → CLI 发邮件并以退出码 1 结束（cron 不会静默失败）；单子一旦发出，之后出任何异常都保留 claim 不重下。

`openinvest ibkr_autopilot daily` = pick → `daily_report`（不发邮件）→ decide → submit → `verdict_review`，各段隔离失败。参考价用 yfinance 最近收盘（≤5 个自然日，不能是兜底陈旧价）。
orderRef = `oi:<date>/<symbol>/<side>`（清仓 `oi:exit:<date>/<symbol>`）；下单邮件逐单附 LLM 的理由。

## 硬闸（代码层，LLM / 委员会给什么都先过这里）

- `ibkr.enabled` 关 → no-op；金额键不合法 → 一单不下；`INVEST_HOME/STOP` 存在 → submit 不下单（人看过删掉才恢复；reconcile 照常记账）
- OAuth 会话的账户列表（`portfolio_accounts`）里没有 `ibkr.account` → 什么都不做
- 只交易 target_assets 里的标的（清仓例外：卖的是券商已有持仓）；选股校验：美股代码、`EQUITY/ETF`、USD、美国交易所、股价 ≥ $5、3 个月日均成交额 ≥ $2000 万、名字不含杠杆 / 反向关键词、不收带点号的分级股（`BRK.B` 在盈透侧写作 `BRK B`，持仓 key 会对不上）
- 单笔 ≤ `ibkr.max_order_usd`；当日累计（本程序已挂未成交 + 当日已成交，按 orderRef `oi:` 前缀识别）≤ `ibkr.max_day_usd`；< `ibkr.min_order_usd` 不下（整笔清仓例外）
- 只挂限价单；买入 ≤ 券商 USD 现金 − 已挂买单 − 1（留佣金）；卖出 ≤ 券商持仓（现金账户本身不能融资 / 做空）；碎股按 4 位小数向下取整
- 合约查不到 / 有歧义 → `not_tradable_at_ibkr`；被券商拒（`Inactive` / `Cancelled`）→ 终局跳过，原因进当日邮件

## 幂等（ADR-016）

decide：当日订单文件即闸——存在就原样复用。submit：每个 `(date, symbol, side)` 一把 `state_claim("ibkr_autopilot", …)`，下单后不 unclaim；盈透侧已有同 orderRef 的挂单或成交也跳过。
只有什么都没下的前置失败（取不到价 / 陈旧行情 / 异常）才 unclaim。dry_run 不占 claim。
reconcile：`idempotency_key=ibkr-<execId>` / `ibkr-fee-<execId>`，重跑只会 duplicate。

## 部署

- 券商接口：盈透 Client Portal Web API + **First Party OAuth 1.0a**（`ibind`），不需要网关进程、不需要每周 IB Key。
  一次性：本机 `openssl` 生成签名 / 加密密钥对 + DH 参数 → 账户持有人登录 `https://ndcdyn.interactivebrokers.com/sso/Login?action=OAUTH&RL=1&ip2loc=US`
  （这一次要 IB Key），填 9 位大写 consumer key、上传三个公钥文件、Generate Token、打开 Enable OAuth Access →
  把 Access Token / Secret 写进运营者自己的 env 文件（`IBIND_OAUTH1A_*`，见 ibind 文档）。令牌一般要到周末盈透服务器重启后才生效。
  每次 cron 运行自建会话、跑完 `close()`（`IBIND_MAINTAIN_OAUTH=False`，不起保活线程）。
- 下单确认问题白名单（`_answers`）：缺行情 / 立即成交 / 价格保护帽 / 可能被拒提示 → 确认；价格偏离 / 数量 / 金额上限 / 平仓 / 多账户 / 现金数量单 → 拒；
  市价确认只对换汇放行。白名单外的问题 ibind 直接抛错 → 单不下、记 `rejected`。
- 账户内换汇：USD 不够下最小单而 AUD ≥ 50 时，submit 先把 AUD（留 5 零头，单日 ≤ 1000）市价换成 USD（`isCcyConv`），
  每天最多一次（claim + cOID）；reconcile 把换汇成交按两腿记账。钱不出账户。
- 依赖：`ibind[oauth]`（`pip install openinvest[ibkr]`；git clone 形态 `uv sync` 的 dev 组已带）。
- 首次上线：home 的 `memory/.state/config_overrides.json` 写 `ibkr.enabled=true`、`ibkr.account=<U…>`（**不写 dry_run**），先跑一次 `submit` 看 whatIf 结果（合约 / 碎股权限 / 佣金），再在 cron 行加 `INVEST_IBKR_DRY_RUN=false` 切真单。账本起点 = 券商 USD 现金（`openinvest deposit -c USD …`）。

## 配置

见 [22-configuration.md](22-configuration.md)：`ibkr.enabled` / `ibkr.dry_run` / `ibkr.account` / `ibkr.max_order_usd` / `ibkr.max_day_usd` / `ibkr.min_order_usd`。
**全部不进 API 白名单**——MCP / web API / agent 的 `config set` 打不开真单；只认 `INVEST_IBKR_*` 环境变量或 home 自己的 override 文件。金额键不合法（NaN / inf / ≤0 / 最小单 > 单笔上限）→ submit 整体拒单。

## 局限

- 成交只能回看 7 天（Web API `trades?days=7`）：连续一周 reconcile 没跑才会漏记 → 持仓对不上 → STOP，要人手补记。
- 分红 / 预扣税 / 入金不自动入账，只在任一币种现金差 ≥ 1 时提醒。
- 杠杆 / 反向 ETF 靠名字关键词识别，漏网的由单笔 / 单日上限兜底。
