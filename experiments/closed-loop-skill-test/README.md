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

⚠ **2026-06-27 修订**:初版含执行 bug —— `paper_trade_simulator` 的 `alloc_cny<=0` 守卫把所有
TRIM(负 alloc)吞成 HOLD → SELL 从不成交,委员会"减仓"全是 no-op。已修(R0:`qty_cny=abs(alloc)`
+ `tests/test_paper_trade_simulator_trim.py`)。初版的 +11.57% / −2.58% **作废**,下面是修复后。

### 基线(空桩),R0 修复前后
| 指标 | 修复前(buggy) | 修复后 |
|---|---|---|
| BUY / SELL 执行 | 2 / 0 | 12 / 6 |
| 总收益 / Sharpe | +41.4% / 1.63 | +21.8% / 1.03 |
| **alpha vs 同资产 buy-hold** | +11.57%(双重假象) | **−8.06%** |

初版 +11.57% 是双重假象:① TRIM 被吞→从不卖;② 现金 day1-2 花光、碰巧没买跑输的 NDQ。
修复后真相:**委员会主动交易(买+卖)跑输持有 8 个百分点。**

### A/B 标尺(对照过拟合)
| 基准 | alpha vs buy-hold |
|---|---|
| buy-hold(满仓持有) | 0%(定义) |
| 傻瓜加速 DCA(`dumb_dca.py`,无委员会机械投) | **−1.15%** |
| 委员会空桩基线(R0 修后) | **−8.06%** |

→ 委员会主动交易**连机械 DCA 都跑不赢**。

### 闭环 ±R3-vol(CI,n=5+5,DeepSeek-v4-flash 2026-06-27)
10 个闭环并行:**5× 带 R3-vol**(conditional vol-target sizing,`INVEST_VOL_TARGET=1`)+ **5× R0-only**。

| 配置 | alpha vs 同资产 buy-hold | 范围 |
|---|---|---|
| 闭环 R0-only(no vol) | **−6.04% ± 1.70** | [−8.66, −4.05] |
| 闭环 + R3-vol | **−4.88% ± 1.63** | [−7.48, −3.44] |
| 差(vol − no-vol) | **+1.16%**(t≈1.10, **p≈0.30 不显著**) | 区间重叠 |
| 对照:傻瓜 DCA | −1.15% | — |
| 对照:buy-hold | 0%(定义) | — |

**铁律判定:R3-vol 必须显著跑赢傻瓜 DCA 的 −1.15% 才算 sizing skill → 失败。**
① vol-target 只把 alpha 从 −6.04% 挪到 −4.88%,差 +1.16% 但 **n=5 下不显著**(范围全重叠);
② 即便最好的一跑(−3.44%)也输给傻瓜 DCA(−1.15%),整个 vol 分布都在 −1.15% 下方;
③ 10 跑里 TRIM/SELL 合计 2 次(给了真实仓位委员会**仍几乎不减仓**)→ 无择时 skill 实锤(ADR-022 T3)。

> **不 promote 到 live**。给委员会真实仓位上下文(闭环 −6.04% vs 空桩 −8.06%)+ vol-target sizing
> 都只是把"亏多少"减轻,改不了"主动委员会跑输被动持有/机械 DCA"这个底色(本上行窗口)。

### 真因(修复后归因)
1. **欠配 / 现金拖累**:140 次 HOLD + ACCUMULATE 单笔太小(10万本金才投 1200-6300)→ 子弹拖 9 个月才投完。
2. **牛市减仓 = 卖飞**:TRIM 真执行后,上行市里卖出拉低收益。

## 复现
```bash
cd <repo root>; export INVEST_HOME=$PWD PYTHONPATH=$PWD INVEST_MAX_DEBATE_ROUNDS=1
# 基线业绩(读已烧 holdout verdict,不重跑):
uv run python experiments/closed-loop-skill-test/scripts/holdout_perf.py
# 逐季立场 vs CMB:
uv run python experiments/closed-loop-skill-test/scripts/holdout_quarterly_stance.py
# 闭环(顺序~数小时;RUN_TAG 写独立目录,INVEST_VOL_TARGET=1 开 R3-vol):
RUN_TAG=v1 INVEST_VOL_TARGET=1 uv run python experiments/closed-loop-skill-test/scripts/holdout_closed_loop.py
# 傻瓜加速 DCA A/B 基准(确定性,秒级):
uv run python experiments/closed-loop-skill-test/scripts/dumb_dca.py
# CI:并行起 5× vol(RUN_TAG=v1..v5 INVEST_VOL_TARGET=1)+ 5× R0-only(n1..n5),grep 各 log alpha 算均值±std
```
`data/` 已存原始 transcript(baseline 955 + closed-loop 192)+ 闭环运行日志,免重烧。

## 局限(预注册诚实声明,引用前必读)
- **窗口几乎全是上行市**(2025-2026)→ 没有大回撤给 de-risk skill 发挥;防御性在熊市也许才值钱,本窗口证不了。
- **LLM 有温度无 seed** → 单次蒙特卡洛;故闭环 ±R3-vol 各跑 5 次报 CI(2026-06-27,DeepSeek-v4-flash;
  MiMo 免费额度耗尽后切付费 DeepSeek)。n=5 下 +1.16% 的 vol 增益不显著,加大 n 也许能定阈但本窗口 alpha 已确定为负。
- **闭环少数委员会 portfolio 上下文降级**("Risk 数据不可用")→ 那几日不建仓,轻微低估活跃度。
- holdout n≈14 个月单宏观路径;R0 修后基线 12 买 6 卖,成交仍偏少 → 业绩对建仓节奏敏感。
- 模型训练 cutoff(2024-12-31)是 MiMo 自报非实证;若实际更晚,holdout 头部可能被污染(ADR-022)。
