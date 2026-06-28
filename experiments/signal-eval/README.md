# signal-eval — 委员会到底有没有 edge?(优化计划 v5 复现包)

> 冻结的可复现研究产物。本 README 自包含。方法论前提见 ADR-022(记忆穿越 + holdout 纪律)。
> ⚠ 分支:本 signal-eval 在 `research/signal-eval-harness`(off pnl-chart 分支);**闭环 CI 那套**
> (R0/R1 修复、wiki 17 closed-loop、closed-loop 实验)在 `feat/config-toggles-via-api`,两者均未并入 main
> —— 引用闭环结论处指那条线,**待统一到 main**(见末尾"待办")。数据无 PII(公开行情)。日期:2026-06-28。

## 问题(经多轮 review 重框)

不是"跑赢沪深300"。把 return 拆成两个**正交投影**,各测各的、互不 gate:
- **Q1 选股(横截面)**:committee 读的确定性特征,在几千股票横截面上能不能排序 forward return?
- **Q2 择时(时序/共有成分)**:regime 能不能预测篮子(黄金/A股/纳指)的 forward return?(用户养老定投的真实域)

配套:**M-stat**(统计闸,钉论文式)、**cutoff 探针**(LLM 回测干净性前置)、**M1**(便宜模型多变量基线)。

## 方法(钉死的严谨)
- **M-stat**(`mstat.py`):rank-IC/ICIR、Newey-West HAC t(auto 带宽,处理重叠自相关)、N_eff、
  Deflated Sharpe + 期望最大 SR(Bailey-LdP 2014 SSRN 2460551 Eq1/2,逐字核对论文)、Holm(per-family)、
  两样本桶差异(Mann-Whitney)。11 个 fixture 对 scipy/statsmodels canonical 与论文式独立复算;CI 有自动 lane。
- 0 前视:特征只用 ≤t 数据;forward 日历日对齐、尾部不成熟行 NaN。
- Q1 宇宙:当前 S&P500(501/503,yfinance)——**survivorship-biased**(Stooq 含退市被 block、CRSP 付费);
  不声明无偏,解读规则:survivorship 只抬高 IC → 不显著=稳健负。
- Q2 regime:**未调参教科书定义**(MA200 趋势 / 252日回撤压力),避开 config 里 optuna/atr 调过阈值的
  循环论证(那些阈值照 NDQ/GC 崩盘调,见 tunable.py:129)。显著性在非重叠子样本上算。

## 结果

| 测试 | 结果 | 判定 |
|---|---|---|
| **Q1 选股(单变量)** | 6 特征 mean-IC 0.025–0.067,NW-t 1.3–1.8,Holm 后 **p=0.397** | 无显著选股信号 |
| **M1 选股(多变量 GBM,OOS)** | mean OOS IC **+0.003,p=0.925** | 组合也救不了 → **选股无信号实锤** |
| **Q2 黄金 90d 趋势** | above>below MA200:中位 **+3.43% vs −0.64%,p_holm=0.016** | ✅ **唯一显著信号(时序趋势)** |
| **Q2 stress 躲跌** | 全不显著、偶反向 | de-risk 择时未显现 |
| **Q2 A股/纳指** | 方向多对但 eff_n 7–67 | 欠功效,判不了 |
| **cutoff 探针** | deepseek-v4-flash effective ≈ 2025-01 | 干净 holdout 须从 2025-06 起 |

## 结论(对"AI/委员会能不能跑赢")

1. **横截面选股:零 edge**(Q1 单变量 null + M1 多变量 OOS≈0,且在 survivorship 顺风宇宙上)。committee
   读的特征没有选股信号 → **委员会(只读这些特征 + 推理)在选股上不可能有 alpha**(必要条件已否)。
2. **时序择时:唯一信号是黄金 MA200 趋势** —— 而那是**一行确定性规则,不是 AI**。"躲大跌"未显现。
3. **委员会 vs 便宜基线**:闭环 CI(feat 分支那套)已证委员会主动交易**跑输被动 + 傻瓜 DCA**;cutoff 探针证
   那轮头部被污染、但污染只抬高业绩 → 委员会**带顺风仍输**,结论更稳。
4. → **委员会(LLM)不比便宜确定性信号多赚**。存在的 edge(黄金趋势)便宜规则就能拿;不存在的(选股)
   AI 也变不出。**诚实定位 = 透明/可审计/纪律,不是 alpha 机器**(与 README 现有口径一致)。

**M3(委员会增量)为何不再花钱跑**:横截面半边被 1 逻辑闭合(输入无信号→委员会无信号);篮子半边
power-limited(3 资产、预注册 inconclusive)且 last-night 已证输给傻瓜 DCA。再烧 token 是确认性 theater。

## 复现
```bash
cd <repo>; export INVEST_HOME=$PWD PYTHONPATH=$PWD
uv run --with scipy --with statsmodels python -m pytest experiments/signal-eval/ -q   # M-stat + Q2 单测
uv run --with scipy --with statsmodels python experiments/signal-eval/regime_forward_q2.py   # Q2
uv run --with scipy --with statsmodels python experiments/signal-eval/q1_cross_sectional.py pull && \
uv run --with scipy --with statsmodels python experiments/signal-eval/q1_cross_sectional.py compute   # Q1
uv run --with scipy --with statsmodels --with scikit-learn python experiments/signal-eval/m1_cheap_model.py  # M1
uv run python experiments/signal-eval/cutoff_probe.py   # cutoff(需 .env DEEPSEEK_API_KEY)
```
原始结果 + 决策审计在 `out/`(q1/q2/m1/cutoff json + decision_log.jsonl)。

## 局限(引用前必读)
- Q1 宇宙 survivorship-biased(当前成分);真无偏需付费源。解读靠"顺风仍 null"的不对称。
- Q2 A股/纳指历史短(eff_n 小)→ 欠功效,非"无信号"。黄金趋势是已知因子(time-series momentum),非新发现;
  raw 收益、未扣成本/风险调整。
- "绝对价位=日期指纹"是工程假设、文献未直接证(标注非已证)。
- 单一历史路径;黄金近年本就强趋势,趋势因子可能被时代放大。

## 待办(分支统一)
两条研究线分居两个未并入 main 的分支:
- `feat/config-toggles-via-api`:R0/R1 修复 + 闭环 CI + wiki 17 closed-loop + closed-loop 实验包。
- `research/signal-eval-harness`(本包):M-stat / Q1 / Q2 / M1 / cutoff 探针。

需把两者统一到 main(各开 PR 或合并),并把 wiki 17 closed-loop 补一节指向本 signal-eval 结论,
形成单一"委员会 edge"叙事。M3 委员会增量:横截面被逻辑闭合(输入无信号→委员会无信号)、篮子半边
power-limited + 已证输傻瓜 DCA,故判定为"无需再花 token 确认";若日后要正式数,跑 2025-06+ 干净窗的
篮子 committee-vs-trend(~¥10,预期 inconclusive,只为去掉污染 caveat)。
