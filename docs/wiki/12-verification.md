---
type: wiki-chapter
title: 12 — Verification（实测验证 / "科学证据"）
tags: [verification, backtesting, experiments, negative-results, prompt-optimization]
intent: 实验验证
documents:
  endpoints: []
  config_keys: []
  symbols: []
---

# 12 — Verification（实测验证 / "科学证据"）

> 这章不讲架构，讲**用真实数据证明这工具有用**。给愿意深读的人看，给质疑"LLM
> 委员会到底比躺平好不好"的人看，也给我自己将来 review 设计是否还成立看。

---

## 这章为什么存在

读者第一次看到"4 个 LLM 互相辩论给投资建议"会有两个反应：
1. **听起来挺酷** → 想 fork 用
2. **听起来像玄学** → 跑路

这章就是给第 2 类人的——把所有"我们认为有用"的主张，**拉到回测数据上证伪/证实**。
含正向结果也含 negative results（同等重要）。

---

## 主张 1：v0 baseline 是个 100% HOLD 复读机

**实测（2026-05-11，6 个月窗口 2024-05-15~11-27, NDQ.AX + GC=F, 33 dates × 2 资产 = 66 verdict）**：

| 指标 | 实测值 |
|---|---|
| HOLD 占比 | **100% (66/66)** |
| 月度 verdict 多样性 | 1 种类型 / 月 |
| confidence 方差 | ≈ 0（固定 0.65±0.05）|
| 总收益 | 0%（账户全程 100% 现金）|
| vs 余额宝 | **-0.12%**（连躺平都跑不赢）|
| Reward signal | **-0.0006**（噪音水平）|

→ 主张证实：**未优化的 prompt 让 LLM 退化成复读机**。

---

## 主张 2：CIO prompt 加"现金机会成本"规则后 unblock

**改动**（commit `2b69c83`）：CIO prompt 加硬规则：

```
CONCENTRATION_PCT < 20%（仓位 < 20%，子弹 ≥ 80%）：
- 不允许 HOLD
- 默认至少 ACCUMULATE，alloc = dry_powder × 5~10%
- 豁免：Macro=risk_off AND Risk=high_risk（两个 AND）
```

**v0 → v1 对比（同样 6 个月窗口）**：

| 指标 | v0 baseline | v1 |
|---|---|---|
| HOLD 占比 | 100% | **44%** |
| BUY+ACCUMULATE | 0% | 56% |
| 总收益 | 0% | **+15.55%** |
| 年化 | 0% | **+25.39%** |
| Sharpe | 0 | **1.58** |
| Max drawdown | 0% | 9.0% |
| **Reward** | -0.0006 | **+0.3978** |
| vs 余额宝 | -0.12% | **+14.72%** |
| vs 沪深 300 | 0% | **+15.55%** |
| vs 等权 buy-hold | 0% | -1.65% ⚠️ |

→ 主张证实：**单点 prompt 改动让账户从持币 → 半年 +25%**。

⚠️ Caveat：**输给等权 buy-and-hold -1.65%**——委员会在牛市不如躺平四资产 25%
买死。这是当前 v1 prompt 的局限，留作下次实验。

---

## 主张 3：v1 prompt 的方向准确率不高，但"不犯大错"

**实测 v1 verdict vs 7 天后实际涨跌**（66 样本，`scripts/build_dspy_trainset.py`）：

| 命中分档 | 比例 |
|---|---|
| 方向对（+1）| 47.0% (31/66) |
| 中性（0）| 39.4% (26/66) |
| 方向错（-1）| **13.6% (9/66)** |
| **策略 hit rate** | **+33.3%** |

按 verdict 类型拆：
- `ACCUMULATE`: 37 个，**仅 38%** 方向对（LLM 不擅 7d timing）
- `HOLD`: 29 个，**59%** 方向对（横盘判定较准）

→ 主张证实：**LLM 不善 timing 但善避大错**。半年 +25% 收益 = 反向错率仅 13.6%
× 累积复利的结果，不是高命中率。

---

## 主张 4：Optuna 30-trial 找到了天花板

**实测（30 trial × 5 维参数空间，~60min wall clock 3-worker 并行）**：

| 排名 | Reward | 年化 | Sharpe | DD% |
|---|---|---|---|---|
| 🥇 #20 | **0.4223** | 34.4% | 1.37 | 15.4 |
| 🥈 #4  | 0.4223 | 34.4% | 1.37 | 15.4 |
| 🥉 #9  | 0.4205 | 29.2% | 1.56 | 10.9 |
| 🔚 #25 | 0.2621 | 20.6% | 1.28 | 10.1 |

**reward 统计**：mean=0.395, median=0.417, **stdev=0.037**（仅 1.5%）。

→ 主张证实：**reward 0.42 是当前 prompt 架构的天花板**，bayesian search converged，
hyperparameter 调不动 prompt 内容决定的上限。

**意外发现**：5 个参数里 **4 个是 placebo**（max_rounds=1 vs 3 reward 相同；
alloc_aggressiveness 0.06 vs 0.25 reward 几乎相同；regime 阈值变化也不解释）。
**只有 `alloc_aggressiveness` 真有效**（控制 SKIP 率，影响 1-2pp reward）。

---

## 主张 5：Optuna 找到的参数不 overfit（hold-out 验证）

**实测**：train 集 (2024-05-13 ~ 11-15) best 参数，在 hold-out (2024-11-18 ~ 12-31,
6 周) 跑一次：

| 指标 | Train (26w) | Hold-out (6w) |
|---|---|---|
| Reward | 0.4223 | **0.2880** |
| 年化 | +29.2% | **+17.4%** |
| Sharpe | 1.56 | **1.59** |
| Max DD | 10.9% | **2.6%** |
| vs 余额宝 | +14.0% | +1.9% |
| vs 沪深 300 | +14.7% | +2.1% |
| vs 等权 | +2.7% ✓ | -1.9% ⚠️ |

判定标准：`holdout_reward > 0.5 × train_reward` → 0.288 > 0.211 ✅

→ 主张证实：**参数泛化 OK，不是 in-sample overfit**。Hold-out 期 NDQ 震荡所以策略
变保守（BUY 2 次 vs train 12 次），但风险控制反而更好（Max DD 缩水 75%）。

---

## 主张 6：DSPy 自动调 prompt 比人手写更准

**实测**（`scripts/rl_optimize_prompts.py`）：

| Run | Train | Dev | Candidates | Bootstrap | Baseline | Optimized | Δ |
|---|---|---|---|---|---|---|---|
| Smoke | 10 | 5 | 3 | 2 | 0.600 | 0.700 | **+10pp** |
| **Full** | **46** | **20** | **10** | **4** | **0.725** | **0.825** | **+10pp** |

两次跑都见 **+10pp 稳定改善**（dev set 单步 verdict 预测准确率 60→70% / 72→83%）。

→ 主张证实：**DSPy 自动选 few-shot examples 比人手写 prompt 更准**。

⚠️ Caveat：dev set +10pp 不直接等于回测 reward 突破 0.42。需要手工把
`experiments/dspy_optimized_v1_full.json` 里的 10 个 best demos 注入
`agents/cio.py` 重跑 walk-forward 才知道。这一步**没做**。

---

## 主张 7：WealthContextOfficer 解决"低现金=高风险"误判

**问题**（2026-05-12 用户实盘报告）：
> "agent 一直说我现金太少，让我卖掉换现金。但实际我的现金 backup 还有家族，
> 这些只是零花钱。"

**根因**：Risk Officer prompt 里 `DRY_POWDER_CNY < 1000` → high_risk，
没有"off-portfolio 兜底"概念。

**实测 E2E**（同样 portfolio 上下文：cash ¥500 + NDQ 重仓 99.9%）：

| 维度 | A (无 backup) | B (¥4M backup) |
|---|---|---|
| Risk SIGNAL | `high_risk` | **`concerned`** |
| Risk STRENGTH | 10 | **8** |
| 建议动作 | 立即减仓 60% | 加仓上限 ≤¥50（10% 子弹）|
| ONE_LINER | "可用子弹仅¥500 **无补仓能力**" | "**虽家族兜底消除流动性风险**，但集中度仍高" |

**关键语义**（commit 待 / agents/wealth_context_officer.py）：
- `INVESTABLE_CASH_CNY = portfolio cash`（永远不加 backup）
- `BACKUP_BUFFER_CNY = off-portfolio 应急金`（**仅风险兜底，不可投资**）
- 家族 ¥4M 只让 SIGNAL 从 high_risk → concerned，**不让 CIO 喊"BUY ¥1M"**

→ 主张证实：**用户上下文缺失是真问题，加一个独立角色能精准修复**——LLM 推理逻辑
没错，错的是它看到的前提条件。

---

## Negative results（同等重要）

记下来防"事后选择性叙事"：

1. **委员会跑不赢等权 buy-and-hold**（v1 -1.65%，hold-out -1.9%）
   → 4 角色辩论在牛市单边涨时反而比"什么都不做的四资产 25% 买死"差
   → 暗示我们应该有"识别牛市无脑买死即可"的能力

2. **多轮辩论是 placebo**（max_rounds=1 vs 3 reward 几乎相同）
   → 之前花精力做 cross-challenge 机制可能 over-engineering
   → 或者 Round 2 prompt 让 agent 倾向"维持原判"

3. **alloc_aggressiveness 0.06 vs 0.25 reward 几乎相同**
   → 这个 hyperparameter 主要影响"SKIP 率"，不影响策略本质
   → 真正的 alpha 来自 LLM 在哪个时点喊 ACCUMULATE，不来自每次买多少

4. **ACCUMULATE 准确率仅 38%**
   → LLM 不擅长 7 天 timing
   → 长期累积仍 +25% 是因为"不犯反向错"（仅 13.6% 错），不是"会择时"

5. **TradingAgents 式分析师 subagent 无方向增量**（2026-06-11，test_ta 实验，
   预注册 Gate 0/3 过线，n=244/分析师）
   → fundamental 50.3%（真 COT 数据也 ≈ 抛硬币）、news 57.4%、sentiment 64.7%
     ——全部低于"无脑猜多数方向"基率 ~73%
   → sentiment 赢了自己输入数据的机械映射（57.5%）：LLM 的价值在**解读**不在
     **方向投票**，支持"确定性事实块 + CIO 否决权"路线
   → **2026-06-12 复测加固**（wiki 16 §5b）：2022 熊市窗口 × 2 厂商 3 模型 ×
     ensemble 三种组合，仅 fundamental-2022-flash 单格过线（跨模型 1/3，
     孤证不立）；"更强模型"假设反向证伪（reasoner 在 2024 窗口更差）
   → 全文 [16-ta-analysts-experiment.md](16-ta-analysts-experiment.md)，
     决策 [adr/009](adr/009-no-ta-style-analyst-agents.md)

6. **黄金 VIX 防御腿"语义错误"假设——验收结论被一次窗口口径 bug 翻转（2026-06-13 漂移审计）**
   （`scripts/validate_gold_defense.py`）
   → 假设：黄金双相（流动性挤兑先跌、危机买盘接力），高 VIX 拦黄金买入是
     股票语义错装（2020-03 案例：挤兑 -8.6% 后 90d +30.7%）
   → 探索性全样本（2002-2026, n≈5963）强烈支持：VIX≥85 分位桶各窗中位全面右偏
   → **2026-06-12 初判 FAIL**（60d 跌破概率两时代边际不满足）——后查明是
     **window 单位 bug**：当时用 `shift(-w)` = w **交易日**(≈1.4w 日历天)，
     与生产路径分布的 **日历天** 口径不一致。
   → **2026-06-13 漂移审计修正口径（日历天，与 forward_return 单源一致）后翻转为
     PASS**：fit/OOS × 30d/60d 四格全满足（中位右偏 + 跌破概率 ≤ 无条件），
     但 **fit-60d margin 仅 1pp（40% vs 41%），偏薄**。
   → **生产防御行为未据此改动**——是否豁免黄金 VIX 腿是独立的、需结合反事实
     记账（interventions.jsonl）钱口径证据的 gated 决策，留待另议。本条只记录
     "验收口径修正后结论翻转"这个事实。
   → 元教训：**验收脚本的 forward-return 必须用与生产同口径（日历天）**，否则
     一个 ±40% 的横轴误差就能翻转 PASS/FAIL。这正是 forward_return 单一可信源
     （`core/regime_probability.py`）要解决的根因。

---

## 怎么复现这些数据

每个主张都对应一个可复现的 pipeline：

```bash
# 主张 1-2 (诊断 + v1 改动)
python -m scripts.backtest_runner --workspace /tmp/diag \
  --start 2024-05-13 --end 2024-11-15 --assets NDQ.AX,GC=F

# 主张 3 (trainset 命中率)
python -m scripts.build_dspy_trainset \
  --workspace /tmp/diag --output /tmp/trainset.json

# 主张 4 (Optuna 30-trial)
python -m scripts.rl_train --workspace /tmp/optuna --n-trials 30 ...

# 主张 5 (hold-out)
python -m scripts.holdout_validate --optuna-workspace /tmp/optuna ...

# 主张 6 (DSPy)
python -m scripts.rl_optimize_prompts \
  --trainset experiments/dspy_trainset_v1_2024_05_to_11.json \
  --output /tmp/dspy_out.json --n-candidates 10
```

完整流程见 [`docs/training_report.md`](../training_report.md)（约 1200 行原始数据），
方法论解释见 [11-rl-training.md](11-rl-training.md)。

---

## 局限性 / 不能宣称的事

- **样本量小**：66 verdict × 2 资产 × 6 个月。不足以说"100% 适用所有市况"
- **LLM 训练数据 lookahead bias**：DeepSeek 训练截止 ~2024-06-30，之后回测含偏差
- **paper trade simulator ≠ 真实交易**：没算滑点、税费、流动性约束
- **未实盘验证**：所有结论来自模拟。真接入用户实盘后效果**可能不同**

---

## 维护规则

新加一个主张要写：
1. **实测数据**（不是 vibe）
2. **复现命令**
3. **negative result 一起公布**（avoiding survivorship bias）
4. 不要为了"好看"删 caveat
