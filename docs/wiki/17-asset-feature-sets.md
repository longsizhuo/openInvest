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
| VIX 哨兵 → INDEP_DEFENSE_FLAG 拦买入 | ✅ | ⚠️ **维持但被审查中**（验收 FAIL 维持现状；反事实记账在攒活体证据）| 同上 |
| ATR 突变比防御腿（资产级自校准）| ✅ | ✅（尺度无关，语义干净）| 同上 |
| trailing PE 估值分档 | ✅ | ❌ **不喂**（黄金无盈利；估值 brief 仅权益类出）| `utils/valuation.py` |
| CNN Fear&Greed | ✅（graceful 退化）| 跟随市场级 sentiment brief（非黄金专属信号）| `utils/sentiment.py` |
| 货币因素（DXY + TIP 实际利率代理）| ❌ | ✅（进 Macro prompt）| Macro 维度 |
| 事件层（新闻 RAG + EVENT_STANCE）| ✅（经 symbol_map 代理映射）| ✅（gold/bullion/xau 实体兜底 + 持金常驻 queries）| `services/symbol_map.py` |
| COT 持仓（CFTC 非商业净持仓）| ❌ | ❌ **暂不喂**——test_ta 实验中机械映射 2022 年 61.3% vs 基率 51.4%（未显著），挂前向验证队列 | wiki 16 §5b 残余线索 |

## 黄金 VIX 语义：当前状态（2026-06-12）

- **假设**：黄金双相（流动性挤兑期被一起卖 → 危机买盘接力），"高 VIX 拦买入"
  是股票语义错装
- **证据**：探索性全样本支持（VIX≥85 分位桶各窗中位右偏）；但 fit/OOS 预注册
  验收 **FAIL**（60d 跌破概率两时代均边际不利，右偏 + 厚左尾并存）
- **裁决**：维持现状（VIX 腿继续拦黄金买入），由反事实记账攒活体样本后复审
  （`memory/.dreams/interventions.jsonl` → `jobs/intervention_review.py`）
- 复现：`uv run python scripts/validate_gold_defense.py`（预注册判据在 docstring）

## 维护规则

1. 新特征上线时在矩阵加一行，**每个资产类别显式打 ✅/❌/⚠️**，不许默认继承
2. ❌→✅ 或 ⚠️ 状态变更需要：预注册验收脚本 + fit/OOS PASS（ADR-010 rule 4）
   或反事实记账 ≥20 条独立样本的钱口径证据
3. 与 [strategy 决策约束]、[16-ta-analysts-experiment](16-ta-analysts-experiment.md)
   的"数据进桌、LLM 不加票"原则一致：特征=确定性事实块，不是投票席位
