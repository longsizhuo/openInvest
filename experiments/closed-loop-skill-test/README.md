# 闭环 skill 测试:委员会有没有"择时 skill"(ADR-022 T3 复现包)

> 冻结的可复现产物。结论叙事在 `../../docs/wiki/17-closed-loop-skill-backtest.md`(本目录只放方法+数据+脚本)。
> 数据已去 PII(回测用中性/模拟组合,无真实持仓)。日期:2026-06-27。

## 问题
回测里委员会 portfolio 上下文硬编码中性空桩(ADR-022 T3)→ Risk 永远看不到真实仓位 →
从不减仓 → 无"决策"可言 → skill 无从测。本实验:**给委员会喂"它自己决策累积出来的真实仓位",
对照空桩,看 skill 是否显现。**

## 方法
| | 基线(baseline) | 闭环(closed-loop) |
|---|---|---|
| portfolio 上下文 | 中性空桩(`run_one_day` 默认) | 每日喂模拟器**当前真实仓位**(持仓/浮亏/集中度/现金) |
| 实现 | `holdout_perf.py` 读已烧 verdict → 模拟器 | `holdout_closed_loop.py`:`run_one_day(portfolio_summary_override=…, out_subdir=…)` 闭环 |
| 窗口 | holdout 2025-01-01..2026-03-20(post-cutoff,无记忆穿越) | 同 |
| 频率 | 日频 verdict(951) | 周频决策(顺序,状态依赖)|
| 基准 | 同资产等权 buy-and-hold(T2 正确基准,非含 AAPL 的幸存者篮) | 同 |
| 防穿越 | 仓位只由过去决策构成;summary 用 as-of-d 价 | 同 |

## 结果
| 指标 | 基线(空桩) | 闭环(真实仓位) |
|---|---|---|
| 总收益 | +41.38% | +31.32% |
| MaxDD | 13.32% | **11.30%** |
| Sharpe | 1.63 | 1.47 |
| **alpha vs 同资产 buy-hold** | +11.57%(**执行假象**) | **−2.58%**(真实) |
| TRIM 执行 | ~0 | **16** |
| BUY 执行 | 2 | 36 |
| verdict 分布 | ACCUMULATE 380 / TRIM 8 / HOLD 563 | ACCUMULATE 36 / TRIM 16 / HOLD 140 |

**两个关键判读**:
1. 基线 +11.57% alpha 是**执行假象**:委员会 day1-2 把 ¥100k 现金买进 510300+黄金,01-07 想买 NDQ 时没钱→SKIP,**碰巧没拿到后来跑输的 NDQ**→赢了等权基准。不是决策。
2. 闭环修复成功(委员会看到真实仓位→TRIM 16、MaxDD↓),但**真实 alpha = −2.58%(跑输 buy-hold)**:牛市里它的减仓=卖飞。**少亏的代价是少赚** = 防御型策略签名,不是正 skill。

## 复现
```bash
cd <repo root>; export INVEST_HOME=$PWD PYTHONPATH=$PWD INVEST_MAX_DEBATE_ROUNDS=1
# 基线业绩(读已烧 holdout verdict,不重跑):
uv run python experiments/closed-loop-skill-test/scripts/holdout_perf.py
# 逐季立场 vs CMB:
uv run python experiments/closed-loop-skill-test/scripts/holdout_quarterly_stance.py
# 闭环全量(顺序,~数小时;写 memory/.backtest_closedloop/):
uv run python experiments/closed-loop-skill-test/scripts/holdout_closed_loop.py
```
`data/` 已存原始 transcript(baseline 955 + closed-loop 192)+ 闭环运行日志,免重烧。

## 局限(预注册诚实声明,引用前必读)
- **窗口几乎全是上行市**(2025-2026)→ 没有大回撤给 de-risk skill 发挥;防御性在熊市也许才值钱,本窗口证不了。
- **MiMo 有温度无 seed** → 单次蒙特卡洛,非点估计;严谨应同窗口重跑 ≥5 次报 CI(未做)。
- **闭环少数委员会 portfolio 上下文降级**("Risk 数据不可用")→ 那几日不建仓,轻微低估活跃度。
- holdout n≈14 个月单宏观路径;基线仅 2 笔实际成交 → 业绩对初始建仓极敏感。
- 模型训练 cutoff(2024-12-31)是 MiMo 自报非实证;若实际更晚,holdout 头部可能被污染(ADR-022)。
