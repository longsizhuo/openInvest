---
type: wiki-chapter
title: 17 — 资产类别特征集声明（显式契约）
tags: [asset-features, regime, gold, dca, feature-matrix]
intent: 资产类别特征集单一可信源
schema_source:
  - core/config/tunable.py:VerdictConfig
documents:
  endpoints: []
  config_keys:
    - verdict.gold_defense_dca_enabled
  symbols:
    - _gold_defense_dca_gate
    - parse_cio_memo
---

# 17 — 资产类别特征集声明（显式契约）

> 为什么有这页：特征是按"股票直觉"逐个加的，黄金等非权益资产**默认继承**了
> 整套——哪些有意义、哪些是错装语义，从来没显式声明过。2026-06-12 黄金 VIX
> 防御腿验收（见 [12-verification](12-verification.md) 负面结果 #6）暴露了这点。
> 本页是单一可信源：**给某资产类别加/删特征，必须改这里**。

## 特征 × 资产类别矩阵

| 特征 | 权益类（NDQ.AX 等）| 黄金/贵金属（GC=F）| 来源 |
|---|---|---|---|
| regime 分类（MA120/250 + ATR + 回撤）| ✅ | ✅（per-asset 阈值覆盖：trend 5.0 / crash_atr 3.5）| `core/regime.py` |
| 路径概率（regime 条件 OHLC 分布 + 校准层）| ✅ | ✅ | `core/regime_probability.py` |
| VIX 2y 分位 → 恐慌/贪婪分档 | ✅ | ✅（**语义存疑**，见下）| `utils/sentiment.py` |
| VIX 哨兵 → INDEP_DEFENSE_FLAG 拦买入 | ✅ 全拦降级 | 🔁 **改分批 DCA**（2026-06-13 裁决）：不全拦，放行 1/3、≥5 交易日/批、≤3 批/20 交易日窗；见下 | 同上 |
| ATR 突变比防御腿（资产级自校准）| ✅ 全拦降级 | 🔁 **同走分批 DCA**（与 VIX 腿 OR 成单一计划，2026-06-13 裁决）| 同上 |
| trailing PE 估值分档 | ✅ | ❌ **不喂**（黄金无盈利；估值 brief 仅权益类出）| `utils/valuation.py` |
| CNN Fear&Greed | ✅（graceful 退化）| 跟随市场级 sentiment brief（非黄金专属信号）| `utils/sentiment.py` |
| 货币因素（DXY + TIP 实际利率代理）| ❌ | ✅（进 Macro prompt）| Macro 维度 |
| 事件层（新闻 RAG + EVENT_STANCE）| ✅（经 symbol_map 代理映射）| ✅（gold/bullion/xau 实体兜底 + 持金常驻 queries）| `services/symbol_map.py` |
| COT 持仓（CFTC 非商业净持仓）| ❌ | ❌ **暂不喂**——test_ta 实验中机械映射 2022 年 61.3% vs 基率 51.4%（未显著），挂前向验证队列 | wiki 16 §5b 残余线索 |

## 黄金防御语义：分批 DCA（2026-06-13 裁决，取代旧"全拦"）

- **假设**：黄金双相（流动性挤兑期被一起卖 → 危机买盘接力）。所以"高 VIX 全拦买入"
  既不对（中位右偏，典型涨势踏空）也不能全放（厚左尾，挤兑深坑真实且 fit 期更深）。
- **这条防御腿保护我免于什么**（用户 §0-C 裁决）：**高 VIX 期的流动性挤兑深跌（左尾
  风险）**，不是亏的频率、也不是踏空典型上涨。两个原统计判据（p_below / 中位右偏）
  **都不采纳**——它们测的是"涨跌频率"，不是"左尾保护"。
- **动作**：高 VIX/ATR 防御触发时，黄金买入**不再全拦，改强制分批 DCA**——放行
  ⌈意图×1/3⌉ 一批、两批至少隔 5 个交易日、滚动 20 交易日窗内最多 3 批；未满
  spacing/quota 的批暂拦为 HOLD。VIX 腿（市场级）与 ATR 腿（资产级）已 OR 成单一
  `defense_flag_on`，故**合成单一分批计划**（非各腿独立分批，否则叠成 1/9）。
- **参数性质**：N=3 / 1/3 / 5 交易日 = 机制选定的圆整值，**不是数据优化**——左尾信号
  仅建立在 ~6-20 个样本上、统计脆，方向靠机制（黄金双相）而非这组数字。将来靠反事实
  账本（`memory/.dreams/interventions.jsonl`，rule=`defense_gold_dca_*`）验证"分批 vs
  一次性"在深坑里省多少、典型涨势里踏空多少后再调。
- **代价（诚实标注）**：典型情况会"付踏空学费换挤兑坑保护"——这是裁决接受的交易。
- **可逆**：`verdict.gold_defense_dca_enabled=false` 即退回旧全拦行为（dataclass 默认
  即关）。配置见 `core/config/defaults.yaml`，代码 `core/committee/cio_parse.py:parse_cio_memo`
  + `core/runner/intervention.py:_gold_defense_dca_gate`。
- 旧验收脚本 `scripts/validate_gold_defense.py`（测"全拦"是否 PASS）已是历史口径——
  裁决不再以"翻 PASS/FAIL"为准，改以账本钱口径长期验证（账本视角下"全拦 vs 分批 vs
  不拦"是三臂对比）。背景见 wiki 12 #6、wiki 18 §5。

## 维护规则

1. 新特征上线时在矩阵加一行，**每个资产类别显式打 ✅/❌/⚠️**，不许默认继承
2. ❌→✅ 或 ⚠️ 状态变更需要：预注册验收脚本 + fit/OOS PASS（ADR-010 rule 4）
   或反事实记账 ≥20 条独立样本的钱口径证据
3. 与 [strategy 决策约束]、[16-ta-analysts-experiment](16-ta-analysts-experiment.md)
   的"数据进桌、LLM 不加票"原则一致：特征=确定性事实块，不是投票席位
