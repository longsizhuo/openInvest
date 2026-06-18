---
type: wiki-chapter
title: Sweep Runner
tags: [sweep, backtesting, parameter-tuning, regime, pnl]
intent: 参数 sweep 工具使用指南
documents:
  endpoints: []
  config_keys:
    - regime.trend_ma_spread_pct
    - regime.crash_atr_pct_min
    - verdict.buy_confidence_overdrive
    - reward.weight_max_drawdown
    - oracle_accuracy.buy_positive
  symbols:
    - run_arithmetic_sweep
    - run_pnl_sweep
---

# Sweep Runner

参数 sweep 工具，支持两种模式。

## 纯算术模式（arithmetic）

只跑 `classify_regime()` 等纯函数，不需要 LLM，秒级完成。

```bash
uv run python -m scripts.sweep_runner --mode arithmetic \
  --param regime.trend_ma_spread_pct \
  --range 2.0,8.0,0.5 \
  --train-start 2018-01-01 --train-end 2023-12-31 \
  --assets NDQ.AX,GC=F \
  --ground-truth docs/wiki/sweep_ground_truth/regime_events.yaml
```

**强制要求**：必须传 `--ground-truth` 文件（git timestamp 校验防事后追加事件）。

## P&L 模式（pnl）

跑 walk-forward paper trading，需要 LLM。每个 trial 做 sanity check + outlier 标记。

```bash
uv run python -m scripts.sweep_runner --mode pnl \
  --param reward.weight_max_drawdown \
  --range -1.0,-0.1,0.1 \
  --train-start 2024-05-13 --train-end 2024-11-15 \
  --assets NDQ.AX,GC=F
```

## 参数格式

`--param` 使用 `section.key` 格式，对应 `core/config/tunable.py` 的字段：
- `regime.trend_ma_spread_pct`
- `regime.crash_atr_pct_min`
- `verdict.buy_confidence_overdrive`
- `reward.weight_max_drawdown`
- `oracle_accuracy.buy_positive`

## Range 格式

`--range start,end,step`（如 `2.0,8.0,0.5` 生成 2.0, 2.5, 3.0, ..., 8.0）

## Sanity Check（P&L 模式）

每个 trial 跑完后自动检查：
- UNCLEAR 率 > 30% → outlier
- 单一 verdict 占比 > 90% → outlier（verdict 塌缩）
- 年化收益偏离中位数 > 3 倍 MAD → outlier

outlier 重跑 1 次，仍 outlier 则排除在汇总之外。

## 相关

- [ADR-010: 参数管理纪律](adr/010-param-management.md)
- [Ground truth 事件清单](sweep_ground_truth/README.md)
