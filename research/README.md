# LLM 分析师 vs 确定性基线：预注册消融复现包

> **一句话**：给 LLM 投资委员会加 TradingAgents 式分析师 agent（基本面/情绪/新闻），
> 在方向预测上**是否赢过同一份输入数据的机械 if-else 基线**？预注册答案：**否**
> （2 窗口 × 2 厂商 3 模型 × 单独/ensemble/联合，全部不过 Gate）。

这是 openInvest ADR-009 实验的去 PII 复现包。无任何个人持仓/财务数据——只有
GC=F / NDQ.AX 的公开市场分析。完整结论叙事见 openInvest `docs/wiki/16-ta-analysts-experiment.md`。

## 核心设计：基线③（本实验的方法论卖点）

多数"LLM 选股有没有用"的研究只和 50% 抛硬币或买入持有比。本实验加了第三条、
也是最严的基线：**用分析师自己看到的同一份数据，写一条确定性 if-else 映射**
（如 VIX 分位 ≥0.85→bearish；COT 净持仓 4 周变化符号）。LLM 只有打赢
`det_stance` 才算"LLM 解读层有增量"，否则它做的事一条规则就能替代。

## 预注册 Gate（跑分前写死）

分析师"有方向信号" = 30d 命中率 Wilson 95% CI 下界 > **max(① 同子集方向基率,
③ 机械映射点估计)**。跨模型 2/3 过线才认信号（防单模型×单窗口巧合）。

## 结果矩阵（30d 方向命中率）

| 窗口 × 模型 | fundamental | sentiment | news | combined |
|---|---|---|---|---|
| 2024 牛市 × mimo-flash | 50.3% | 64.7% | 57.4% | 48.7% |
| 2024 牛市 × ds-reasoner | 28.8% | 58.2% | 59.3% | — |
| 2022 熊市 × mimo-flash | **74.3%** ✓ | 48.3% | 50.9% | 66.7% |
| 2022 熊市 × ds-reasoner | 60.6% | 48.0% | 50.0% | — |
| 2022 熊市 × ds-chat | 57.8% | — | — | — |

**14 FAIL / 1 PASS（fundamental-2022-flash 单格）→ 跨模型 1/3 → 孤证不立。**
机制：bullish 表态≈基率复读；bearish 逆 regime 大势即反指标；ensemble 共识强度
与正确率不单调；联合阅读三源不涌现反而稀释。详见 SCHEMA.md + wiki 16 §5b。

## 复现

```bash
pip install pandas numpy openai     # 或 uv sync（在 openInvest 仓内）
export LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=mimo-v2.5-pro

# 1. 拉数据源（COT 已缓存在 inputs/；GDELT 头条会限流，已缓存）
# 2. 跑 Phase A（断点续跑，INVEST_TA_OUT 指定输出文件）
INVEST_TA_DATES=inputs/decision_dates_2022.json \
INVEST_TA_OUT=my_run.jsonl python scripts/ta_phase_a.py --analysts fundamental,sentiment,news

# 3. 预注册 Gate 判定
INVEST_TA_OUT=my_run.jsonl python scripts/eval_ta_signal.py
```

`data/` 下已有全部原始结果——直接 `eval_ta_signal.py` 指向它们即可复算 Gate，
无需重跑 LLM。

## 局限（投稿前须知）

- **n=2 资产 × 2 窗口**：结论限于 GC=F/NDQ.AX 这两个标的两个窗口，非普适
- 模型训练截止可能晚于回测期（lookahead 风险）——见 wiki 12 caveat
- 基线③是手搓机械映射，未与经典因子/动量 benchmark 对比
- news 仅标题无正文；fundamental-NDX 是估值代理（无免费历史 PE）

## 引用

```
openInvest TA-analyst pre-registered ablation, 2026-06.
github.com/longsizhuo/openInvest @ branch test_ta, docs/wiki/16-ta-analysts-experiment.md
```

## baselines/ — 三臂对照·免费臂（2026-06-12）

`baselines/gold_baselines.py`（预注册映射，零 LLM）：GC=F 买入持有 / 200DMA 趋势 /
production regime 确定性规则。结果 `gold_baselines_result.json`。要点：全历史
（2000-2026）买入持有 CAGR 11.1% / Sharpe 0.68 三臂最优；**regime 臂 MaxDD -54.5%
比买入持有(-44.4%)更深**——production 分类器作为黄金单独择时规则在 2011-15 熊市
whipsaw，与"MA regime 看不见快速崩盘"互证。2024+ 窗三臂无差异（基本全程持仓）。
第四臂（完整 LLM 系统）复用历史消融 draws 后补。
