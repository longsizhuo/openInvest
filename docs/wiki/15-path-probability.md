---
type: wiki-chapter
title: 15 — 概率表路径化：多窗分布 + 四类路径形状
tags: [regime, probability, path-distribution, calibration, committee]
intent: 路径形状分布算法与校准
schema_source:
  - core/regime_probability.py:compute_regime_return_frame
  - core/regime_probability.py:get_path_profile
  - core/regime_probability.py:build_reentry_reference_text
documents:
  endpoints: []
  config_keys:
    - path.shrinkage_k
    - path.band_gamma
  symbols:
    - compute_regime_return_frame
    - get_path_profile
    - build_reentry_reference_text
---

# 15 — 概率表路径化：多窗分布 + 四类路径形状

> 实现：`core/regime_probability.py`（`compute_regime_return_frame` / `get_path_profile` /
> `build_reentry_reference_text`）。测试：`tests/test_regime_probability.py`。
> 引入版本：v0.6.0（多窗+三类形状）→ v0.6.x（四类形状 + regime 持续标注）。

## 0. 一句话

对几十年 OHLC 的**每一个历史交易日**：用 production 同款 `classify_regime` 判定当天
regime，再向前看 30/60/90 天记录**期末收益、途中最低/最高点、各自到达时间、regime
实际持续天数**——把所有"与今天同 regime"的历史日聚合成分布，注入 CIO brief。
纯算术，0 LLM token，每次委员会实时算。

设计动机：committee 的 verdict 方向预测 ≈57%（闭卷）跑不赢基率，但 **regime 间
forward return 差异显著**（见 [12-verification.md](12-verification.md)）。所以方向
由概率表锚定，LLM 负责翻译与情景化；本文档的路径化把"30 天单点分布"升级为
"带路径形状的预测"——回答用户真正的问题：**卖出后什么时候跌、什么时候涨、
途中给不给低吸点 / 更高的卖点**。

## 1. 数据与逐日 regime 判定（`compute_regime_return_frame`）

输入：`MarketStore` 本地 OHLC 全历史（GC=F 2000→今 ~6500 行，NDQ.AX 2015→今 ~2800 行）。

对每个历史日 i，point-in-time 计算（只用 i 及之前的数据，无前视）：

| 指标 | 算法 |
|---|---|
| ma20 / ma120 | close 的 20/120 日滚动均值 |
| return_30d | close[i]/close[i−30] − 1 |
| rebound_off_30d_low | close[i]/近 30 日最低 − 1 |
| price_quantile_2y | 滚动 504 交易日窗内 (closes ≤ 当日)/n 真百分位 |
| atr_pct | Wilder TR = max(H−L, \|H−prevC\|, \|L−prevC\|)，ewm(α=1/14) ÷ 当日 close ×100；无 H/L 列退化 \|ΔClose\| |

逐日调 **production 的 `classify_regime(metrics, symbol)`**——与实盘委员会同一个函数、
同一套 per-asset 阈值，保证历史分布与今天的 regime 标签口径一致。warmup 不足
（头部 ~120 行）→ unknown，不进分布。

## 2. 前向路径量（日历日对齐）

对每窗 w ∈ {30, 60, 90} **日历日**（与 verdict_review 的 "30d" 语义一致）：

```
pos[i]      = index.searchsorted(date[i] + w 天, side="left")   # 窗末当天或之后第一个收盘
fwd_w[i]    = close[pos]/close[i] − 1          # 期末收益
min_w[i]    = min(close[i+1..pos])/close[i] − 1   # 途中最深回踩
tmin_w[i]   = argmin 的交易日偏移（i+1 起算）      # 几个交易日见谷底（"什么时候跌"）
max_w[i]    = max(close[i+1..pos])/close[i] − 1   # 途中最高冲高（镜像 min）
tmax_w[i]   = argmax 的交易日偏移                  # 几个交易日见顶（"什么时候涨"）
persist_w[i]= min(regime 前向连续段长, pos[i]−i+1) # regime 在窗内实际持续的交易日数
wdays_w[i]  = pos[i] − i + 1                       # 窗内交易日数（persist 的分母参照）
```

尾部 lookahead 不足整窗的行 → 全部 NaN（绝不偷看半截窗）。
`persist` 的连续段长用倒序扫一遍 O(n) 的 run-length 得到，逐行精确 cap 到窗末，
无近似魔数。

## 3. 聚合（`get_path_profile(asset, regime)`）

取 frame 中 `regime == 当前 regime` 的全部历史日。

**每窗分布**（30/60/90 各一组）：

- `median_pct` 中位收益、`p_below` = P(期末 < 现价)、`p_down` = P(跌 > 5%)
- `p10_pct / p90_pct` 悲观/乐观 10 分位
- `downside_pct` = 20 分位（悲观情形，换算买回参考价 `现价×(1+downside/100)`）
- **`effective_n = n // 窗口天数`** —— 相邻样本重叠 w−1 天，n=1423 在 90d 窗下独立
  样本只有 ≈15；`effective_n < 10` → 输出标 ⚠样本不足。诚实折算，防重叠把置信度撑虚高

**路径形状**（90d 窗，**四类完备互斥**）：

```
单位 u  = 当日 atr_pct（自校准，无绝对百分比 magic number）
dipped = min_90d ≤ −1×u      # 途中给过"显著回踩"（≥1 倍自身日常波动）
popped = max_90d ≥ +1×u      # 途中给过"显著高点"
up     = fwd_90d > 0

先跌后涨   = dipped  ∧ up     # 等回踩能接回（持有者：浅回踩别恐慌）
直接涨     = ¬dipped ∧ up     # 没给低吸机会（等回调会踏空）
冲高回落   = popped  ∧ ¬up    # 先给更高卖点再收跌 —— 卖出时机的核心读数
一路收跌   = ¬popped ∧ ¬up    # 全程没给过像样的离场点
```

> **为什么是四类不是三类**（2026-06 口径修正）：最初三类（先跌后涨/直接涨/收跌）
> 的分类轴只有"途中最低点"，它问"途中跌没跌过"但不问"途中涨没涨过"——于是
> "一路阴跌"和"先冲 +8% 再掉头"被糊进同一个"收跌"，而后者恰恰是卖出决策最
> 关心的形状。修法 = 加镜像的 max 轴，把 ¬up 支按 popped 拆开。实测 NDQ uptrend
> 旧"收跌 27%" = 冲高回落 19% + 一路收跌 8%——收跌支里七成是"给过更高卖点"。

外加伴随统计（百分比都换算成现价价位）：

- 冲高：`pop_median_pct` / `pop_p75_pct`（高四分位）+ `days_to_peak_median`
- 回踩：`dip_median_pct` / `dip_p25_pct`（深四分位）+ `days_to_trough_median`
- **`regime_persist_median_days / window_median_days`**：窗内 regime 实际持续中位

> **为什么标 persist**（2026-06 解读防护）：形状分布按"窗口起点的 regime"分桶，
> 窗内 regime 可能切换——"先跌后涨 49%"里混着"regime 早切走了、那个涨不是本
> regime 给的"。persist 中位 / 窗中位 比值低 → 形状占比主要在说切换后的事，解读
> 权重要打折。实测：NDQ uptrend 45/64（大半窗内有效）；**GC=F downtrend 13/63**
> ——金的 downtrend 中位 13 个交易日就切走，其 90d 形状大半是切换后的行为（这也
> 解释了 downtrend 下 90d 中位收益 +3.0% 的"反直觉"）。

## 4. 输出与进决策（`build_reentry_reference_text`）

单次 OHLC 加载算完三窗 + 形状（`get_path_profile` 一次调用复用 frame），文本注入
CIO brief 的 `=== 卖出后路径 / 买回点参考 ===` 段。真实样例（2026-06-11）：

```
# 路径参考（regime=uptrend 历史 forward 路径分布；TRIM 买回点 + 持有路径预期）：
- 现价: ¥60.73
- 30d (n=1423，重叠窗口独立≈47): 跌破现价概率 35%、跌>5% 概率 9%；悲观情形(20分位) -2.1% → ¥59.46；中位 +1.4%
- 60d (n=1423，重叠窗口独立≈23): 跌破现价概率 32%、跌>5% 概率 14%；悲观情形(20分位) -2.9% → ¥58.95；中位 +2.7%
- 90d (n=1423，重叠窗口独立≈15): 跌破现价概率 27%、跌>5% 概率 15%；悲观情形(20分位) -2.6% → ¥59.15；中位 +4.3%
- 90d 路径形状（n=1423，重叠窗口独立≈15）: 先跌后涨 49% / 直接涨(无显著回踩) 23% / 冲高回落 19% / 一路收跌 8%（"显著"=途中波幅 ≥1×当日ATR；冲高回落=先给更高卖点再收跌）
- 90d 途中最高冲高: 中位 +7.1%（→ ¥65.04），高四分位 +10.5%（→ ¥67.12）；中位 48 个交易日见顶
- 90d 途中最深回踩: 中位 -3.3%（→ ¥58.72），深四分位 -6.9%（→ ¥56.53）；中位 18 个交易日见谷底
- 90d 窗内 regime 中位持续 45/64 个交易日——持续占比低则形状占比含 regime 切换成分，解读权重打折
```

约束闭环：

- CIO SKILL（`agents/skills/cio/SKILL.md`）强制 `EXPECTED_PATH` 引用这些数字
  （形状占比/回踩深度/见底时点），**禁止凭空编路径**
- TRIM 的 `REENTRY_PRICE ≥ 现价` 仍被 parse_cio_memo Sanity 5 确定性打回 HOLD
- 概率口径同源：`regime_brief` 的 STRATEGY_HINT（30d 中性概率）与本路径参考来自
  同一个 `compute_regime_return_frame`，不会互相打架

## 5. 边界与已知局限

- **graceful**：行情/计算任何失败 → reentry_reference 退化空串，不阻断委员会
- **条件分布按起点 regime 分桶**：窗内切换是事实存在的（persist 行让它可判断），
  分类本身不按"全程同 regime"过滤——那会引入存活偏差（只统计"撑满 90 天的
  uptrend"= 挑了最强的样本）
- **小样本诚实标注**：GC=F downtrend 90d 独立样本 ≈4 → ⚠样本不足；不假装精确
- **1×ATR 是定义不是调参结果**：它只影响"显著回踩/冲高"的展示分界，不进任何
  确定性 verdict 改写；要 sweep 它需先建验证口径（同 EVENT_STANCE 权重的纪律，
  见 `scripts/eval_event_stance.py` 的 INSUFFICIENT_DATA 闸门先例）

## 6. 校准闭环（path_review）与 walk-forward 基线

路径预测是可验证的——`jobs/path_review.py`（照 verdict_review 模式）：

- **决策时落快照**：committee_runner 把 CIO 看到的同一份结构化 profile 写到
  `memory/.committee/<date>/<sym>_path.json`（防代码口径漂移污染 ground truth）
- **成熟后对账**：实际 fwd 30/60/90、窗内最低/最高/到达时点、同口径形状分类
- **校准口径**：P10-P90 带覆盖率（目标 ~80%）、p_below 的 Brier vs 基率 Brier、
  形状概率分 vs uniform 0.25、见底时点 MAE
- **recompute 模式**：预测器纯确定性 → `--recompute-weekly-since` 历史逐周重算
  "当时会预测什么"（`get_path_profile(asof=...)` 零前视），不用等 90 天攒样本

**Walk-forward 校准基线（2026-06-11，2024-01 起每周一 × 2 资产，n=388）**：

| 窗 | n | 带覆盖(目标 0.8) | p_below Brier | 基率 Brier | 中位\|误差\| | 中位偏差 |
|---|---|---|---|---|---|---|
| 30d | 388 | **66%** | 0.248 | 0.220 | 4.4pp | +0.5pp |
| 60d | 357 | **62%** | 0.275 | 0.220 | 7.2pp | +2.1pp |
| 90d | 335 | **56%** | 0.240 | 0.185 | 9.6pp | +1.6pp |

形状：概率分 0.29（uniform 0.25）、top1 命中 34%、见底时点 MAE 19.1 交易日。

**诚实解读（单一牛市窗口，下同 16 章的局限）**：
1. **预测带系统性偏窄**——2024-26 实际波动（尤其黄金大涨）频繁落出历史条件分布
   的 P10-P90。读 brief 里的"悲观分位/带"时要知道它低估尾部
2. **p_below 作为概率在本窗口输给基率**——per-regime 条件分布的方向概率不如
   无条件基率校准（与 16 章"打不过基率"互证）。中位偏差为正=预测偏保守
3. 形状分布略有信息量（0.29>0.25）但远非精确；见底时点 MAE ~19 天≈低信息
4. ~~改进方向~~ **已落地（2026-06-11，校准层）**：扩窗到 2007-2026（n=1579）
   做时代分桶确认两缺陷是**结构性**（带覆盖 7 个时代 6 个 <80%；小样本桶最差
   ——GC 07-09 downtrend 覆盖 7%）→ `calibrate_profile` 两个修复：
   **小样本收缩**（λ=eff_n/(eff_n+k)，条件分布向同资产无条件分布收缩）+
   **带宽扩张**（P10/P90/downside 围绕中位 ×γ）。
   `scripts/fit_path_calibration.py` 预注册 fit(2007-17)/OOS(2018-26)：
   **OOS 三窗带覆盖 76/73/70% → 81/80/76% 全进 [75,85]，Brier 全窗改善 → PASS**，
   k=80 / γ=1.1 已写入 defaults（path 节）。caveat：p_below 与"事后基率"
   差距缩到 0.5-1.5% 但未反超（事后基率不可预知，仅参考）。
   重拟合流程：walk-forward recompute → fit 脚本重跑（recompute 快照永远存
   未校准原始分布，校准只在生产出口应用——fit 数据不会被自己污染）。
   live 快照攒到 n≥30 后启用周度 path_review job 持续追踪

## 7. 怎么验证

```bash
uv run pytest tests/test_regime_probability.py -q     # 数值精确性 + 形状完备性
```

关键测试：min/max/tmin/tmax/persist 在单调序列上的逐位精确断言；四类占比和=1；
单边上行 → 直接涨 100% 且 persist==窗长；合成锯齿（先 +8% 后跌穿）→ 冲高回落
主导收跌支。秒级真实 spot-check（零委员会重跑）：

```bash
uv run python -c "
from utils.market_metrics import compute_metrics
from utils.exchange_fee import get_history_data
from core.regime import classify_regime
from core.regime_probability import build_reentry_reference_text
df = get_history_data('NDQ.AX', '2y'); m = compute_metrics(df)
rg = classify_regime(m, symbol='NDQ.AX')['regime']
print(build_reentry_reference_text('NDQ.AX', rg, m['current_price']))"
```
