# ADR-009: 否决 TradingAgents 式分析师 subagent 扩展

**日期**: 2026-06-11
**状态**: ✅ 已采纳（基于 test_ta 实验，预注册判定）

## 背景

TradingAgents（TauricResearch）的核心范式是多分析师（基本面/情绪/新闻/社媒）
产报告 → researcher 辩论 → trader 决策。openInvest 在 2026-06-09 对齐其数据维度时
选择了"确定性事实块进推理，不加投票 agent"（见 wiki 02/15），但该选择当时是
设计判断，未经实验。用户要求实测：加 3 个 TradingAgents 式分析师有没有收益。

## 决策

**不引入报告型/投票型分析师 subagent。** 维持现有架构：确定性事实块
（regime 概率口径 / 估值 / 情绪表盘 / 路径参考）注入 + LLM 翻译解读 + 概率表锚方向
+ parse_cio_memo 确定性后处理。

## 依据（test_ta Phase A，全文见 wiki 16）

预注册 Gate（30d 命中率 Wilson 95% CI 下界 > max(同子集方向基率, 输入数据机械映射)）：

- fundamental：50.3% ≈ 抛硬币（金侧用的是**真** CFTC COT 数据——数据本身无信号）
- news：57.4% vs 基率 73.0%（GDELT 真实头条，仅标题降级）
- sentiment：64.7%——赢了自己输入的机械映射（57.5%），**但输给"无脑看多"基率 73.1%**

0/3 过线 → 集成阶段（Phase B/C）按预注册不触发。n=244/分析师，与历史消融同款
122 个决策日，零前视，~¥10-20 成本。

## 含义与边界

- LLM 的增量在**解读**不在**方向投票**（sentiment 赢映射输基率即证）——支持
  "事实块 + CIO 否决权"路线
- 单边牛市窗口结论；熊市/震荡窗未覆盖。新闻维度留前向实验队列（事件层攒数据）
- 推翻本 ADR 的条件：某分析师在新窗口/新数据下过预注册 Gate → 跑 Phase B
  集成 A/B（代码已在 test_ta 分支备好，flag 默认关）

## 关联

- wiki [16-ta-analysts-experiment.md](../16-ta-analysts-experiment.md)（实验全文）
- wiki [12-verification.md](../12-verification.md)（核心主张实测清单）
- 既有结论"regime 是信号，verdict 是噪声"（verdict 方向 ≈57% 闭卷）与本实验互证
