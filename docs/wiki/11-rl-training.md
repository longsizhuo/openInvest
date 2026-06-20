---
type: wiki-chapter
title: 11 — RL 训练 / Backtest / 参数搜索
tags: [backtest, optuna, paper-trade, dspy, prompt-optimization]
intent: 回测与参数搜索流程
documents:
  endpoints: []
  config_keys: []
  symbols: [PaperTradeSimulator, compute_strategy_reward]
---

# 11 — RL 训练 / Backtest / 参数搜索

> 想优化你的委员会"水平"？这章讲清楚我们怎么验证 + 优化的，**绝不是真 ML 训练**。

**See also**: [13 — 参数调优可行性诊断](13-param-tuning-feasibility.md) — 50+ 硬编码常数完整清单 + 耦合分析 + Do-Not-Tune 列表 + Optuna 回顾 + sweep 策略

---

## TL;DR：我们做的根本不是"训练"

直觉里"训练 AI"意味着 GPU + 梯度下降 + 模型权重更新。**openInvest 完全没有这些**：

- ❌ 没有 model weight 更新
- ❌ 没有 backprop / gradient
- ❌ 没有 GPU 需求
- ❌ 没有 embedding 模型
- ❌ 没有 fine-tuning

我们做的是 **"参数搜索"+"prompt 工程"**：
- LLM (DeepSeek) 当成**不可改的黑盒函数**
- 用 Optuna 自动试不同 hyperparameter（regime 阈值、alloc 比例等），找让回测账户最赚的那组
- 用 prompt 改动让 LLM 行为本身变好（这才是真正影响 reward 的地方）

---

## 概念地图

```
        ┌──────────────────────────────┐
        │  LLM (DeepSeek-v4-flash)     │   ←  不可改的黑盒
        │  ├─ Macro Strategist         │
        │  ├─ Quant Analyst            │
        │  ├─ Risk Officer             │
        │  └─ CIO                      │
        └────────────┬─────────────────┘
                     ↓ verdicts (BUY/HOLD/...)
        ┌──────────────────────────────┐
        │  Paper Trade Simulator        │   ←  纯 Python 模拟器
        │  ¥100k 起始 → 每周决策一次     │
        │  跟着 LLM verdict 模拟买卖      │
        └────────────┬─────────────────┘
                     ↓ 账户曲线 (daily values)
        ┌──────────────────────────────┐
        │  Reward Function              │   ←  纯数学
        │  = 年化 - 0.5×回撤 + alpha    │
        │    + 0.2×max(0, Sharpe-1)    │
        └────────────┬─────────────────┘
                     ↓ single number (e.g. 0.42)
        ┌──────────────────────────────┐
        │  Optuna (TPE Sampler)         │   ←  贝叶斯优化（CPU only）
        │  根据历史 (params, reward) 学   │
        │  下一组参数试什么              │
        └────────────┬─────────────────┘
                     ↑ params: regime_threshold=4.5, alloc_pct=0.2...
                     └─────→ 回到 LLM 调用，循环
```

**没有任何东西被"训练"**。Optuna 在 CPU 上做贝叶斯采样，所有"训练时间"都是**反复打电话给 DeepSeek API**。

---

## 为什么需要做这个

**问题**：openInvest 的 LLM 委员会效果好不好？不知道。直接看实盘要等几个月才能看出。

**解决**：用历史数据**回测**：
- 假设你 2024-05-13 开始用，¥100k 起始
- 让 LLM 看截至那天的数据给 verdict
- 按 verdict 模拟买卖
- 沿时间线 mark-to-market 到 2024-11-15
- 算账户总值 vs 余额宝 / 沪深 300 / 等权 buy-and-hold

如果跑赢基准 → 委员会有用；跑输 → 不如躺平。

**进阶**：用 Optuna 自动调参看能不能再赚多点。

---

## 4 阶段 pipeline

### 阶段 1：防穿越（lookahead prevention）

**问题**：回测 2024-05-13 时，LLM 不能看到 2024-05-14 之后的数据，否则等于"先知"。

**对策**：3 处必须修
1. `utils/exchange_fee.py:get_history_data()` 加 `as_of_date` 参数，DB 缓存按日期 cutoff 过滤
2. `core/committee/persist.py:_capture_macro_context()` 用 `decision_date` 而非 `datetime.now()`
3. `scripts/backtest_committee.py` 加 `--allow-lookahead` flag，默认拒绝 decision_date > 2024-06-30（DeepSeek 训练数据 cutoff，再之后的回测含 LLM 自身 lookahead bias）

**测试**：`tests/test_backtest_no_lookahead.py`

---

### 阶段 2：数据隔离（workspace）

**问题**：回测会写很多 verdict 文件，绝不能污染真实持仓数据。

**对策**：`scripts/backtest_runner.py` 在 import invest 模块**之前** monkey-patch 数据路径：

```python
import core.memory_store as _ms
_ms.MEMORY_ROOT = workspace / "memory"   # 重定向到 /tmp/xxx/memory/
import db.trades_db as _trades
_trades.DB_PATH = str(workspace / "db" / "trades.db")
```

**核心保证**：所有回测产物都在 `/tmp/xxx/` 隔离 workspace，真实 `memory/` / `db/` 一字不改。

---

### 阶段 3：Paper Trade Simulation（核心）

**用户洞察**："verdict 命中率"不重要，重要的是**"如果我每次都听 AI，账户会赚还是亏"**。

**实现**：`core/paper_trade_simulator.py`

```python
sim = PaperTradeSimulator(start_date="2024-05-13", initial_cash_cny=100_000)
for decision_date in walk_forward_dates:
    verdict = run_committee(decision_date, "NDQ.AX")  # 4 角色辩论
    sim.execute_verdict(decision_date, "NDQ.AX", verdict)  # BUY → 扣现金 + 加股数

# 沿时间线 mark-to-market
for date in trading_days:
    sim.account.daily_values.append((date, sim.mark_to_market(date)))
```

**对比 4 基准**（`scripts/run_walk_forward.py:_build_benchmark_curves`）：
1. 余额宝（年化 1.3%）
2. 沪深 300（510300.SS buy-and-hold）
3. 纯现金（不操作）
4. 等权组合（4 资产各 25% buy-and-hold）

**Reward function**（`core/backtest_reward.py`）：
```
reward = annualized_return
         - 0.5 × max_drawdown
         + 0.5 × alpha_vs_yuebao
         + 0.2 × max(0, Sharpe - 1)
```

设计意图：奖励赚钱、罚回撤、跑赢躺平基准给奖、高 Sharpe 加分。

---

### 阶段 4：Optuna 自动调参

> **想看 / 改训练参数？看 [`experiments/train_config.py`](../../experiments/train_config.py)**
> —— 单一可信源，列出所有 hyperparameter + 范围 + 含义 + 实测有效性。
>
> **`INVEST_*` env var 是内部实现细节**：Optuna trial 之间通过 env var 跨边界传
> 参数。这些**不在 `.env.example` 里**，普通用户 / fork 用户**不需要**配置——
> 代码默认就是 v0 行为（无 clamp / 1 round）。只有 `rl_train.py` 跑起来时 Optuna
> 才会临时 override，进程结束 env var 也消失。

**5 个 hyperparameter**（详细描述见 `train_config.py`）：

| 参数（CLI/Optuna） | 内部 env var | 范围 | 含义 | 实测有效 |
|---|---|---|---|---|
| `alloc_aggressiveness` | `INVEST_ALLOC_AGGRESSIVENESS` | 0.05 ~ 0.30 | 单笔 alloc 上限占 ¥100k baseline 比例 | ✅ 真有效 |
| `regime_uptrend` | (monkey-patch THRESHOLDS) | 3.0 ~ 6.0 | MA20/MA120 spread 阈值 | ⚠️ 弱 |
| `regime_atr` | (monkey-patch THRESHOLDS) | 2.5 ~ 5.0 | ATR 崩盘阈值 | ⚠️ 弱 |
| `max_rounds` | `INVEST_MAX_DEBATE_ROUNDS` | 1 ~ 3 | cross-challenge 轮数 | ❌ placebo |
| `cio_confidence_cap` | `INVEST_CIO_CONFIDENCE_CAP` | 0.7 ~ 0.95 | CIO confidence 上限 | ❌ placebo |

**Optuna 怎么搜**：TPE Sampler 跑 30 次回测，每次试一组参数，根据 (params, reward) 历史学**下一组参数往哪个方向试**。

**SQLite study**：`workspace/optuna_study.db` 存所有 trial 状态，支持断点续传 + 多进程并行。

**并行实测**：3 worker 共享 study DB，wall clock 5h → 1h（3x 加速，无 DeepSeek 限流）。

---

## 实际成果（2026-05-11 第一轮）

### v0 baseline → v1 prompt → Optuna

| 阶段 | Reward | 年化 | HOLD % | 关键 |
|---|---|---|---|---|
| **v0 baseline** | -0.0006 | 0% | **100%** | 100% HOLD 复读机 |
| **v1 prompt（现金机会成本）** | **+0.398** | +25.4% | 44% | unblock 关键 |
| **Optuna best (trial #20)** | **+0.422** | **+34.4%** | - | 30 trial 找到的天花板 |
| **Hold-out 6w 验证** | +0.288 | +17.4% | - | 泛化 OK, Sharpe 1.59 |

完整数据见 [docs/training_report.md](../training_report.md)。

### v1 prompt 改动了什么

CIO prompt（`agents/cio.py`）加 **"现金机会成本"硬规则**：

```
CONCENTRATION_PCT < 20%（仓位 < 20%，子弹 ≥ 80%）：
- 不允许 HOLD
- 默认至少 ACCUMULATE，alloc = dry_powder × 5~10%
- 豁免：Macro=risk_off AND Risk=high_risk（两个 AND）
```

**金融逻辑**：等回调 ≠ 零仓位等，是留 90% 子弹等更低位。

---

## Negative results（同样重要）

1. **Reward landscape 几乎平**（30 trial stdev 0.037 vs mean 0.40）→ 参数 hyperparameter 不是 reward 主因
2. **max_rounds=1 vs 3 reward 几乎相同** → 多轮辩论是 placebo（推测 LLM 第二轮只是 rephrase 第一轮）
3. **alloc_aggressiveness 0.06 vs 0.25 reward 也相近** → 大单 vs 小单本质同策略
4. **Reward 0.42 = 当前 prompt 架构的天花板**——Bayesian search converged，再加 100 trial 也突不破

**结论**：真瓶颈是 **prompt 内容**（哪几个 example、什么样的措辞），不是 hyperparameter。

---

## 怎么自己跑一遍

### 跑诊断（看当前 prompt 是否复读机）

```bash
cd ~/openInvest
set -a && . ./.env && set +a
rm -rf /tmp/my_diagnose

# 用 backtest_runner 包装（自动 monkey-patch 数据路径 + 预热 10y yfinance）
.venv/bin/python -m scripts.backtest_runner \
  --workspace /tmp/my_diagnose \
  --start 2024-05-13 --end 2024-11-15 \
  --assets NDQ.AX,GC=F

# 分析 verdict 分布
ls /tmp/my_diagnose/memory/.backtest/ | wc -l
grep -h '\*\*Verdict\*\*:' /tmp/my_diagnose/memory/.backtest/*/*.md | sort | uniq -c
```

### 跑 Optuna 调参

```bash
.venv/bin/python -m scripts.rl_train \
  --workspace /tmp/my_optuna \
  --train-start 2024-05-13 --train-end 2024-11-15 \
  --assets NDQ.AX,GC=F \
  --n-trials 30

# 看 best params
.venv/bin/python -c "
import optuna
s = optuna.load_study(study_name='invest_committee', storage='sqlite:////tmp/my_optuna/optuna_study.db')
print(s.best_params)
print('reward:', s.best_value)
"
```

### 跑 hold-out 验证（防 overfit）

```bash
.venv/bin/python -m scripts.holdout_validate \
  --optuna-workspace /tmp/my_optuna \
  --holdout-workspace /tmp/my_holdout \
  --start 2024-11-18 --end 2024-12-31 \
  --assets NDQ.AX,GC=F
```

### 并行加速（3-worker）

```bash
# Terminal A
.venv/bin/python -m scripts.rl_train --workspace /tmp/my_optuna --n-trials 10 ...

# Terminal B（同时启动）
.venv/bin/python -m scripts.rl_train --workspace /tmp/my_optuna --n-trials 10 ...

# Terminal C
.venv/bin/python -m scripts.rl_train --workspace /tmp/my_optuna --n-trials 10 ...
```

Optuna SQLite study 自动协调，不会重复 trial。

---

## 关键文件速查

```
# 训练 / 调参入口
scripts/rl_train.py            ← Optuna 主入口，TPE bayesian search
scripts/backtest_runner.py     ← workspace 隔离 wrapper（必经包装）
scripts/run_walk_forward.py    ← walk-forward simulation runner
scripts/holdout_validate.py    ← hold-out 验证（防 overfit）
scripts/build_dspy_trainset.py ← 给 DSPy 准备 trainset (verdict + 7d return)

# 核心模拟器 / metric
core/paper_trade_simulator.py  ← PaperAccount + execute_verdict
core/strategy_metrics.py       ← 年化 / Sharpe / Sortino / vs benchmark
core/backtest_reward.py        ← reward function (单数值)
core/committee/cio_parse.py    ← parse_cio_memo 含 INVEST_* env clamp

# 实验 archive
experiments/optuna_final_summary.json           ← 30-trial 数据
experiments/dspy_trainset_v1_2024_05_to_11.json ← DSPy 候选 trainset
experiments/prompt_variants/                    ← prompt v0/v1 archive
docs/training_report.md                         ← 完整一轮训练报告
```

---

## 安全保证

✅ **永不污染真实持仓**：所有训练 workspace 在 `/tmp/`，监控 `core.memory_store.MEMORY_ROOT`
✅ **不连真实交易**：PaperTradeSimulator 纯 Python in-memory，不调任何 broker
✅ **防穿越**：阶段 1 修了 3 个数据穿越漏洞 + 强制 LLM 训练 cutoff 拒绝
✅ **可重复**：每个 Optuna trial 用 fixed seed，trainset 落盘可重跑

---

## DSPy 阶段（scaffold 已实现，smoke 实测 +10pp）

当 Optuna 把"hyperparameter 调参"做到极限后（0.42 是当前 prompt 架构天花板），
下一个突破点是**优化 prompt 内容本身**——这正是 DSPy 干的事。

**DSPy 是 Stanford NLP 出的工具**，专门做 LLM 程序的"反向优化"——给定 trainset
+ metric，自动选择最优 few-shot examples 和 prompt 模板。

**实现状态（2026-05-11）**：
- ✅ DSPy 3.2.1 装好
- ✅ `experiments/dspy_trainset_v1_*.json` 66 样本就绪
- ✅ `scripts/rl_optimize_prompts.py` scaffold 完成
- ✅ Smoke test 通过：**baseline 0.600 → optimized 0.700（+10pp）**
- ✅ **正式 run 完成**（10 candidate × 46 train × 20 dev，66 min）：
  **baseline 0.725 → optimized 0.825（+10pp / +13.8% 相对）**
  - archive: `experiments/dspy_optimized_v1_full.json`（含 10 个 best demos）
  - summary: `experiments/dspy_optimized_v1_full.summary.json`
- ⏳ 把 optimized few-shot 注入回 `agents/cio.py` 待手工合并 → 重跑 walk-forward 验证 reward 突破 0.42

### 跑 DSPy

```bash
# Smoke (~5 min ~¥0.5, 验证 pipeline)
python -m scripts.rl_optimize_prompts \
  --trainset experiments/dspy_trainset_v1_2024_05_to_11.json \
  --output experiments/dspy_optimized_v1.json \
  --smoke-test

# 正式 (~30 min ~¥3)
python -m scripts.rl_optimize_prompts \
  --trainset experiments/dspy_trainset_v1_2024_05_to_11.json \
  --output experiments/dspy_optimized_v1.json \
  --n-candidates 10 --n-bootstrap 4
```

预期产出：突破 reward 0.42 → 0.5+（smoke 看到 dev score 已 +10pp，正式跑可能更好）。

### 当前 scaffold 的限制

当前 DSPy module 只 wrap "market context → verdict" **单步预测**，不重做 4 角色
cross-challenge。如果要更深度优化（让 DSPy 学每个 role 怎么独立 reasoning + 怎么 synthesis），
需要 refactor `agents/sdk_agent.py` 改成 `dspy.Module`，工作量 1-2 天。

当前 minimal viable 已能产生 +10pp 改进，不一定值得做大 refactor。

---

## Prompt 组织：SKILL.md 模式（已落地）

参考 [OpenClaw](https://github.com/openclaw/openclaw) 和
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的
prompt 组织模式，**已切到 SKILL.md**。

### 目录结构

```
agents/
├── skills/                          ← prompt 本体（markdown，跟代码解耦）
│   ├── cio/SKILL.md
│   ├── macro_strategist/SKILL.md
│   ├── quant/
│   │   ├── SKILL.md                 # round_label="opening"
│   │   └── SKILL_rebuttal.md        # round_label="rebuttal"
│   ├── risk_officer/
│   │   ├── SKILL.md
│   │   └── SKILL_rebuttal.md
│   ├── wealth_context_officer/SKILL.md
│   └── README.md
├── skills_loader.py                 ← load_skill() + 占位符渲染（零依赖）
├── cio.py                           ← 薄 wrapper：build_cio_prompt() = load_skill("cio", ...)
├── macro_strategist.py
├── quant.py
├── risk_officer.py
└── wealth_context_officer.py
```

### SKILL.md 格式

```markdown
---
name: cio
description: 综合 Macro/Quant/Risk 三人输出，给最终 verdict
role: cio
---

你是首席投资官 (CIO)，刚听完 ... {{asset_name}} ({{asset_symbol}}) ...
```

运行时占位符（`{{asset_name}}` / `{{asset_symbol}}`）由 `skills_loader.load_skill()` 替换。

### 改 prompt 的流程

1. 直接编辑对应 `agents/skills/<role>/SKILL.md`
2. 不动 `.py`（除非加新角色 / 新占位符）
3. `pytest tests/test_skills_loader.py` 确认 frontmatter 解析无误
4. commit 仅 .md diff —— **跟代码改动完全分离**

### 跟 OpenClaw / Hermes 对比

| 维度 | 当前 invest | OpenClaw / Hermes-Agent |
|---|---|---|
| 组织模式 | ✅ SKILL.md + frontmatter | ✅ SKILL.md + frontmatter |
| 动态字段 | ✅ `{{var}}` 占位符（零依赖）| ⚠️ jinja2 / 各自实现 |
| DSPy/GEPA 集成 | ⏳ 待对接（`SkillModule(dspy.Module)`）| ✅ 已有（`evolution/skills/skill_module.py`）|
| 改 prompt 不碰代码 | ✅ | ✅ |

### 下一步（plan 阶段 5）

参考 `NousResearch/hermes-agent-self-evolution:evolution/skills/skill_module.py`，
把 SKILL.md wrap 成 `dspy.Module`，让 `scripts/rl_optimize_prompts.py` 的 DSPy 优化
**直接读写 .md 文件**（当前需要手工把 best demos 转 .py 注入）。

---

## ADR 索引

未来这些决策会写到 `adr/`：
- ADR-008: 为什么用 Optuna 而非 PPO/DPO（LLM 离散非可微）
- ADR-009: paper trade vs verdict-level evaluation（strategy-level 信噪比高 10x）
- ADR-010: 训练数据 cutoff 选 2024-06-30（DeepSeek 自身训练数据）
- ADR-011: 暂不切到 SKILL.md 模式（见上节"Prompt 组织"权衡）
