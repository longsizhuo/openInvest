---
type: adr
title: ADR-023 — 诚实定位:透明/纪律/低成本被动,不是 alpha 机器
status: accepted
date: 2026-06-28
tags: [positioning, product, verification, adr]
intent: 产品定位
schema_source:
  - experiments/signal-eval/README.md
  - docs/wiki/12-verification.md
documents:
  config_keys: []
  symbols: []
supersedes: []
superseded_by: []
---

# ADR-023 — 诚实定位:透明/纪律/低成本被动,不是 alpha 机器

**日期**: 2026-06-28
**状态**: Accepted
**关联**: [12-verification](../12-verification.md) ·
        [16-ta-analysts-experiment](../16-ta-analysts-experiment.md) ·
        [022-backtest-memory-contamination-and-holdout-discipline](022-backtest-memory-contamination-and-holdout-discipline.md) ·
        [009-no-ta-style-analyst-agents](009-no-ta-style-analyst-agents.md)

## Context

经一轮系统的、预注册、扣成本、抗过拟合的研究(signal-eval,PR #112),**委员会/AI 能否产生
可交易 alpha 的问题已被实证关闭——答案是否定的**:

- **横截面选股**:Q1 单变量 rank-IC(Holm 后 p=0.397)+ M1 多变量 GBM(OOS IC +0.003,p=0.925),
  且在 survivorship 顺风宇宙上 → 委员会读的特征**无选股信号**。
- **时序择时**:Q2 发现的"黄金 MA200 上方收益更高"是 **beta**——做成可交易的 long/flat 择时后
  (匹配平均敞口、扣 0.38% 成本、DSR deflate)**输给纯被动**(终值 3.07 vs 买持 15.10、Sharpe
  +0.36 vs +0.68、回撤 −57% vs −44%,**连"躲跌"都失败**)。
- **不止黄金、不止趋势**:三资产(GC=F/510300.SS/NDQ.AX)× 4 信号族(趋势/均值回归/波动率目标/突破)
  × 网格 = 72 变体,**无一过 DSR>0.95**(正向对照作弊信号 DSR=1.00,证 harness 能识别真信号)。
- **新闻/情绪**:ta-analysts(ADR-009)三分析师全低于基率,"头条已被价格消化"。
- **委员会闭环**:主动交易跑输被动 + 傻瓜 DCA(wiki 12 Negative #1)。

委员会**唯一被测到的正向价值**是**纪律**:方向性 verdict 84% 是 HOLD、反向错仅 13.6%——即它主要
靠"不犯反向错"而非"会择时/选股"(wiki 12 主张 3、Negative #4)。

与此同时 README 首屏(line 27)仍以**"相较于 8 类基准资产的累计超额收益 (Alpha)"** 作为卖点,
而同页 line 36 已诚实披露"审计工具、非收益放大黑盒、命中率 25%、HOLD 占 84%"。**首屏与证据自相矛盾。**

## Decision

**openInvest 的定位 = 透明 / 纪律 / 低成本被动的投资伴侣,不是 alpha 机器。** 据此:

1. **不主张、不营销 alpha**(含回测与 live 的"超额收益"作为卖点)。落实"公开数据红线 / 绝不拿
   回测 alpha 营销"。首屏 PnL 区**保留净值透明披露**(展示业绩本身是透明,不是问题),但**移除
   "超额收益 (Alpha)" 的措辞与卖点框架**,改为中性的"净值 vs 基准对照(透明披露,非 alpha 主张)",
   让首屏与 line 36 的诚实披露一致。OUTPERFORM_FEED 若展示须同时含 winning+losing(红线 #3)。
2. **产品价值锚定三件可证的事**:
   - **透明/可审计**:telemetry(成本/延迟/tool calls)、committee transcript、`decision_log`、
     intervention 反事实账本——让用户看清"为什么这么决定"。
   - **纪律**:委员会的实测价值是"不犯反向错"+ HOLD 偏好;防御/sanity 规则防冲动操作。
   - **低成本被动**:平均 DCA + 核心永不卖 + 低换手,是研究中**打不过**的基线 → 把它做成诚实默认/推荐。
3. **停止为"找收益"投入**:不再做委员会选股调优、更多 vol-target/sizing 变体、趋势/regime 择时
   微调当 alpha(全已证 null)。per-asset regime 阈值的尺度无关化(issue #113)纯为 robustness/
   self-host,**不是收益项**。

## Consequences

- README 首屏 alpha 措辞改为中性透明披露(本 PR 同步改;outward-facing,经 PR review 由 owner 拍板)。
- 后续产品工作向"透明/纪律/低成本被动"倾斜:把纪律价值(拦了多少冲动操作)、审计流水做成用户能看见的;
  把平均 DCA + 永不卖做成清晰推荐路径。
- 研究结论冻结在 signal-eval + wiki 12;不再周期性烧 token 复算"委员会有没有 alpha"(确认性 theater)。
- 对 fork/self-host 用户:诚实定位降低"被当成 alpha 黑盒"的预期错配,与 self-host 分发目标一致。

## 不做什么(红线延续)

- 不拿回测/精选窗口 alpha 营销;n<30 不展示具体命中率数字;公开 URL 不含可反推持仓字段(红线不变)。
- 不把本 ADR 当作"委员会无用"——它有纪律 + 透明价值,只是**不是 alpha 来源**,定位据此校准。
