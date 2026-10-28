# LLM 分析师 vs 确定性基线：预注册消融复现包

> **一句话**：给 LLM 投资委员会加 TradingAgents 式分析师 agent（基本面/情绪/新闻），
> 在方向预测上**是否赢过同一份输入数据的机械 if-else 基线**？预注册答案：**否**
> （跨模型未达到 2/3 门槛；单格存在 1 PASS，见下表）。

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

## 离线评分复现（无需 LLM）

从仓库根运行：

```bash
pip install pandas numpy
python experiments/ta-analysts/scripts/eval_ta_signal.py \
  --reports experiments/ta-analysts/data/2022_mimo-flash.jsonl \
  --prices /path/to/frozen_closes.csv
```

`frozen_closes.csv` 必须含 `date,symbol,close`，覆盖报告涉及的资产与决策日及之后
60 个日历天；日期唯一、价格为正数，复权/期货连续合约口径必须与原实验相同。
脚本打印两个输入的 SHA256，不自动下载行情，不读取个人数据库。
**本包目前没有评分所用的完整冻结行情/标签，因此仅凭已提交文件尚不能复算
14 FAIL / 1 PASS。** `inputs/xauusd_daily_1966_2026.csv` 不能替代 GC=F 与 NDQ.AX
原始标签。结果矩阵是历史记录，尚未通过本次独立重算。

Phase A 的历史脚本仍依赖 openInvest 安装、本地 MarketStore、dotenv 与生产 LLM
配置，以及 `memory/.ta_experiment/` 缓存；不是只安装三个库即可重跑的独立包。
新采样应在隔离研究目录进行，记录模型版本、prompt、采样参数与输入哈希。

`coverage` 在 TA 评分中仅指非 neutral 表态比例；与 P10–P90 预测区间覆盖率不同。
原 Gate 的机械基线在自身非 neutral 子集上计算，与 LLM 不一定同分母；共同子集
仅报告一致率。保留该历史 Gate，不把它改写成配对显著性检验。周频 30/60 日标签
重叠，Wilson 区间未作相关性修正。零收益不属于 bullish 或 bearish 命中。
投稿证据审计与待补清单见 [submission-audit](../submission-audit/README.md)。

## 局限（投稿前须知）

- **n=2 资产 × 2 窗口**：结论限于 GC=F/NDQ.AX 这两个标的两个窗口，非普适
- 模型训练截止可能晚于回测期（lookahead 风险）——见 wiki 12 caveat
- 基线③是手搓机械映射，未与经典因子/动量 benchmark 对比
- news 仅标题无正文；fundamental-NDX 是估值代理（无免费历史 PE）

## 引用

```
openInvest TA-analyst pre-registered ablation, 2026-06.
github.com/longsizhuo/openInvest @ main:experiments/ta-analysts/, docs/wiki/16-ta-analysts-experiment.md
```
（实验**活代码**仍在 `test_ta` 分支；本目录是冻结的去 PII 复现包。见 `experiments/README.md` 的 3 角色约定。）

## baselines/ — 三臂对照·免费臂（2026-06-12）

`baselines/gold_baselines.py`（预注册映射，零 LLM）：GC=F 买入持有 / 200DMA 趋势 /
production regime 确定性规则。结果 `gold_baselines_result.json`。要点：全历史
（2000-2026）买入持有 CAGR 11.1% / Sharpe 0.68 三臂最优；**regime 臂 MaxDD -54.5%
比买入持有(-44.4%)更深**——production 分类器作为黄金单独择时规则在 2011-15 熊市
whipsaw，与"MA regime 看不见快速崩盘"互证。2024+ 窗三臂无差异（基本全程持仓）。
第四臂 `baselines/gold_fourth_arm.py`（复用 2026-06-04 B 链消融 draws，零新 LLM）：

| 窗口 | LLM 委员会 | 买入持有 | 200DMA | regime |
|---|---|---|---|---|
| 2024-26 牛市 | +43.9%/1.82 | +42.4%/1.74 | 同左(全程满仓) | 同左 |
| 2022 熊市重放 | **+1.9%/0.20** | -0.4%/0.05 | -6.5%(whipsaw) | -5.7% |
| 2020 COVID 重放 | +13.9%/0.73 | **+24.4%/1.13** | +12.3% | +24.4% |

要点（单 draw，35-45% 翻转噪声，不可下强结论）：三窗无一臂占优；委员会 ≈
买入持有 ± 窗口噪声——2022 微胜（避开 whipsaw），2020 明显跑输（V 型里
6 个离场周代价 ~10pp，与"V 型里拦多/卖出有真实成本"互证）；两个确定性
择时臂在两个重放窗都输给买入持有。黄金单资产上，买入持有仍是最硬的基线。
注意：各窗 _s1/_s2 文件是主跑分片（verdict 逐位同，已核验），非独立 draws。
