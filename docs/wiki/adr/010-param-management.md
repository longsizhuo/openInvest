---
type: adr
title: "ADR-010 — 参数管理纪律"
tags: [config, params, sweep, governance]
intent: 参数分层纪律
documents:
  endpoints: []
  config_keys: []
  symbols: []
status: proposed
date: 2026-05-27
supersedes: []
superseded_by: []
---

# ADR-010 — 参数管理纪律

**日期**: 2026-05-27
**状态**: Proposed
**关联**: [007-few-shot-retirement](007-few-shot-retirement.md) ·
        [008-caution-insight-deferred](008-caution-insight-deferred.md) ·
        [13-param-tuning-feasibility](../13-param-tuning-feasibility.md)

## 背景

openInvest 有 50+ 个硬编码参数，分三类：
1. **可调参数**（tunable.py）：走 YAML/CLI/env 注入链，sweep runner 可自由搜索
2. **锁定参数**（locked.py）：代码直接读源码，注入链完全不碰，改需要新 ADR
3. **工程参数**（不进 config）：重试/超时/tool 轮次，在 locked.py 里但改动无收益

之前没有统一的参数管理纪律，参数散落在 6 个文件里，调了什么、为什么调、
有没有验证全靠记忆。locked.py 用代码结构强制纪律，不靠注释提醒。

## 决策

### 规则 1：谁可调谁不可调

- `core/config/tunable.py` 里的参数：**可调**。sweep runner 可以自由搜索。
- `core/config/locked.py` 里的参数：**不可调**。代码从 locked.py 读值，注入链
  （YAML/CLI/env）在物理上无法覆盖。改动需要新开 ADR，说明为什么推翻原设计承诺。

> **2026-06-17（ADR-017）**：注入链在 env 之后追加了一层**持久 API override**
> （`memory/.state/config_overrides.json`，最高优先级），让 tunable 里的**白名单子集**
> （`API_SETTABLE`）能经 `GET/PUT /api/config` + `skill config` 运行时配置——"配置走 API，
> env 退为 bootstrap 默认"。白名单只放用户安全的行为开关；内部 sweep 阈值不暴露（守本 ADR
> rule 4）。详见 [017-config-via-api](017-config-via-api.md)。`locked.py` 仍既不进 env 也不进 API。

### 规则 2：Sweep 必须 train + holdout 双指标

任何参数 sweep 的结果，必须同时报告 train set 和 holdout set 的指标。
只报告 train reward 的 sweep 结果**无效** — 不能合并到 defaults.yaml。

过拟合判定：
- P&L-based：train → holdout reward 缩水 > 50% = 过拟合
- 纯算术：train 最优阈值在 holdout 上的 crash 召回率 < 80% = 过拟合

### 规则 3：locked.py 改动需要新 ADR

要改 locked.py 里的任何参数，必须：
1. 新开 ADR，说明为什么原设计承诺不再成立
2. 提供数据证据（不是直觉/感觉）
3. ADR 被 Accepted 后才能改 locked.py

### 规则 4：Sweep 结果合并到 defaults.yaml 必须有 OOS 验证

sweep 找到的最优参数要写回 defaults.yaml，必须满足：
- P&L-based：holdout set 上的 reward > train set reward 的 50%
- P&L-based：holdout set 上的 Sharpe > 0.5
- 纯算术：holdout 上的 crash 召回率 ≥ 80%
- 参数值在 train 和 holdout 上的最优值差距 < 30%（参数稳定）

### 规则 5：Prompt 改动优先于参数改动

training_report 已证明"prompt 内容是真天花板，hyperparameter 顶不破"。
如果 sweep 找不到显著改善，应该先考虑 prompt 改动。

**明确范围**：本条"prompt 改动"指修改 `agents/*/` 下的 SKILL.md 或 prompt 文本，
**不含** few-shot demos（ADR 007 已钉死 CIO zero-shot，few-shot 路线已验证不通）。
不允许以"prompt 改动"为由重启 few-shot 实验。

### 规则 6：纯算术 sweep 必须用事前定义的 ground truth

纯算术 sweep 的目标函数（如 regime 分类准确率）必须基于**事前 commit 的
ground truth 事件清单**评估，不能事后追加事件。

ground truth 文件放在 `docs/wiki/sweep_ground_truth/`，必须在 sweep 运行前
commit（git timestamp 强制校验）。sweep 后不能修改 — 要加事件，新开文件。

理由：纯算术 sweep 不用 LLM 不代表没有 reward hack 空间。如果 ground truth
是事后定义的，等于在训练集上过拟合阈值。

### 规则 7：P&L 模式 sweep 必须做 trial sanity check

每个 P&L trial 跑完后必须做 sanity check：
- UNCLEAR 率 > 30% → outlier
- 单一 verdict 占比 > 90% → outlier（verdict 塌缩）
- 年化收益偏离中位数 > 3 倍 MAD → outlier

outlier trial 标记后重跑 1 次。仍 outlier 则排除在汇总之外。
sweep 结果报告必须标注 outlier 数量。

理由：Phase 1.5 实测有 7% Quant 抽风率，13 个 trial 大概率有 1-2 个 outlier。

## 证据

- Optuna 30-trial: 5 个参数都是 placebo（reward stdev/mean = 9.4%）
- training_report: v1 prompt 改动 reward 0 → 0.4，Optuna 只能搜到 0.42
- ADR 007: few-shot v1-v4 全部失败，CIO 保持 zero-shot
- ADR 008: caution lift-based 试金石验证

## 边界

- 本 ADR 不约束 DSPy / prompt 工程的参数（那条路由 ADR 007 管）
- 本 ADR 不约束交易摩擦参数（银行/券商费率表决定，非校准对象）
