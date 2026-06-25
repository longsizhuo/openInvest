# ADR-020：集中度 lens 默认 OFF（concentration 改为 opt-in）

**日期**：2026-06-25
**状态**：accepted
**取代/延续**：ADR-013（trim-concentration-override）、ADR-019（移除 solvency 自动兜底，集中度只由 lens 控制）、ADR-017（config-via-API 提供运行时覆盖）

## Context

集中度（concentration）视角的隐含前提是：**用户把全部净资产都录进了 openInvest**，所以"单资产占持仓 X%"等于"单资产占身家 X%"。

实际上 openInvest 的持仓多是**自选 / watchlist**——用户只录了想跟踪的几个标的，没把所有钱写进来（例：用户净资产含 ¥4M 家族 backup，仅作破产兜底；系统里只是 ¥22 万的零花钱账户）。在这个前提下，"持仓集中度 = 风险 → 建议减仓"对默认用户就是**错的**。

过去几轮一直在修集中度的**算法**让它"算得更对"：
- ADR-019 / #84：移除 solvency 自动兜底（"兜底足→集中度不算险"在 parse 层反转 CIO，prompt 不知情，自相矛盾）。
- #89：NaN 价不再污染总资产/集中度。

但这些都是在修一个**前提就错**的功能，只会不断冒新 bug。最新一例（2026-06-25）：黄金委员会凌晨跑时 NDQ.AX（ASX/澳元腿）无新鲜报价，按 #89 被剔出 `total_assets_cny` → 黄金集中度从真实 ~60% 被动算成 **91.2%**（= 黄金/(黄金+现金)，NDQ 不在分母）→ 驱动了一个本不该有的 `TRIM −60000`。

## Decision

**`core/config/tunable.py: concentration_lens_enabled` 默认 `True → False`。** 集中度从"默认开、人人得想办法关"改为 **opt-in**：

- **默认（False）**：不因持仓集中度建议减仓——`TRIM_REASON=concentration` 无条件 force-HOLD，Risk/CIO prompt 压掉超配规则。仍保留**波动 / 回撤 / 止损 / 估值 / 压力测试 / 现金流动性**风险维度。
- **想要的人**（把全部净资产录入系统、确实关心组合集中度）显式开启：`/api/config` 设 `verdict.concentration_lens_enabled=true`，或 env `INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=true`。

既有的 3 层 disable 实现（`agents/risk_officer.py` + `agents/cio.py` prompt 软抑制 + `core/committee/cio_parse.py` Sanity 4 硬 force-HOLD）**不变**——只是默认值翻了。

## Consequences

- 默认用户不再收到"集中度超配 → 减仓"建议；不会再因 watchlist 被当净资产而误判。
- #89 的 NaN-leg 集中度误算对默认用户**不再有 verdict 影响**（lens off 时该数字不驱动任何决策）。它仅作为 opt-in 用户的已知边界保留——后续可单独修（缺价腿用 last close / 均价计入分母，而非整条剔除），但不再是反复打补丁的压力点。
- 行为变更：相关测试改为显式开 lens 才测 on-path；`test_concentration_lens_off_by_default_forces_hold` 守新默认。
