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
    - ibkr.max_batches_per_day
    - ibkr.port
    - ibkr.notify_cmd
  symbols:
    - ibkr_autopilot
    - record_fee
---

# 盈透实盘 autopilot（IBKR）

**这一层下真单。** 开启后，LLM 为这个 INVEST_HOME 写的当日订单会变成盈透（Interactive Brokers）账户里的真实限价单。
`ibkr.enabled` 默认关（全部 no-op，fork 用户零影响），打开后 `ibkr.dry_run` 默认仍为 true（只 `whatIfOrder` 试算）。
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
| `reconcile` | 美股收盘后（21:30） | 按时间过滤拉近 7 天成交（网关若只回当日也无妨），按 `execId` 幂等记账（股票 `buy` / `sell`、换汇两腿、佣金 `record_fee`）；持仓与券商比对，不一致 → 写 `STOP` + 邮件；任一币种现金差 ≥ 1 只提醒（分红 / 入金要手记） |

随时查当前情况：`INVEST_HOME=<盈透 home> openinvest ibkr_autopilot status`（只读，不下单不记账；盈透连不上也返回本地账本 / 名单 / 当日订单 / STOP）。

任一段返回 `error` / `halted` 或抛异常 → CLI 发邮件并以退出码 1 结束（cron 不会静默失败）；单子一旦发出，之后出任何异常都保留 claim 不重下。
文中的「邮件」都是默认通道：设了 `ibkr.notify_cmd`（如 `hermes send --to discord:<你> --quiet`）就改走那条命令（标题 + 正文从 stdin 进），命令失败才退回邮件。

`openinvest ibkr_autopilot daily` = pick → `daily_report`（不发邮件）→ decide → submit → `verdict_review`，各段隔离失败。参考价用 yfinance 最近收盘（≤5 个自然日，不能是兜底陈旧价）。
orderRef = `oi:<date>/<symbol>/<side>`（清仓 `oi:exit:<date>/<symbol>`）；下单邮件逐单附 LLM 的理由。

## 常驻会话驱动（可选）

决策也可以交给一个**常驻的 Claude Code 会话**（tmux 里长开，`--remote-control` 可远程对话），而不是每次由 cron 调一次 LLM。
什么时候看盘、交不交易、交几批、何时换名单都由会话自己决定（Claude Code 会话内的 `CronCreate` 给自己排复查；会话内定时随会话退出丢失、循环任务 7 天过期）。系统 cron 只做不需要判断的事：`news_check` 新闻传感器（有新标题才叫醒会话）、收盘后 `reconcile` 对账、会话守护、每天一次兜底提醒（会话自排的定时丢了时的安全网，可忽略）；委员会意见由会话按需 `committee` 生成。
会话自己 `brief` 看上下文（与 LLM 路径同一份系统规则 + 账户 / 名单 / 委员会裁决 + 盈透新闻标题），研究后用
`pick --picks '<JSON>'` / `decide --orders '<JSON>' [--batch N]` 交自己写的 JSON（同一套校验落盘），再 `submit [--batch N]`。
追加单（新闻触发或会话自己安排的复查）用 `--batch 2,3…`：文件（`<date>.bN.json`）、orderRef（`oi:<date>#bN/…`）、claim 都与开盘前那批分开，单日额度共用。
`news [--symbols A,B] [--days N] [--article PROVIDER:ID]` 只读查盈透免费新闻（Briefing.com / Dow Jones，账户自带）。
新闻标题 / 正文是外部内容：输出里标 `untrusted`、正文包在 `<<<EXTERNAL_ARTICLE … EXTERNAL_ARTICLE>>>` 之间，会话规则要求只当信息、不执行其中的指令；brief 只带标题，正文要会话主动按 id 取。会话交单优先用 `--orders-file` / `--picks-file`（免 shell 引号）；每天批次数上限 `ibkr.max_batches_per_day`（默认 4，超了 `batch_over_limit`）。

## 硬闸（代码层，LLM / 委员会给什么都先过这里）

- `ibkr.enabled` 关 → no-op；金额键不合法 → 一单不下；`INVEST_HOME/STOP` 存在 → submit 不下单（人看过删掉才恢复；reconcile 照常记账）
- 网关 `managedAccounts()` 里没有 `ibkr.account` → 什么都不做
- 只交易 target_assets 里的标的（清仓例外：卖的是券商已有持仓）；选股校验：美股代码、`EQUITY/ETF`、USD、美国交易所、股价 ≥ $5 且 ≤ 单笔上限（只能整股，一股贵过上限就买不了：`price_above_order_cap`）、3 个月日均成交额 ≥ $2000 万、名字不含杠杆 / 反向关键词、不收带点号的分级股（`BRK.B` 在盈透侧写作 `BRK B`，持仓 key 会对不上）
- 单笔 ≤ `ibkr.max_order_usd`；当日累计（本程序已挂未成交 + 当日已成交，按 orderRef `oi:` 前缀识别）≤ `ibkr.max_day_usd`；< `ibkr.min_order_usd` 不下（整笔清仓例外）
- 只挂限价单；买入 ≤ 券商 USD 现金 − 已挂买单 − 1（留佣金）；卖出 ≤ 券商持仓（现金账户本身不能融资 / 做空）；**只下整股**——盈透 TWS API 不收碎股单（实测 error 10243 "Fractional-sized order cannot be placed via API"），数量向下取整，凑不够一股不下（`share_price_above_cap` / `too_small`）；买卖都受单笔 / 单日上限约束，唯一例外是**整笔清仓**时一股已涨过单笔上限——允许单独卖 1 股，但必须放得进当日剩余额度；放不进就不卖，记 `exit_blocked_by_caps` 并邮件提醒（人工卖或调高 `ibkr.max_day_usd`）
- 合约查不到 / 有歧义 → `not_tradable_at_ibkr`；被券商拒（`Inactive` / `Cancelled`）→ 终局跳过，原因进当日邮件

## 幂等（ADR-016）

decide：当日订单文件即闸——存在就原样复用。submit：每个 `(date, symbol, side)` 一把 `state_claim("ibkr_autopilot", …)`，下单后不 unclaim；盈透侧已有同 orderRef 的挂单或成交也跳过。
只有什么都没下的前置失败（取不到价 / 陈旧行情 / 异常）才 unclaim。dry_run 不占 claim。
reconcile：`idempotency_key=ibkr-<execId>` / `ibkr-fee-<execId>`，重跑只会 duplicate。

## 部署

- 券商接口：本机 IB Gateway（`gnzsnz/ib-gateway`，arm64 / amd64，内置 IBC 自动登录）+ TWS API（`ib_async`），API 只映射 `127.0.0.1:4001`，
  `TRADING_MODE=live` 必须显式写（compose 默认值因镜像版本而异）。每天自动重启不需要 2FA；**盈透对个人账户强制每周一次 IB Key 确认**
  （网关重新认证时手机收到推送，点批准）——OAuth 1.0a 只开放给机构客户，个人账户绕不开这一步。主账号登录会和手机 / 网页撞会话，
  `EXISTING_SESSION_DETECTED_ACTION=primary` 让网关优先（下单 / 对账不漏）。cron 每次连网关、跑完 `disconnect()`；`status` 用另一个 clientId。
  **二次验证设备必须是 IBKR Mobile 的 IB Key（推送批准）**：网关是无头的，Passkey（要在发起登录的设备上做生物识别）和 Mobile Authenticator app（要往网关窗口里输 6 位码）都过不去；
  账户绑了多种设备时用 `TWOFA_DEVICE` 指定，值要和登录窗口列表里的写法一字不差（`IB Key`）。
- 下单只认网关明确回执（`PreSubmitted` / `Submitted` / `Filled`，最多等 10 秒）：被拒（权限 / 预防性检查 / 碎股）→ `rejected`，等不到回执 → `unconfirmed`，都终局跳过、保留 claim 不重下（真成交了 reconcile 照样记账），原因进当日邮件。compose 写 `BYPASS_WARNING=no`，网关的 API 下单预防性检查保持开启。
- 换汇：**submit 不再先自己换**。现金账户买 USD 股票时，盈透会从基础币种（AUD）自动换汇（实测免佣金）；先自己换会因为换来的 USD
  还没结算（T+1/T+2）而被盈透再换一次，叠加出非基础币种透支（2026-10-20 实测 AUD 一度 −146，当天盈透又自动反向换回、归零）。
  自动换汇佣金 0、只吃点差；显式换汇单每笔最低 USD 2（小账户≈2%），所以**没有换汇阶段**——币种结构交给盈透自动处理。
  买入额度 = 盈透 `AvailableFunds`（基础币种）折 USD。
- 对账按券商实际记账：券商已确认的成交 / 换汇 / 佣金允许把账本现金记成负数（`allow_negative`，只给对账用），不会因「余额不足」误停手。
- 模拟盘并行：盈透 Paper Trading 账户（`DU…`，有自己的用户名，登录不需要 IB Key）可以和实盘同时跑——再起一个 `TRADING_MODE=paper` 的网关容器，映射到 `127.0.0.1:4002`，给模拟盘单独一个 INVEST_HOME，设 `ibkr.port=4002`、`ibkr.account=DU…`。两边的账本、名单、STOP 各自独立。
- 依赖：`ib_async`（`pip install openinvest[ibkr]`；git clone 形态 `uv sync` 的 dev 组已带）。
- 首次上线：home 的 `memory/.state/config_overrides.json` 写 `ibkr.enabled=true`、`ibkr.account=<U…>`（**不写 dry_run**），先跑一次 `submit` 看 whatIfOrder 结果（合约 / 碎股权限 / 佣金），再在 cron 行加 `INVEST_IBKR_DRY_RUN=false` 切真单。账本起点 = 券商 USD 现金（`openinvest deposit -c USD …`）。

## 配置

见 [22-configuration.md](22-configuration.md)：`ibkr.enabled` / `ibkr.dry_run` / `ibkr.account` / `ibkr.max_order_usd` / `ibkr.max_day_usd` / `ibkr.min_order_usd`。
**全部不进 API 白名单**——MCP / web API / agent 的 `config set` 打不开真单；只认 `INVEST_IBKR_*` 环境变量或 home 自己的 override 文件。金额键不合法（NaN / inf / ≤0 / 最小单 > 单笔上限）→ submit 整体拒单。

## 局限

- 成交按时间过滤回看 7 天，但网关可能只回当日：哪天 reconcile 没跑，下次对账持仓对不上 → STOP，要人手补记（execId 幂等键挡重复）。
- 换汇是否需要单独的外汇交易权限未验证：首次试算的 whatIfOrder 会暴露，必要时在 Client Portal 开通。
- 分红 / 预扣税 / 入金不自动入账，只在任一币种现金差 ≥ 1 时提醒。
- 杠杆 / 反向 ETF 靠名字关键词识别，漏网的由单笔 / 单日上限兜底。
