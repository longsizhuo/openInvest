---
type: adr
status: proposed
title: "ADR-027：账本活动类型——分红 / 利息 / 费用 / 税 / 拆股 / 场外基金两段式确认"
tags: [ledger, portfolio_history, activities, dividend, split, fund, t+1, idempotency]
intent: 把 portfolio_history.jsonl 从"买卖存取"扩成完整活动账本，不改 portfolio.md v2 快照 schema
schema_source:
  - src/openinvest/core/portfolio_manager.py:PortfolioManager
  - src/openinvest/core/memory_store.py:MemoryStore
  - src/openinvest/services/account_performance.py:_ledger_events
  - src/openinvest/calc/ledger_replay.py:replay_ledger
  - src/openinvest/core/schemas.py:Activity
documents:
  endpoints: []
  config_keys: []
  symbols:
    - record_dividend
    - record_fee
    - record_split
    - place_fund_order
    - confirm_fund_order
    - replay_ledger
---

# ADR-027：账本活动类型

- 状态：🟡 提议（2026-10-18；按 PR #472 评审修订：有状态回放、在途对全部估值方可见、确认 CAS 幂等、按类型的确认延迟、`schema_version` 命名、复权价核实），评审通过后实现（评审路线 C7）；
  **PR1 已实现（#479）**：Activity schema + `calc/ledger_replay.py:replay_ledger` + 消费方切换 + 回放不变量 + 复权价核实（见 D3/D4 的 PR1 注）；
  **PR2 已实现（#482）**：dividend / interest / fee / tax / split / return_of_capital 写入 + MCP/CLI + status 近 30 天分红利息 + `daily_prices.close_raw`
  与估值价格口径（见 D4 的 PR2 注）
- 背景：2026-10-17 全面评审对照 Wealthfolio / Ghostfolio / Portfolio Performance / Sharesight：它们都有
  分红 / 利息 / 费用 / 税 / 拆股 / 退回本金 的活动类型；openInvest 的账本只有买卖存取，
  `grep dividend|tax` 在 src/ 零命中，场外基金按"下单即成交"记账（实际 T+1 按当日净值确认）。

## 现状（只写事实）

- 快照：`memory/portfolio.md` v2 = `cash{ccy: amt}` + `holdings[]`（symbol / kind / units / avg_cost /
  cost_currency / proxy_kind …），`schema_version` 钉死 `2`（`core/schemas.py`）。
- 流水：`memory/portfolio_history.jsonl` 追加式，记录形状**不统一**：`action` 有 buy / bought / sold /
  deposit / withdraw / gift / manual_override / delete_holding / dca_reconcile_*；价格字段有
  `price` 也有 `price_per_unit`+`total_amount`；`fee` 只在 19 条里出现；外部现金流靠
  `action ∈ {deposit, withdraw}` 或 `funding_source=external_funding` 识别（`account_performance._ledger_events`
  里有一份别名表）。
- 消费方：`services/account_performance`（NAV 回放 / TWR / XIRR）、`core/decision_ledger`（决议↔成交匹配）、
  `services/holdings_digest`（#468）、`jobs/pnl_snapshot`、`services/discipline`。各自读原始 jsonl。

## 决策

### D1 不改 portfolio.md 的 schema（不做 v3 迁移）

快照仍是 v2。活动只进流水；快照字段的变化（units / avg_cost / cash）由写入方法在同一个
`with_portfolio_tx` 事务里落下。理由：v2 → v3 迁移是 2026-07-08 覆盖事故的同类风险源，
而新活动不需要新快照字段（场外基金的在途金额例外，见 D4，用 `extra` 字段承载）。

### D2 活动记录统一 schema（新记录带 `schema: 2`，老记录不动）

`core/schemas.py` 新增 `Activity`（Pydantic）：

| 字段 | 说明 |
|---|---|
| `schema_version` | 固定 2（老记录无此字段 = 1）。不叫 `schema`：与 Pydantic v2 的 `BaseModel.schema()` 同名会告警 |
| `action` | 新写入只用：`buy` / `sell` / `deposit` / `withdraw` / `gift` / `dividend` / `interest` / `fee` / `tax` / `split` / `return_of_capital` / `fund_order_placed` / `fund_order_confirmed` / `fund_order_cancelled` / `delete_holding` / `correction`。历史动作在解析层归一：`bought`→`buy`、`sold`→`sell`、`dca_reconcile_backfill`→`buy`(source=dca_reconcile, external=True)、`dca_reconcile_reverse`→`sell`(同)、`manual_override`→`correction`（它记的是**绝对值**，解析层从前序状态算出增量，见 D3） |
| `symbol` | 标的（现金类活动可空） |
| `units` / `price` / `amount` / `currency` | 数量 / 单价 / 不含费用的总额（`amount = units × price`）；费用与税单列，符号与现金影响只看 `cash_effect` |
| `fee` / `tax_withheld` | 可选，与 `amount` 同币种 |
| `cash_effect` | 该活动对现金的净影响（符号明确：流入正、流出负），**读方只认这个字段** |
| `units_effect` | 对持仓数量的净影响（拆股为乘法：见 `split_ratio`） |
| `split_ratio` | 仅 split：新/旧（10:1 拆写 10，1:10 并写 0.1） |
| `external` | bool：是否外部现金流（deposit / withdraw / gift 为 True；分红利息为 False） |
| `trade_date` / `settle_date` / `nav_date` | 日期三件套（场外基金用全部三个） |
| `source` / `idempotency_key` / `note` / `correction` | 来源（cli / mcp / dca_daily / commsec / fund_auto_confirm）、幂等键（#432 已有）、备注、是否修正记录（manual_override 归一后为 True） |

### D3 单一解析入口 `calc/ledger_replay.py`（纯）

`replay_ledger(records, *, alias) -> Tuple[List[Activity], LedgerState]`：**有状态的折叠**，不是逐条无状态映射——
`manual_override` 记的是持仓绝对值，必须从前序状态算出增量再归一成 `correction`（`units_effect` = 新−旧，
`correction=True`）。老记录按现有别名表归一（bought→buy、sold→sell、price_per_unit→price、
`funding_source=external_funding`→external=True、gift→external=True 等），新记录直接校验。
读 `portfolio_history.jsonl` 的全部消费方切换到它：`services/account_performance`、`core/decision_ledger`
（含 `core/decision_calc`）、`services/holdings_digest`、`jobs/pnl_snapshot`、`services/discipline`、
`skill_cmds/lifecycle_cmds`、`connectors/web_api/routers/read.py`；`core/memory_store` 只保留写入与原始读取。
各自的私有别名表删除。
> **PR1 注（实现与上面清单的差异）**：实际自己解析流水字段的只有 `services/account_performance`（含"账本里有、持仓里没有的
> symbol"登记）和 `core/decision_calc.history_as_trades`（`decision_ledger` 经它读），这两处已切换。`holdings_digest` /
> `jobs/pnl_snapshot` 不读流水（经 `account_performance` 取业绩），`services/discipline` 读的是 verdict_review / interventions；
> `skill_cmds/lifecycle_cmds`（判空）、`routers/read.py` 与 `services/skill_views`（原样展示原始行）不解析字段，故不动；
> `scripts/reconcile_dca_phantom` 是写入侧对账脚本，按原始动作名查自己写的行，保持原样。`gift` 保持 `gift`（D2 动作表里它自成一类）
> 而不是并进 `buy`，否则决议↔成交匹配无法把赠股和下单区分开。`tests/test_ledger_replay_invariance.py` 另守"没人再自抄别名表"。

**回放不变量测试**：对生产流水只读拷贝，改前改后 NAV 序列逐日相等
（合成脱敏流水进 tests；生产比对结果贴 PR）。

### D4 新活动对快照的确定性效果

| action | cash | units | avg_cost | 备注 |
|---|---|---|---|---|
| dividend | +net（gross − tax_withheld） | 0 | 不变 | `reinvest=True` 时再追加一条 `buy`（source=drip，external=False） |
| interest | +amount | — | — | 货币基金 / 现金利息 |
| fee | −amount | 0 | 不变 | 账户级或标的级（symbol 可空） |
| tax | −amount | 0 | 不变 | 红利税补缴、港股通股息税差额等 |
| split | 0 | ×ratio | ÷ratio | 市值不变 |
| return_of_capital | +amount | 0 | 减 amount/units | 成本降低，不是收益 |
| fund_order_placed | −amount | 0 | — | 快照 `holding.extra.pending_orders[]` 记在途（金额 / 下单日 / 预计确认日） |
| fund_order_confirmed | 0 | +amount÷nav（扣申购费） | 加权更新 | 用确认日净值；清掉对应在途 |
| fund_order_cancelled | +amount | 0 | — | 退回在途 |

TWR / XIRR 口径（`account_performance`）：`external=True` 的进分母与 XIRR 现金流；dividend / interest /
fee / tax 都是内部流，进 NAV 不进分母；split 对 NAV 无影响。

**在途金额对所有估值方可见**（评审指出 D1 的漏洞）：`fund_order_placed` 扣了现金、份额还没到，若只在 TWR 里补回，
status / `pnl_snapshot` / `holdings_digest` / `rebalance_plan` / 集中度 在 T 到 T+1 之间都会看到总资产凭空少一笔。
因此在途金额进**唯一的总资产实现** `utils/fx.total_portfolio_value_cny`（#417 起所有估值方都走它）：按面值计入，
并由 `PortfolioManager.pending_fund_orders()` 统一读取；不允许任何估值方自己读 `extra.pending_orders`。

**split 与复权价**：若 `db/market_data.db` 存的是复权价（yfinance `auto_adjust` 默认 True，需在 PR1 核实
`daily_prices.source` 的口径），回放时不得再按 `split_ratio` 缩放历史 units，否则市值双重调整；
正确做法是回放全程用拆股后的数量口径（等价于 原始 units × 未复权价）。PR1 必须先确认价格口径并写进
`calc/ledger_replay.py` 的 docstring；两种口径各有测试。

> **PR1 核实结论**（PR2 补充：Yahoo 的 `Close` 本身已按拆股复权、只有 `Adj Close` 才含分红复权，所以 `close_raw` 是"拆股后口径、未含分红"，不是拆股前原始价）：`daily_prices.close` **是复权价，且拆股与分红都复权**（全仓只用 `yf.Ticker().history(period=...)`，yfinance 1.7.0
> 默认 `auto_adjust=True`，无任何调用传 `auto_adjust=False`；生产库实测 NVDA 2024-06-07 收盘 120.68 = 拆股后口径）。精确的回放规则因此是：
> ① **split**：价格序列已按拆股后口径连续，回放的数量必须在**所有历史日期**用拆股后口径（拆股前数量 ×ratio 重述）；上一段
> "不得再按 split_ratio 缩放历史 units"指的是不能在拆股日才 ×ratio——否则拆股前"拆股前数量 × 复权价"低估 ratio 倍、拆股日 NAV 凭空跳变。
> ② **分红**：复权价路径已把分红当再投资算进收益，PR2 若再把 `dividend` 现金记进 NAV，该标的 TWR 会把同一笔分红算两次——
> PR2 须二选一（有分红活动的标的改读未复权价，或分红只做现金流分类不入 NAV）。

> **PR2 落地（对上面"二选一"的决定）**：分红必须记真实现金，所以历史估值**不能**用复权价——`daily_prices` 加可空列 `close_raw`
> （yfinance `Close`，`history(auto_adjust=False)`；`close` 仍是 `Adj Close`，逐位不变，regime / 指标 / forward_return 零影响）。
> 账户业绩 / 持仓归因按 symbol 选口径：所需日期范围内 `close_raw` 全有 → 原始价，缺一天 → 整只复权价，不混用，返回 `price_basis`。
> 复权价回退的标的，收入类活动（dividend / return_of_capital / DRIP 买入）不进回放（价格已含，不双计）。split 不论哪种口径都用拆股后
> 数量（`later_split_factors`）。场外基金（只有前复权净值）与黄金克价代理（期货 × 外汇无分红）不回填，恒为 `adjusted`。
> 回填：`openinvest backfill_raw_close`。`record_split` 补记历史拆股时，账本里已有除权日当天或之后的份数变动 → 拒绝（快照已混拆股后份数，账本无撤销），
> `--ledger-only` 只补流水（#482 评审）。持仓归因的"今日涨跌"不计入当日分红（除息日原始价会下跌，分红现金不在这一段）——另议。

### D5 场外基金两段式 + 自动确认

- `place_fund_order(symbol, amount, order_date=today)`：15:00 前下单按当日净值、之后按下一交易日，
  `settle_date` 默认 T+1 交易日（规则写在 `calc/fund_rules.py`，A 股交易日历用现有 `is_closed_weekend` +
  节假日表，没有节假日表就按工作日并在记录里标 `calendar=weekday_only`）。
- `confirm_fund_order(order_id, nav=None)`：不传 nav 时用 `utils/eastmoney_fund.fetch_fund_nav_history`
  取 `nav_date` 的净值；新增 job `fund_confirm`（工作日 21:30–23:00 每 30 分钟，看门狗纳入）自动确认
  已到 `nav_date` 且净值已公布的在途单；取不到就留着，status 显示"在途待确认 N 天"。
- 确认延迟按基金类型配置：`fund.confirm_lag_days` 默认 `{default: 1, qdii: 2, fof: 2}`（QDII / FOF 的确认与净值公布
  常为 T+2 或更晚；类型从东方财富基金概况取，取不到按 default 并在 status 标"按 T+1 估计"）。
- **幂等（ADR-016 红线 4，cron 重跑 + 手动并发）**：确认是对 `order_id` 的 `placed → confirmed` 状态转换，检查与写入在
  同一个 `with_portfolio_tx` 里完成（CAS，参照 ADR-016 #127）；手动 `confirm_fund_order` 与 `fund_confirm` job 同时执行只有
  一个生效，另一个返回 `already_confirmed`；`fund_order_cancelled` 对已确认的单子拒绝。PR3 在 ADR-016 的全路径审计表里
  为 `place_fund_order` / `confirm_fund_order`（手动与 job）/ `fund_order_cancelled` / `record_dividend` 等各加一行。
- 旧行为（`buy` 直接记基金）保留可用，`buy` 对 `kind=fund` 且 symbol 是场外代码时打 warning 建议改用两段式。

### D6 入口（agent 必须拥有全部功能）

MCP 工具：`record_dividend`、`record_fee`（含 tax 作为 kind 参数）、`record_split`、`place_fund_order`、
`confirm_fund_order`；CLI 同名子命令；全部走 `with_portfolio_tx` + `idempotency_key`（#432 同款）；
`status` 显示在途基金单与最近 30 天分红合计；SKILL.md 命令表同步。REST 不新增（已弃用）。

### D7 不做

- 税务批次（FIFO/LIFO/specific lot）与资本利得报表：Sharesight/Rotki 级功能，中国个人投资者场景需求弱，
  且需要批次级成本模型，另立 ADR。
- 港股通结算汇率两价（参考价 / 结算价）：先在 `fee` / `note` 里手记，等有真实港股通成交流水再做。
- 券商自动同步：C6 的 CSV 快照导入与本 ADR 正交。

## 一句话信号影响翻译

账本能记分红、利息、费用、税、拆股和场外基金的"下单 → 次日按净值确认"；你看到的裁决与委员会输入不变，
变的是业绩和持仓数字更对（分红算进收益、拆股不再把持仓算成暴跌、基金申购不再按下单日价格记）。

## 验证

- 回放不变量：老流水 NAV 序列逐日不变（`tests/test_ledger_events_invariance.py`，用脱敏的合成流水 +
  对生产只读拷贝的一次性人工比对贴 PR）。
- 每种活动一个"快照效果"测试 + 幂等重放测试 + TWR 分类测试（external 与否）。
- 场外基金：下单 → 次日自动确认 → units = amount/nav 的端到端（mock 净值）；净值未公布时留在途。
- 插件实测：虚构 INVEST_HOME 跑 MCP 五个新工具 + status。

## 实施切分（三个 PR）

1. `feat(ledger)`: Activity schema + `replay_ledger` 有状态解析入口 + 上述全部消费方切换 + 回放不变量 + 价格口径核实（纯重构，行为不变）。
2. `feat(ledger)`: dividend / interest / fee / tax / split / return_of_capital 写入方法 + MCP/CLI + status + 业绩口径。
3. `feat(fund)`: 场外基金两段式 + `fund_confirm` job + 自动确认 + status 在途显示。
