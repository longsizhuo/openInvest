# ADR-020：集中度 lens 默认 OFF（concentration 改为 opt-in）

**日期**：2026-06-25
**状态**：accepted
**取代/延续**：ADR-013（trim-concentration-override）、ADR-019（移除 solvency 自动兜底，集中度只由 lens 控制）、ADR-017（config-via-API 提供运行时覆盖）

## Context

集中度（concentration）视角的隐含前提是：**用户把全部净资产都录进了 openInvest**，所以"单资产占持仓 X%"等于"单资产占身家 X%"。

实际上 openInvest 的持仓多是**自选 / watchlist**——用户只录了想跟踪的几个标的，没把所有钱写进来（例：用户只录了一个小额账户，主要净资产不在系统里）。在这个前提下，"持仓集中度 = 风险 → 建议减仓"对默认用户就是**错的**。

过去几轮一直在修集中度的**算法**让它"算得更对"：
- ADR-019 / #84：移除 solvency 自动兜底（"兜底足→集中度不算险"在 parse 层反转 CIO，prompt 不知情，自相矛盾）。
- #89：NaN 价不再污染总资产/集中度。

但这些都是在修一个**前提就错**的功能，只会不断冒新 bug。最新一例（2026-06-25）：某持仓的委员会凌晨跑时，另一只海外市场持仓（非 CNY 腿）无新鲜报价，按 #89 被剔出 `total_assets_cny` → 该持仓的集中度被动虚高（= 该持仓/(该持仓+现金)，缺报价的那只不在分母）→ 驱动了一个本不该有的大额 `TRIM`。

## Decision

**`core/config/tunable.py: concentration_lens_enabled` 默认 `True → False`。** 集中度从"默认开、人人得想办法关"改为 **opt-in**：

- **默认（False）**：不因持仓集中度建议减仓——`TRIM_REASON=concentration` 无条件 force-HOLD，Risk/CIO prompt 压掉超配规则。仍保留**波动 / 回撤 / 止损 / 估值 / 压力测试 / 现金流动性**风险维度。
- **想要的人**（把全部净资产录入系统、确实关心组合集中度）显式开启：`/api/config` 设 `verdict.concentration_lens_enabled=true`，或 env `INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=true`。

既有的 3 层 disable 实现（`capabilities/committee/risk_officer.py` + `capabilities/committee/cio.py` prompt 软抑制 + `core/committee/cio_parse.py` Sanity 4 硬 force-HOLD）**不变**——只是默认值翻了。

## Consequences

- 默认用户不再收到"集中度超配 → 减仓"建议；不会再因 watchlist 被当净资产而误判。
- #89 的 NaN-leg 集中度误算对默认用户**不再有 verdict 影响**（lens off 时该数字不驱动任何决策）。它仅作为 opt-in 用户的已知边界保留——后续可单独修（缺价腿用 last close / 均价计入分母，而非整条剔除），但不再是反复打补丁的压力点。
- 行为变更：相关测试改为显式开 lens 才测 on-path；`test_concentration_lens_off_by_default_forces_hold` 守新默认。

## 修订（2026-10-10）：lens 关 = 不当减仓理由，不是藏数字

原实现在 lens 关时让 `portfolio_summary_text` **不渲染**集中度、Risk prompt 令模型"不输出、不提及占比"。插件实测（虚构用户：¥50,000 现金 + ¥10,000 基金，真实占比 16.7%）暴露：Risk 模板的 `WORST_CASE_LOSS_PCT_AT_-20` 本质就是"占比 × 20%"，模型拿不到数字只能自算——编出"约占总资产 1.7%"写进压力测试，CIO 照抄成"仓位仅占 1.7%、小仓位"。这正是 2026-05-19 把数字显式渲染出来要防的事（LLM 自算集中度连错 6 天）。

改为：

- `portfolio_summary_text` **始终**渲染真实集中度；lens 关时同行追加"仅作背景/压力测试，不构成减仓理由"。该腿汇率或总资产不可用时渲染"暂不可计算"，绝不伪造 0.0%。
- Risk prompt（lens 关）：`CONCENTRATION_PCT` 照抄、`WORST_CASE_LOSS_PCT_AT_-20 = 集中度 × 20%`、禁止自算；不得以集中度升级 SIGNAL 或建议减仓。CIO prompt（lens 关）：提及占比只能照抄原值。
- 真值进 summary 后，既有 SENTINEL 覆写（`_override_concentration_in_risk_output`）对默认用户也生效。
- **Sanity 4（lens 关 + `TRIM_REASON=concentration` → force-HOLD）不变**——减仓闸仍由它硬兜底。

上文"lens off 时该数字不驱动任何决策"仍成立；改的只是可见性。#89 的缺价腿分母问题仍待单独修：cron 路径（`daily_report.py`）把缺价腿剔出总资产，其余占比会偏高；Direct / Coordinator 路径（`total_portfolio_value_cny`）按成本价兜底计入，不受影响。lens 关时这个偏高的数字只作背景，不驱动减仓。
