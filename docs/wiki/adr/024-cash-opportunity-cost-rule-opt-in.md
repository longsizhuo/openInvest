# ADR-024：现金仓位机会成本规则改为 opt-in（默认 OFF）

**日期**：2026-06-30
**状态**：accepted
**取代/延续**：ADR-007（CIO zero-shot 钉死——本 ADR 移除其捆绑的 `cash_opportunity_cost_rule` 死 flag）、ADR-017（config-via-API 提供运行时覆盖）、ADR-020（concentration lens 同款 opt-in 范式）、ADR-023（诚实定位，不假装 alpha）

## Context

CIO prompt（`agents/skills/cio/SKILL.md`）里有一段「🔥 现金仓位机会成本规则（强制，必读）」：

> CONCENTRATION_PCT < 20% → **不允许给 HOLD**，默认至少 `ACCUMULATE`，alloc 取 DRY_POWDER_CNY × 5%~10%（建小试探仓）。唯一豁免：Macro=risk_off **且** Risk=high_risk。

它的金融逻辑是「持币观望不是免费的，市场每涨 1% 你就跑输 1%；0% 仓位等回调 = 赌时点」。本质是一条**激进部署杠杆**——只要子弹多、仓位低，就强制下场。

两个问题：

1. **它对默认用户过于强势**。和 ADR-020 的集中度 lens 同理：openInvest 持仓常是自选/watchlist，"现在不加仓"完全可能是正确决策（估值 99% 分位、downtrend、等更低位）。把 HOLD 直接判成"错误的 default"剥夺了委员会说"现在什么都不做最好"的能力——而"少做错事"正是 ADR-023 的诚实定位核心。
2. **它名义上像个开关，其实关不掉**。`core/config/locked.py` 里有 `cash_opportunity_cost_rule: bool = True`，挂在 `LockedPromptIdentity`（ADR-007 锁死区），看着像配置项——但**全仓没有任何代码读它**，只有一条测试断言它 == True。真正生效的是 SKILL.md 里的硬编码 prompt 文本，没接任何开关。

实测影响（2026-06-30，用户加 ¥10k 现金后跑委员会）：510300.SS 估值处 2 年 99% 分位、Quant neutral，CIO 本倾向 HOLD，但被该规则强制改判 `ACCUMULATE ¥2,100`——理由栏直接写「现金仓位机会成本规则：CONCENTRATION_PCT 0.9% < 20%，因此不允许 HOLD」。用户裁决：删掉这条强制。

## Decision

把它从「硬编码 + 死 flag」改成**真正的 config-via-API 开关**，对齐 ADR-020 集中度 lens 范式：

- 新增 `verdict.cash_opportunity_cost_rule_enabled`（`core/config/tunable.py`），**默认 `False`（规则关）**。
- 关闭（默认）：`agents/cio.py` 注入 `CASH_OPP_COST_DIRECTIVE` 软层 directive，明确「HOLD 在任何仓位/任何现金比例都合法，不得仅因低集中度/子弹多强制 ACCUMULATE 或禁止 HOLD；下方 Verdict 选项里『ACCUMULATE=100% 现金 default』『HOLD 只在 20%+ 合法』同样作废」。是否加仓纯按 Quant/Macro/Risk 信号 + 估值/趋势证据决定。
- 开启：directive 为空串，SKILL.md 原规则段照常生效（想要"低集中度就至少建试探仓"行为的用户显式 opt-in）。
- 进 `API_SETTABLE` 白名单（GUI 委员会配置区 + skill `config` 子命令 + `PUT /api/config` 自动可调，落盘持久跨进程共读）。
- 移除 `locked.py` 的死 flag `cash_opportunity_cost_rule` 及其测试断言（误导性：名字像开关但读不到）。

**纯 prompt 软层，无确定性后处理**。`cio_parse.py` 的 `_force_hold` 只会强制 *进入* HOLD，从不强制 *离开* HOLD，所以关闭机会成本规则不需要硬兜底——光去掉 prompt 压力，LLM 即可自由选 HOLD。

## Consequences

- 默认行为变更：低集中度 + 多子弹时，委员会不再被强制至少 ACCUMULATE，可以正当地 HOLD。**fork 用户首次升级会看到更多 HOLD**——这是预期的（对齐 ADR-023：HOLD 不是失败，是纪律）。
- 想保留旧激进部署行为的用户：`config --set verdict.cash_opportunity_cost_rule_enabled true`（或 GUI 委员会配置区开启）。
- 与 `verdict.risk_profile=aggressive`（uptrend HOLD→ACCUMULATE 顺势杠杆）正交：那条管"趋势顺势加杠杆"，本条管"低仓位强制下场"，两者都默认关、都是显式 opt-in 的激进选项。

## 验证

- `tests/test_committee_parser.py`：默认 prompt 含"机会成本规则已被用户关闭"directive；override `enabled=True` 时 directive 消失、原规则段保留。
- `tests/test_config.py`：locked 断言去掉死 flag 后全绿。
