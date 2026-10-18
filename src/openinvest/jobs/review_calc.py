"""verdict/path review 统计纯核（域绑定纯模块，ADR-026）

从 jobs/verdict_review.py 与 jobs/path_review.py 拆出的纯计算：命中判定、
分桶统计、路径形状分类、校准汇总。全部函数吃传入的 reviews 列表 / 数值，
零 IO——行情拉取（_closes/_window_return/_detect_macro_shock/realized_path）
与文件读写留在各自 shell。

两个 summarize 因重名改为 summarize_verdict_reviews / summarize_path_reviews；
shell 各自 `import ... as summarize` 保持历史调用面。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from openinvest.calc.symbols import is_closed_weekend

# =====================================================================
# verdict_review 纯核
# =====================================================================

# 命中率窗口：天级 / 周级 / 月级，给短期+中期反馈
HIT_WINDOWS = [1, 7, 30]


# 记忆穿越 cutoff：决议日 ≤ 此日 = 落在 LLM 训练知识窗口内，"预测"实为记忆回放（非业绩）。
# 单一可信源（机器强制，不靠记忆）——backtest_committee 落盘 `**Contaminated**` 标记 + 本文件
# 分桶都 import 这个常量，绝不让两处各自硬编码 "2024-12-31" 漂移（见 CLAUDE.md 机器强制原则）。
# 2026-07-22 上调:运行时模型实为 deepseek-v4-flash(billing+get_llm_config 双确认),
# 实测自报训练截止 2025-05(2026 事件探针"不知道")→ 取自报月末,保守向后。
# 旧值 2024-12-31 锚定 MiMo 自报(2026-06-25 实测)。换模型必须重验(ADR-022 更新节)。
CONTAMINATION_CUTOFF = "2025-05-31"


# verdict → 期望方向
EXPECTED_DIRECTION = {
    "BUY": "up",
    "ACCUMULATE": "up",
    "SELL": "down",
    "TRIM": "down",
    "HOLD": "flat",  # 期望波动小
}

# CIO 输出解析不出合法裁决（UNCLEAR）：没有期望方向，**不进任何命中率桶**，只计数。
# 2026-10 之前它被 .get(verdict, "flat") 折进 HOLD 桶（live 12 条 + 舰队 14%），把 HOLD 命中率灌水。
UNCLEAR_DIRECTION = "none"
VERDICT_BUCKETS = ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL", "UNCLEAR"]


@dataclass
class VerdictReview:
    """单次 verdict 的事后评估"""
    date: str
    asset: str
    verdict: str
    confidence: float
    expected_direction: str
    macro_at_decision: Dict[str, float]
    actual_returns: Dict[str, float] = field(default_factory=dict)  # {"1d": 0.012, "7d": -0.034, ...}
    hits: Dict[str, bool] = field(default_factory=dict)  # 同上 key
    macro_shock: Dict[str, Any] = field(default_factory=dict)  # 事后 macro 突变标记
    regime_at_decision: Optional[str] = None  # 决议日 regime（crash 样本供下游免责，留痕不删）
    directions: Dict[str, str] = field(default_factory=dict)  # 每窗口原始市场方向 up/down/flat（verdict 无关；给 Dreaming 算 regime 基率 + caution lift）
    source: str = "live"  # "live" 或 "backtest"
    contaminated: bool = False  # 决议日 ≤ CONTAMINATION_CUTOFF：落在 LLM 训练窗口，记忆穿越非业绩
    weekend_dup: bool = False  # 周末休市资产（FX/加密除外）的周末决议：基准=周五收盘，是周五样本的重复，不进命中率（D8）
    input_health: Dict[str, Any] = field(default_factory=dict)  # transcript 的 Input Health 块（老存档 {}）
    # ---- 反事实账本（Track A3，2026-10）：同窗口基准 + 扣往返成本的"照做 vs 不做" ----
    alloc_cny: float = 0.0                 # CIO 建议金额（基准币种；买正卖负；HOLD 0；老存档符号不可信，方向看 verdict）
    alloc_currency: str = ""               # 金额币种（transcript 的 "Suggested allocation XXX" 标签；老存档 ""=CNY）
    benchmark_symbol: str = ""             # 组合基准（config portfolio.benchmark_symbol）
    benchmark_returns: Dict[str, float] = field(default_factory=dict)  # 基准同窗口收益
    excess_returns: Dict[str, float] = field(default_factory=dict)     # 标的 − 基准（verdict 无关）
    followed_pnl: Dict[str, float] = field(default_factory=dict)       # 照做 vs 现金的净损益（基准币种）
    cost_rate: float = 0.0                 # 该市场往返成本率（小数）


# HOLD "没动" 的判定阈值 = K_FLAT × 资产日波动(atr_pct) × sqrt(窗口天数)，再封顶。
# 设计（2026-05-26，与用户讨论后定）：
# - 不写死 3%——黄金一周波动 ~1%、纳指 ~1.8%、加密 ~3-5%，统一阈值对黄金太松、
#   对加密太紧。改成"小于该资产平时这段时间正常会动的幅度"才算 HOLD 命中。
# - 适应的是**测量值**（atr_pct 实时从行情算），固定的是**规则**（K_FLAT 常数）——
#   绝不让系统自学习这把"给自己打分的尺子"，否则会 reward hacking（把及格线挪低）。
# - sqrt(天数) 是随机游走的标准波动随时间缩放（日波动 → 窗口波动）。
# - 封顶 FLAT_CEILING_PCT 防 atr 异常时尺子失控。
K_FLAT = 1.0


FLAT_CEILING_PCT = 8.0


def _flat_band(atr_pct: float, window_days: int) -> float:
    """HOLD "没动" 的阈值，返回小数（如 0.026 = 2.6%）。"""
    import math
    band_pct = min(K_FLAT * atr_pct * math.sqrt(window_days), FLAT_CEILING_PCT)
    return band_pct / 100.0


def _is_hit(verdict: str, return_pct: float, flat_threshold: float = 0.03) -> bool:
    """verdict 方向是否被事后行情验证。

    flat_threshold: HOLD "没动" 的阈值（小数）。默认 0.03 仅作回退；正常由
    review_one 按资产波动率传入 _flat_band 的结果。
    """
    direction = EXPECTED_DIRECTION.get(verdict, "flat")
    if direction == "up":
        return return_pct > 0
    elif direction == "down":
        return return_pct < 0
    elif direction == "flat":
        return abs(return_pct) < flat_threshold  # |涨跌| < 该资产窗口波动 = HOLD 命中
    return False


def wilson_ci(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    """命中率的 Wilson 95% 区间 [lo, hi]（3 位小数）。n=0 → None。

    选 Wilson 而非正态近似：n 在 30~300、命中率接近 0/1 时正态区间会越界、且覆盖率不足。
    """
    if n <= 0:
        return None
    import math
    p_hat = k / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


# 复盘层对 Input Health 的"降级"判定（事实→判断只在这一处）：
# 行情刷新失败 / 任一 worker 失败 / 市场情绪表盘为空（VIX 拉不到 → VIX 防御静默失效）/ macro 数据为空。
# 估值 / 事件 / insights 为空不算（黄金本无估值、无事件是常态、Dreaming 本就关着）。
DEGRADED_EMPTY_INPUTS = ("sentiment_brief", "portfolio_summary")


def is_degraded_input(health: Optional[Dict[str, Any]]) -> bool:
    h = health or {}
    if h.get("price_fetch_failed") or h.get("macro_data_empty") or h.get("worker_unavailable"):
        return True
    return any(name in (h.get("empty_inputs") or []) for name in DEGRADED_EMPTY_INPUTS)


def is_forced_hold_review(r: VerdictReview) -> bool:
    """强制 HOLD：新存档看 Input Health 的 forced_hold；老存档退回展示层同款启发式
    （HOLD 且自报 ≤ LOOKUP_FORCED_CONF——transcript 只留封顶后的数，认不出 Sanity 3/4）。"""
    h = r.input_health or {}
    if "forced_hold" in h:
        return bool(h["forced_hold"])
    return r.verdict == "HOLD" and float(r.confidence or 0.0) <= LOOKUP_FORCED_CONF


def _label_true(direction: str, ret: Optional[float], mkt_dir: Optional[str]) -> Optional[bool]:
    """与 _is_hit **同一把尺子**的标签：up = 涨 >0，down = 跌 <0，flat = 留在 flat band 内
    （band 是 per-row 算的，jsonl 里只剩 directions，所以 flat 从 directions 读）。"""
    if direction == "up":
        return None if ret is None else ret > 0
    if direction == "down":
        return None if ret is None else ret < 0
    if direction == "flat":
        return None if not mkt_dir else mkt_dir == "flat"
    return None


def _direction_base_rate(reviews: List[VerdictReview], window_key: str, direction: str) -> Optional[float]:
    """同桶全部样本（verdict 无关）里"该方向标签为真"的占比 = 一个永远喊这个方向的傻瓜的命中率。

    尺子必须与 _is_hit 一致：ACCUMULATE 按"涨 >0"算命中，基率就得是 P(涨 >0)，
    不能拿 directions 的 up（涨 > flat band，口径更严）当基率，否则提升被系统性高估。
    """
    # 样本池 = 进命中率的行（有期望方向）；UNCLEAR 行不进命中率也不进基率，与 export 口径一致
    flags = [_label_true(direction, r.actual_returns.get(window_key), r.directions.get(window_key))
             for r in reviews if r.verdict in EXPECTED_DIRECTION]
    flags = [f for f in flags if f is not None]
    if not flags:
        return None
    return round(sum(flags) / len(flags), 3)


# ---- 反事实账本纯核（Track A3）----
# 往返成本保守估计（买入并在窗口末卖出 / 卖出并在窗口末买回），按市场后缀，单位 bps：
# A 股 印花税 0.1%(卖) + 佣金 2×0.03%；港股通 印花税 2×0.1% + 各项费 ≈0.5%；澳股 2×0.12% + 点差；
# 期货/贵金属代理与加密按点差 0.4–0.5%；美股零佣金时代只剩点差 + SEC 费。
# ponytail: 常量表；要按券商/费率精确化时挪进 core/config/tunable.py。
ROUNDTRIP_COST_BPS: Dict[str, int] = {
    ".SS": 16, ".SZ": 16, ".BJ": 16, ".HK": 50, ".AX": 30, ".L": 60, "=F": 50, "-USD": 40,
}
DEFAULT_ROUNDTRIP_COST_BPS = 5


def roundtrip_cost_rate(symbol: str) -> float:
    """symbol → 往返成本率（小数，如 0.0016）。"""
    sym = (symbol or "").upper()
    for suffix, bps in ROUNDTRIP_COST_BPS.items():
        if sym.endswith(suffix.upper()):
            return bps / 10_000
    return DEFAULT_ROUNDTRIP_COST_BPS / 10_000


def counterfactual_pnl(verdict: str, alloc: float, actual_ret: Optional[float],
                       cost_rate: float) -> Optional[float]:
    """"照 CIO 的金额做"相对"什么都不做（留现金）"的净损益，基准币种，1 位小数。

    **方向由 verdict 定，金额只取绝对值**：regex 解析时代出过"TRIM 负号"类 bug（debate.py:300），
    老存档里金额符号不可信，按符号分侧会把钱算到相反的一边（CR #442 必改 1）。
    看涨（BUY/ACCUMULATE）：|alloc| × (标的收益 − 往返成本)
    看跌（TRIM/SELL）：|alloc| × (−标的收益 − 往返成本)——卖掉的那笔躲过了跌、也错过了涨
    HOLD / UNCLEAR / 金额 0 → None（没有金额可言；HOLD 的对错看 excess_returns）
    """
    side = EXPECTED_DIRECTION.get(str(verdict or "").upper())
    if actual_ret is None or not alloc or side not in ("up", "down"):
        return None
    notional = abs(float(alloc))
    if side == "up":
        return round(notional * (actual_ret - cost_rate), 1)
    return round(notional * (-actual_ret - cost_rate), 1)


def _summarize_bucket(reviews: List[VerdictReview], *, suppress_rates: bool) -> Dict[str, Any]:
    """对单桶（holdout 或 contaminated）算命中率聚合，按 verdict 类型 + 窗口分类。

    suppress_rates=True（holdout 且 n<30）：按公开数据红线 #2 **不展示具体命中率数字**，
    只留样本量 n（防小样本被截图误传）。contaminated 桶永不抑制（它本就标注"非业绩"）。
    """
    bucket: Dict[str, Any] = {"n": len(reviews), "by_window": {}, "by_verdict": {},
                              "macro_shock_count": 0, "live_count": 0, "backtest_count": 0,
                              "rates_suppressed_sub30": suppress_rates}
    for window in HIT_WINDOWS:
        key = f"{window}d"
        hits = [r.hits[key] for r in reviews if key in r.hits]
        if not hits:
            continue
        entry: Dict[str, Any] = {"n": len(hits)}
        if not suppress_rates:
            entry["hit_rate"] = round(sum(hits) / len(hits), 3)
            entry["ci95"] = wilson_ci(sum(hits), len(hits))
            # 剔除 macro shock 后再算
            non_shock = [r.hits[key] for r in reviews
                         if key in r.hits and not r.macro_shock.get("detected")]
            if non_shock:
                entry["hit_rate_excl_macro_shock"] = round(sum(non_shock) / len(non_shock), 3)
                entry["n_excl_shock"] = len(non_shock)
        bucket["by_window"][key] = entry

    for verdict in VERDICT_BUCKETS:
        subset = [r for r in reviews if r.verdict == verdict]
        if not subset:
            continue
        entry = {"n": len(subset),
                 "avg_confidence": round(sum(r.confidence for r in subset) / len(subset), 3)}
        expected = EXPECTED_DIRECTION.get(verdict)
        if not suppress_rates and expected is not None:
            for window in HIT_WINDOWS:
                key = f"{window}d"
                hits = [r.hits[key] for r in subset if key in r.hits]
                if hits:
                    rate = round(sum(hits) / len(hits), 3)
                    entry[f"hit_rate_{key}"] = rate
                    # 提升 = 命中率 − 同桶市场基率（HOLD 命中 ≈ 市场横盘基率时提升 ≈ 0，
                    # 这正是 2026-10 评审发现的"与永远 HOLD 无法区分"）
                    base = _direction_base_rate(reviews, key, expected)
                    entry[f"base_rate_{key}"] = base
                    entry[f"lift_{key}"] = round(rate - base, 3) if base is not None else None
                    entry[f"ci95_{key}"] = wilson_ci(sum(hits), len(hits))
                # 反事实：同窗口基准超额（verdict 无关的"这个标的跑没跑赢基准"）+ 照做净损益
                ex = [r.excess_returns[key] for r in subset if key in r.excess_returns]
                if ex:
                    entry[f"excess_mean_{key}"] = round(sum(ex) / len(ex), 4)
                    entry[f"beat_benchmark_rate_{key}"] = round(sum(1 for e in ex if e > 0) / len(ex), 3)
                    entry[f"n_benchmark_{key}"] = len(ex)
                followed = [r for r in subset if key in r.followed_pnl]
                if followed:
                    pnl = [r.followed_pnl[key] for r in followed]
                    notional = sum(abs(r.alloc_cny) for r in followed)
                    entry[f"followed_pnl_sum_{key}"] = round(sum(pnl), 1)
                    # 名义金额加权净收益率（Σ净损益 / Σ|金额|）：可公开/可跨账户比较的那个数，金额不是
                    entry[f"followed_net_return_{key}"] = round(sum(pnl) / notional, 4) if notional else None
                    entry[f"followed_positive_rate_{key}"] = round(sum(1 for x in pnl if x > 0) / len(pnl), 3)
                    entry[f"n_followed_{key}"] = len(pnl)
        bucket["by_verdict"][verdict] = entry

    # 反事实账本桶级合计：所有有金额的裁决（BUY/ACCUMULATE/TRIM/SELL）照做 vs 现金，扣成本
    bucket["benchmark_symbols"] = sorted({r.benchmark_symbol for r in reviews if r.benchmark_symbol})
    bucket["benchmark_missing_count"] = {
        f"{w}d": sum(1 for r in reviews if f"{w}d" in r.actual_returns and f"{w}d" not in r.excess_returns)
        for w in HIT_WINDOWS}
    if not suppress_rates:
        for window in HIT_WINDOWS:
            key = f"{window}d"
            followed = [r for r in reviews if key in r.followed_pnl]
            if followed:
                pnl = [r.followed_pnl[key] for r in followed]
                notional = sum(abs(r.alloc_cny) for r in followed)
                bucket[f"followed_pnl_total_{key}"] = round(sum(pnl), 1)
                bucket[f"followed_net_return_{key}"] = round(sum(pnl) / notional, 4) if notional else None
                bucket[f"followed_n_{key}"] = len(pnl)

    # 单独标注（不剔除，只让读者看见）：UNCLEAR / 被规则强制的 HOLD / 残缺输入上做的裁决
    bucket["unclear_count"] = sum(1 for r in reviews if r.verdict not in EXPECTED_DIRECTION)
    bucket["forced_hold_count"] = sum(1 for r in reviews if is_forced_hold_review(r))
    bucket["degraded_input_count"] = sum(1 for r in reviews if is_degraded_input(r.input_health))
    bucket["health_tagged_count"] = sum(1 for r in reviews if r.input_health)

    bucket["macro_shock_count"] = sum(1 for r in reviews if r.macro_shock.get("detected"))
    # 决议日 regime 分布 + crash 计数（crash 样本下游免责剔除，但此处留痕统计）
    bucket["regime_crash_count"] = sum(1 for r in reviews if r.regime_at_decision == "crash")
    bucket["regime_recovery_count"] = sum(1 for r in reviews if r.regime_at_decision == "recovery")
    regime_dist: Dict[str, int] = {}
    for r in reviews:
        reg = r.regime_at_decision or "unknown"
        regime_dist[reg] = regime_dist.get(reg, 0) + 1
    bucket["regime_distribution"] = regime_dist
    bucket["live_count"] = sum(1 for r in reviews if r.source == "live")
    bucket["backtest_count"] = sum(1 for r in reviews if r.source == "backtest")
    return bucket


def summarize_verdict_reviews(reviews: List[VerdictReview]) -> Dict[str, Any]:
    """按 contaminated（决议日 ≤ CONTAMINATION_CUTOFF）**强制分桶**聚合。

    机器强制（不靠记忆）：holdout（cutoff 之后，干净业绩）与 contaminated（落在 LLM 训练
    窗口，记忆穿越非业绩）**绝不合并成一个命中率**——本函数不产出任何跨桶 union 数字，
    两桶各自独立 `_summarize_bucket`。partition assert 守"每条 review 非此即彼，无遗漏无重叠"。
    holdout 桶 n<30 按红线 #2 不出命中率切片；contaminated 桶数字带 note 标注"含记忆穿越,非业绩"。
    周末重复样本（is_closed_weekend）先剔除，只计数；total 仍是全部行。
    """
    total = len(reviews)
    reviews = [r for r in reviews if not is_closed_weekend(r.asset, r.date)]
    holdout = [r for r in reviews if not r.contaminated]
    contaminated = [r for r in reviews if r.contaminated]
    assert len(holdout) + len(contaminated) == len(reviews), \
        "contaminated 分桶必须无遗漏无重叠（每条 review 非 holdout 即 contaminated）"

    # A5：自报置信度校准 + 裁决中性度只看 live 且干净（与公开 JSON 同口径，backtest 不混进来）
    live_holdout = [r for r in holdout if r.source == "live"]
    return {
        "total": total,
        "weekend_dup_excluded": total - len(reviews),
        "cutoff": CONTAMINATION_CUTOFF,
        "calibration": calibration_summary(live_holdout),
        "neutrality": neutrality_summary(live_holdout),
        "holdout": _summarize_bucket(holdout, suppress_rates=len(holdout) < 30),
        "contaminated": {
            **_summarize_bucket(contaminated, suppress_rates=False),
            "note": "含记忆穿越,非业绩",
        },
    }


def _bucket_lines(title: str, bucket: Dict[str, Any], *, note: Optional[str] = None) -> List[str]:
    """单桶（holdout / contaminated）的报告片段。命中率被 sub30 抑制时只报样本量。"""
    lines = [f"\n## {title} (n={bucket['n']}, "
             f"{bucket['live_count']} live + {bucket['backtest_count']} backtest)\n"]
    if note:
        lines.append(f"> ⚠️ {note}\n")
    if bucket["n"] == 0:
        lines.append("_无样本_\n")
        return lines
    if bucket.get("rates_suppressed_sub30"):
        lines.append(f"📊 样本量 {bucket['n']} < 30 —— 按公开数据红线 #2 **不展示具体命中率**"
                     "（防小样本被截图误传）。继续积累到 30+ 再做正式评估。\n")
        return lines

    lines += ["### 按时间窗口命中率\n",
              "| 窗口 | N | 总命中率 | 剔除宏观突变后 |",
              "|---|---|---|---|"]
    for w_key, w in bucket["by_window"].items():
        excl = w.get("hit_rate_excl_macro_shock")
        excl_n = w.get("n_excl_shock", "—")
        if isinstance(excl, float):
            lines.append(f"| {w_key} | {w['n']} | {w['hit_rate']*100:.1f}% | {excl*100:.1f}% (n={excl_n}) |")
        else:
            lines.append(f"| {w_key} | {w['n']} | {w['hit_rate']*100:.1f}% | — |")

    lines += ["\n### 按 verdict 类型命中率\n",
              "| Verdict | N | 平均 confidence | 1d hit | 7d hit | 30d hit | 30d 基率 | 30d 提升 | 30d 95% CI |",
              "|---|---|---|---|---|---|---|---|---|"]
    for v_key, v in bucket["by_verdict"].items():
        if v_key not in EXPECTED_DIRECTION:
            lines.append(f"| {v_key} | {v['n']} | {v['avg_confidence']:.2f} | — | — | — | — | — | 不计命中 |")
            continue
        base = v.get("base_rate_30d")
        lift = v.get("lift_30d")
        ci = v.get("ci95_30d")
        lines.append(
            f"| {v_key} | {v['n']} | {v['avg_confidence']:.2f} | "
            f"{v.get('hit_rate_1d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_7d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_30d', 0)*100:.0f}% | "
            f"{'—' if base is None else f'{base*100:.0f}%'} | "
            f"{'—' if lift is None else f'{lift*100:+.0f}pp'} | "
            f"{'—' if not ci else f'{ci[0]*100:.0f}–{ci[1]*100:.0f}%'} |"
        )
    bench = "、".join(bucket.get("benchmark_symbols") or []) or "未配置"
    lines += [f"\n### 反事实（同窗口基准 {bench}；照做 = 按 CIO 金额执行并在窗口末平仓，扣往返成本，对比留现金）\n",
              "| Verdict | N(有基准) | 30d 超额均值 | 30d 跑赢基准 | N(有金额) | 30d 照做净损益合计 | 照做净收益率 | 照做为正比例 |",
              "|---|---|---|---|---|---|---|---|"]
    def _pp(x: Optional[float]) -> str:
        return "—" if x is None else f"{x * 100:+.1f}pp"

    def _pct(x: Optional[float]) -> str:
        return "—" if x is None else f"{x * 100:.0f}%"

    def _money(x: Optional[float]) -> str:
        return "—" if x is None else f"{x:+,.0f}"

    for v_key, v in bucket["by_verdict"].items():
        nb, nf = v.get("n_benchmark_30d"), v.get("n_followed_30d")
        if not nb and not nf:
            continue
        lines.append(
            f"| {v_key} | {nb or 0} | {_pp(v.get('excess_mean_30d'))} | "
            f"{_pct(v.get('beat_benchmark_rate_30d'))} | {nf or 0} | "
            f"{_money(v.get('followed_pnl_sum_30d'))} | {_pp(v.get('followed_net_return_30d'))} | "
            f"{_pct(v.get('followed_positive_rate_30d'))} |"
        )
    if bucket.get("followed_n_30d"):
        miss = (bucket.get("benchmark_missing_count") or {}).get("30d", 0)
        lines.append(f"\n**全部有金额的裁决照做（30d，扣成本，vs 留现金）：{bucket['followed_pnl_total_30d']:+,.0f}，"
                     f"名义加权净收益率 {_pp(bucket.get('followed_net_return_30d'))}（n={bucket['followed_n_30d']}）**；"
                     f"缺基准数据 {miss} 条。\n")
    lines.append(
        f"\n> 单独标注（已计入 N，未剔除）：UNCLEAR {bucket.get('unclear_count', 0)} 条不计命中；"
        f"强制/低置信 HOLD {bucket.get('forced_hold_count', 0)} 条；"
        f"残缺输入上做的裁决 {bucket.get('degraded_input_count', 0)} 条"
        f"（带 Input Health 块的样本 {bucket.get('health_tagged_count', 0)} 条，其余老存档按未知处理）。\n"
    )
    return lines



# =====================================================================
# 置信度校准 + 裁决中性度（评审路线 Sprint 2 · A5）——只测量/展示，不碰裁决逻辑
# =====================================================================
# 校准：CIO 自报的 confidence 当"命中概率"，和 30d 实际命中对账。展示层早已不用自报分
# （见下面的查表），这里回答的是"自报分到底有没有信息"。n<30 的档不出数字（红线 #2 同阈值）。
# 中性度：裁决分布 vs 同样本市场实际方向分布——"永远 HOLD"在横盘市里不是判断，是运气。

CALIBRATION_BINS = ("≤0.4", "0.4–0.6", "0.6–0.7", "0.7–0.8", ">0.8")
CALIBRATION_MIN_N = 30   # 红线 #2 / LOOKUP_MIN_N 同阈值


def _confidence_bin(conf: float) -> int:
    """自报分 → 档位下标。边界：≤0.4 | (0.4,0.6) | [0.6,0.7) | [0.7,0.8] | >0.8。
    0.6 归 0.6–0.7 档（它是降级常量，生产里扎堆在此）。先 round(2) 防浮点贴边。"""
    c = round(float(conf), 2)
    if c <= 0.4:
        return 0
    if c < 0.6:
        return 1
    if c < 0.7:
        return 2
    if c <= 0.8:
        return 3
    return 4


def calibration_from_pairs(pairs: Iterable[Tuple[float, bool]], *,
                           min_n: int = CALIBRATION_MIN_N) -> Dict[str, Any]:
    """[(自报 confidence, 是否命中)] → 校准汇总（纯函数；review 与公开 export 共用同一份算法）。

    - brier = mean (confidence − hit)²：把自报分当"命中概率"打分，越低越好。
    - base_rate_brier = mean (p̄ − hit)²，p̄ = 整个样本的命中率：一个永远报 p̄ 的傻瓜的 Brier。
      brier 高于它 = 自报分比"不看内容、永远报平均命中率"还差。每档的 base_rate_brier 用同一个
      p̄（整体命中率）算，所以各档按样本数加权平均 = 整体值。
    - ece = Σ (档 n / N) × |档平均自报 − 档命中率|（档权重 ECE）。
    - 自报恰为 0.0 的行不进校准、单列 n_zero_conf：那是 cio_parse 解析失败的兜底值，不是"自报 0% 把握"，
      混进来会被当成最低档的"全错预测"拉高 Brier / ECE。
    样本 n < min_n → 整体数字全 None；档 n < min_n → 该档数字 None（n 始终保留）。
    """
    all_pts = [(float(c), 1.0 if h else 0.0) for c, h in pairs]
    pts = [(c, y) for c, y in all_pts if c != 0.0]
    n = len(pts)
    out: Dict[str, Any] = {"n": n, "n_zero_conf": len(all_pts) - n, "min_n": min_n, "hit_rate": None,
                           "ece": None, "brier": None, "base_rate_brier": None, "bins": []}
    by_bin: List[List[Tuple[float, float]]] = [[] for _ in CALIBRATION_BINS]
    for c, y in pts:
        by_bin[_confidence_bin(c)].append((c, y))
    p_bar = sum(y for _, y in pts) / n if n else 0.0
    if n and n >= min_n:
        out["hit_rate"] = round(p_bar, 3)
        out["brier"] = round(sum((c - y) ** 2 for c, y in pts) / n, 4)
        out["base_rate_brier"] = round(sum((p_bar - y) ** 2 for _, y in pts) / n, 4)
        out["ece"] = round(sum(
            len(b) / n * abs(sum(c for c, _ in b) / len(b) - sum(y for _, y in b) / len(b))
            for b in by_bin if b), 4)
    for label, b in zip(CALIBRATION_BINS, by_bin):
        cell: Dict[str, Any] = {"label": label, "n": len(b), "hit_rate": None,
                                "mean_confidence": None, "brier": None, "base_rate_brier": None}
        if len(b) >= min_n:
            cell["hit_rate"] = round(sum(y for _, y in b) / len(b), 3)
            cell["mean_confidence"] = round(sum(c for c, _ in b) / len(b), 3)
            cell["brier"] = round(sum((c - y) ** 2 for c, y in b) / len(b), 4)
            cell["base_rate_brier"] = round(sum((p_bar - y) ** 2 for _, y in b) / len(b), 4)
        out["bins"].append(cell)
    return out


def calibration_summary(reviews: List[VerdictReview], window: str = "30d", *,
                        min_n: int = CALIBRATION_MIN_N) -> Dict[str, Any]:
    """按自报 confidence 分档对账 window 窗口的命中。只算有 hits[window] 的行
    （UNCLEAR 不打 hits，自然排除；强制 HOLD 的 ≤0.4 行照算，它们也是自报分；
    自报恰为 0.0 的行排除并计入 n_zero_conf）。"""
    out = calibration_from_pairs(
        ((r.confidence, r.hits[window]) for r in reviews
         if window in r.hits and r.verdict in EXPECTED_DIRECTION),
        min_n=min_n)
    out["window"] = window
    return out


def neutrality_summary(reviews: List[VerdictReview], window: str = "30d", *,
                       min_n: int = CALIBRATION_MIN_N) -> Dict[str, Any]:
    """裁决分布（各 verdict 占比，含 UNCLEAR）vs 同样本（有 directions[window] 的行）市场实际方向分布。
    sentence = 一行人话；n < min_n 不出（None）——日报据此不出这行。"""
    rows = [r for r in reviews if r.directions.get(window) in ("up", "down", "flat")]
    n = len(rows)

    def _cell(c: int) -> Dict[str, Any]:
        return {"n": c, "share": round(c / n, 3) if n else None}

    vc = {v: 0 for v in VERDICT_BUCKETS}
    for r in rows:
        vc[r.verdict if r.verdict in EXPECTED_DIRECTION else "UNCLEAR"] += 1
    mc = {d: sum(1 for r in rows if r.directions[window] == d) for d in ("up", "down", "flat")}
    out: Dict[str, Any] = {"window": window, "n": n,
                           "verdicts": {v: _cell(c) for v, c in vc.items()},
                           "market": {d: _cell(c) for d, c in mc.items()},
                           "sentence": None}
    if n and n >= min_n:
        out["sentence"] = (
            f"过去 {n} 条已成熟裁决（{window}）中 {vc['HOLD'] / n:.0%} 是 HOLD，"
            f"同期市场 {mc['flat'] / n:.0%} 横盘（{mc['down'] / n:.0%} 下跌 / {mc['up'] / n:.0%} 上涨）；"
            f"BUY {vc['BUY']} 条、SELL {vc['SELL']} 条。")
    return out


def _calibration_lines(cal: Dict[str, Any]) -> List[str]:
    lines = [f"\n## 置信度校准（CIO 自报 vs {cal.get('window', '30d')} 实际命中 · live 干净样本 n={cal['n']}）\n"]
    zero = (f"另有 {cal['n_zero_conf']} 条自报恰为 0.0（解析失败兜底），不计入。\n"
            if cal.get("n_zero_conf") else "")
    if cal["n"] == 0:
        return lines + ["_无样本_\n", zero]
    if cal["ece"] is None:
        return lines + [f"📊 样本量 {cal['n']} < {cal['min_n']} —— 按红线 #2 不展示具体数字。\n", zero]
    lines += ["| 自报档 | N | 平均自报 | 实际命中率 | 本档 Brier | 本档基率 Brier |", "|---|---|---|---|---|---|"]
    for b in cal["bins"]:
        if b["hit_rate"] is None:
            lines.append(f"| {b['label']} | {b['n']} | {'n<30 不出数字' if b['n'] else '—'} | — | — | — |")
        else:
            lines.append(f"| {b['label']} | {b['n']} | {b['mean_confidence']:.2f} | {b['hit_rate']:.1%} | "
                         f"{b['brier']:.3f} | {b['base_rate_brier']:.3f} |")
    verdict = "自报分比「永远报整体命中率」更差" if cal["brier"] > cal["base_rate_brier"] else "自报分好于「永远报整体命中率」"
    lines.append(f"\n**整体**：命中率 {cal['hit_rate']:.1%}，ECE {cal['ece']:.3f}，"
                 f"Brier {cal['brier']:.4f} vs 基率 Brier {cal['base_rate_brier']:.4f} → {verdict}。"
                 "（Brier 以自报分当命中概率；基率 Brier = 常数预测整体命中率。）\n")
    if zero:
        lines.append(zero)
    return lines


def _neutrality_lines(neu: Dict[str, Any]) -> List[str]:
    lines = [f"\n## 裁决中性度（裁决分布 vs 同样本市场 {neu.get('window', '30d')} 实际方向 · live 干净样本 n={neu['n']}）\n"]
    if neu["n"] == 0:
        return lines + ["_无样本_\n"]
    pct = lambda c: "—" if c["share"] is None else f"{c['share']:.0%}"  # noqa: E731
    lines += ["| 裁决 | N | 占比 |", "|---|---|---|"]
    lines += [f"| {v} | {c['n']} | {pct(c)} |" for v, c in neu["verdicts"].items()]
    lines += ["\n| 同期市场方向 | N | 占比 |", "|---|---|---|"]
    lines += [f"| {d} | {c['n']} | {pct(c)} |" for d, c in neu["market"].items()]
    n = neu["n"]
    lines.append(f"\n**{neu['sentence'] or f'样本量 {n} < 30，不出结论'}**\n")
    return lines


# =====================================================================
# 置信度展示查表（2026-10 D10 P1，只改展示层）
# =====================================================================
# 给用户看的"置信度"不再是 CIO 自报的 0-1 数（历史上几乎不带信息：没有一个格子显著好于
# 常数），改成"同类决议 30 天后怎样"的确定性查表。只改展示：verdict / alloc /
# sanity / high_confidence_buy / transcript 里的 `(confidence X)` 一律不动，自报原数照常落盘。
# 样本池 = live + 前瞻纸面舰队（签字口径 = R3 池）；本机某 verdict n<30 时改用包里随附的
# 默认表（只用舰队样本生成，见 scripts/gen_confidence_lookup_default.py）。

LOOKUP_MIN_N = 30            # 红线 #2 同阈值：n<30 只给 n
# 强制 HOLD 的封顶（Sanity 3/4，config 默认 0.4）+ 自报 ≤0.4 的低分行：展示时打标不查表。
# 查表样本不按自报分剔除（签字口径 = R3 池：只剔周末）；按自报分切样本要先 PREREG。
LOOKUP_FORCED_CONF = 0.4


def build_confidence_lookup(live: List[VerdictReview],
                            fleet: List[VerdictReview] = ()) -> Dict[str, Any]:
    """按 verdict 的 30d 查表。样本 = live（source=="live"）+ 舰队，都要非污染、30d 已成熟、
    非周末重复（is_closed_weekend），不按自报分剔除（≤0.4 只在展示时打标）。HOLD 的 rate =
    30 天涨跌留在 flat band 内的比例（= hits["30d"]）；有方向的 = 方向判对比例。
    market_flat = 同一批样本（不分 verdict）30 天落在 flat band 内的比例，给 HOLD 当基率对照。
    每格带来源构成 n_live / n_fleet。n<30 → rate=None。"""
    def _ok(r: VerdictReview) -> bool:
        return (not r.contaminated and "30d" in r.hits and r.verdict in EXPECTED_DIRECTION
                and not is_closed_weekend(r.asset, r.date))

    pools = {"live": [r for r in live if r.source == "live" and _ok(r)],
             "fleet": [r for r in fleet if _ok(r)]}

    def _cell(flag, keep=lambda r: True) -> Dict[str, Any]:
        got = {src: [flag(r) for r in rows if keep(r)] for src, rows in pools.items()}
        flags = got["live"] + got["fleet"]
        n = len(flags)
        return {"n": n, "rate": round(sum(flags) / n, 2) if n >= LOOKUP_MIN_N else None,
                "n_live": len(got["live"]), "n_fleet": len(got["fleet"])}

    verdicts = sorted({r.verdict for rows in pools.values() for r in rows})
    return {
        "source": "live+fleet", "window": "30d", "min_n": LOOKUP_MIN_N,
        "by_verdict": {v: _cell(lambda r: r.hits["30d"], lambda r, v=v: r.verdict == v)
                       for v in verdicts},
        "market_flat": _cell(lambda r: r.directions.get("30d") == "flat"),
    }


def merge_confidence_lookup(local: Optional[Dict[str, Any]],
                            default: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """逐个 verdict 选表：本机 n≥30 用本机；否则内置默认表（只含舰队）n≥30 用默认表；
    两边都不够 → 留本机的 n（显示"样本不足"）。选中的格子带 from=local/default，
    以及同一张表的 market_flat（HOLD 的基率必须和它出自同一个样本池）。"""
    if local is None and default is None:
        return None
    tables = {"local": local or {}, "default": default or {}}
    out: Dict[str, Any] = {}
    for v in {v for t in tables.values() for v in (t.get("by_verdict") or {})}:
        for src in ("local", "default"):
            cell = (tables[src].get("by_verdict") or {}).get(v) or {}
            if cell.get("rate") is not None and int(cell.get("n") or 0) >= LOOKUP_MIN_N:
                out[v] = {**cell, "from": src, "market_flat": tables[src].get("market_flat") or {}}
                break
        else:
            n = int(((tables["local"].get("by_verdict") or {}).get(v) or {}).get("n") or 0)
            out[v] = {"n": n, "rate": None, "from": "local"}
    return {"by_verdict": out}


def is_forced_or_low_confidence(v: Dict[str, Any]) -> bool:
    """被规则强制成 HOLD（WORKER_UNAVAILABLE / 集中度 lens / TRIM 没买回点 / 防御拦买），
    或自报 ≤0.4（含 UNCLEAR 的 0.0）。transcript 只留封顶后的数，所以从 md 读到的只能靠
    ≤0.4 认出 Sanity 3/4；内存里的 verdict dict 还带 _original_* 溯源。"""
    try:
        conf = float(v.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    forced = "_original_confidence_unavailable" in v or (
        v.get("verdict") == "HOLD" and v.get("_original_verdict") not in (None, "HOLD"))
    return forced or conf <= LOOKUP_FORCED_CONF


def confidence_display(v: Dict[str, Any], lookup: Optional[Dict[str, Any]],
                       *, with_raw: bool = True) -> str:
    """裁决旁展示的那串字（邮件 / 事件提醒 / API）。lookup = merge_confidence_lookup 的结果；
    None（job 没跑过且没有默认表）按 n=0。HOLD 不叫命中率：它衡量的是那段时间市场横没横盘，
    所以同时给同一样本池的市场基率。末尾注明样本来源（含纸面舰队样本 / 默认表 / 本机样本）。"""
    raw = v.get("confidence")
    raw_txt = (f"（自报 {raw:.2f}）"
               if with_raw and isinstance(raw, (int, float)) else "")
    if is_forced_or_low_confidence(v):
        return f"输入缺失/强制 HOLD，不查表{raw_txt}"
    verdict = str(v.get("verdict") or "").upper()
    cell = ((lookup or {}).get("by_verdict") or {}).get(verdict) or {}
    n, rate = int(cell.get("n") or 0), cell.get("rate")
    if rate is None or n < LOOKUP_MIN_N:
        return f"样本不足（n={n}）{raw_txt}"
    note = ("默认表" if cell.get("from") == "default"
            else "含纸面舰队样本" if cell.get("n_fleet") else "本机样本")
    if verdict == "HOLD":
        mk = cell.get("market_flat") or {}
        base = f"；同期市场横盘基率 {mk['rate']:.0%}" if mk.get("rate") is not None else ""
        return (f"同类 HOLD 之后 30 天涨跌留在正常波动带内的比例 {rate:.0%}"
                f"（n={n}{base}；{note}）{raw_txt}")
    return f"同类 {verdict} 30 天后方向判对的比例 {rate:.0%}（n={n}；{note}）{raw_txt}"


# =====================================================================
# 实验臂对照纯核（评审路线 B4 密封投票臂）——只测量/展示，零 IO
# =====================================================================
# 实验臂（同日同标的、同输入，只差"抽样 N 次 + 按票聚合"）和对照臂（.backtest 舰队行）并排。
# 本节只吃传入的 VerdictReview 列表；实验臂行永不进 summarize_verdict_reviews / build_confidence_lookup /
# 公开 JSON——调用方 jobs/verdict_review.run_arm 单独写 jsonl + 报告。

ARM_MIN_N = 30   # 红线 #2 同阈值：成熟（30d）可评分样本 < 30 的格子不展示命中率数字
ARM_TIERS = ("unanimous", "majority", "split")   # 票数一致度：全票 / 多数（非全票）/ 无多数→HOLD


def arm_vote_verdicts(r: VerdictReview) -> List[str]:
    """实验臂行的逐票 verdict（transcript Input Health 的 votes）；对照臂行 / 老存档没有 → []。"""
    votes = (r.input_health or {}).get("votes")
    if not isinstance(votes, list):
        return []
    return [str(v.get("verdict") or "UNCLEAR").upper() for v in votes if isinstance(v, dict)]


def _vote_tier(votes: List[str]) -> str:
    """全票一致 / 严格多数 / 无严格多数（聚合成 HOLD）。与 backtest_committee.aggregate_votes 同定义。"""
    top = max(votes.count(v) for v in set(votes))
    if top == len(votes):
        return "unanimous"
    return "split" if top * 2 <= len(votes) else "majority"


def _rate_ci(k: int, n: int) -> Dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": wilson_ci(k, n)}


def _share(k: int, n: int) -> Optional[float]:
    return round(k / n, 3) if n else None


def vote_stability(arm: List[VerdictReview], control: Iterable[VerdictReview] = (), *,
                   min_n: int = ARM_MIN_N) -> Dict[str, Any]:
    """臂内稳定度（A4 口径）：全票一致率、方向翻转率（N 票的方向类不全相同）、无多数率、HOLD 占比
    （单票 / 聚合后 / 对照臂），以及按票数一致度分档的 30d 命中（"用投票分裂度代替自报置信度"的核心问题：
    分裂度到底带不带信息）。没有 votes 的行（对照臂行 / 老存档）不算。
    control 传对照臂行（HOLD 占比并排用），内部只取与臂同日同标的、同口径（非污染/非休市周末重复）的那些。"""
    arm = [r for r in arm if _arm_row_ok(r)]
    keys = {(r.date, r.asset) for r in arm}
    control = [r for r in control if (r.date, r.asset) in keys and _arm_row_ok(r)]
    rows = [(r, v) for r in arm for v in [arm_vote_verdicts(r)] if len(v) >= 2]
    n = len(rows)
    tier_of = [_vote_tier(v) for _, v in rows]
    flips = sum(1 for _, v in rows if len({EXPECTED_DIRECTION.get(x, UNCLEAR_DIRECTION) for x in v}) > 1)
    all_votes = [x for _, v in rows for x in v]
    tiers: Dict[str, Dict[str, Any]] = {}
    for t in ARM_TIERS:
        sub = [r for (r, _), tt in zip(rows, tier_of) if tt == t]
        scored = [r for r in sub if "30d" in r.hits]
        tiers[t] = {"n": len(sub), "n_scored_30d": len(scored),
                    "hit_rate_30d": (round(sum(r.hits["30d"] for r in scored) / len(scored), 3)
                                     if len(scored) >= min_n else None)}
    return {
        "n": n,
        "arm_tags": dict(Counter(str((r.input_health or {}).get("arm") or "?") for r, _ in rows)),
        "unanimous": _rate_ci(tier_of.count("unanimous"), n),
        "flip": _rate_ci(flips, n),
        "split": _rate_ci(tier_of.count("split"), n),
        "hold_share_votes": _share(all_votes.count("HOLD"), len(all_votes)),
        "hold_share_final": _share(sum(1 for r, _ in rows if r.verdict == "HOLD"), n),
        "hold_share_control": _share(sum(1 for r in control if r.verdict == "HOLD"), len(control)),
        "tiers": tiers,
    }


def _arm_row_ok(r: VerdictReview) -> bool:
    """与 summarize_verdict_reviews 的 holdout 同口径：非污染、非休市周末重复。"""
    return not r.contaminated and not is_closed_weekend(r.asset, r.date)


def _side_summary(rows: List[VerdictReview], min_n: int) -> Dict[str, Any]:
    """一侧（臂 / 对照臂）的 30d 摘要。逐 verdict 的命中/基率/提升/超额/照做净收益全部复用
    _summarize_bucket（#431 同尺子基率 + #442 反事实），这里只补整侧合计的基率与提升。"""
    bucket = _summarize_bucket(rows, suppress_rates=False)
    scored = [r for r in rows if "30d" in r.hits]
    n30 = len(scored)
    followed_n = bucket.get("followed_n_30d", 0)
    out: Dict[str, Any] = {
        "n_rows": len(rows), "n_scored_30d": n30, "suppressed": n30 < min_n,
        "hold_share": _share(sum(1 for r in rows if r.verdict == "HOLD"), len(rows)),
        "hit_rate_30d": None, "base_rate_30d": None, "lift_30d": None,
        "ci95_30d": None, "lift_ci95_30d": None,
        "followed_net_return_30d": bucket.get("followed_net_return_30d") if followed_n >= min_n else None,
        "followed_n_30d": followed_n,
        "benchmark_symbols": bucket.get("benchmark_symbols", []),
        "by_verdict": {},
    }
    # 每个数字各按自己的分母抑制（n<30 不出数字，红线 #2；与公开 JSON 的 n_benchmark 抑制同口径）
    gates = (("n_scored_30d", ("hit_rate_30d", "base_rate_30d", "lift_30d", "ci95_30d")),
             ("n_benchmark_30d", ("excess_mean_30d", "beat_benchmark_rate_30d")),
             ("n_followed_30d", ("followed_net_return_30d",)))
    for v, cell in bucket["by_verdict"].items():
        slim: Dict[str, Any] = {
            "n": cell["n"], "n_benchmark_30d": cell.get("n_benchmark_30d", 0),
            "n_followed_30d": cell.get("n_followed_30d", 0),
            "n_scored_30d": sum(1 for r in rows if r.verdict == v and "30d" in r.hits)}
        for n_key, keys in gates:
            if slim[n_key] >= min_n:
                slim.update({k: cell[k] for k in keys if k in cell})
        out["by_verdict"][v] = slim
    if n30 >= min_n:
        k = sum(1 for r in scored if r.hits["30d"])
        bases = {d: _direction_base_rate(rows, "30d", d) for d in ("up", "down", "flat")}
        per_row = [bases[EXPECTED_DIRECTION[r.verdict]] for r in scored]
        per_row = [b for b in per_row if b is not None]
        ci = wilson_ci(k, n30)
        out.update(hit_rate_30d=round(k / n30, 3), ci95_30d=ci)
        if per_row and ci:
            base = sum(per_row) / len(per_row)   # 整侧基率 = 各行"永远喊本行方向的傻瓜"命中率的均值
            out.update(base_rate_30d=round(base, 3), lift_30d=round(k / n30 - base, 3),
                       lift_ci95_30d=[round(ci[0] - base, 3), round(ci[1] - base, 3)])  # 基率当常数，同 live 页口径
    return out


def arm_vs_control(arm: List[VerdictReview], control: List[VerdictReview], *,
                   min_n: int = ARM_MIN_N) -> Dict[str, Any]:
    """同日同标的（date, asset）取交集后两侧并排；配对 30d：谁命中谁没命中。"""
    a_by = {(r.date, r.asset): r for r in arm if _arm_row_ok(r)}
    c_by = {(r.date, r.asset): r for r in control if _arm_row_ok(r)}
    keys = sorted(set(a_by) & set(c_by))
    a_rows, c_rows = [a_by[k] for k in keys], [c_by[k] for k in keys]
    hits = [(a_by[k].hits["30d"], c_by[k].hits["30d"]) for k in keys
            if "30d" in a_by[k].hits and "30d" in c_by[k].hits]
    a_churn, c_churn = direction_churn(a_rows), direction_churn(c_rows)
    return {
        "matched": len(keys), "arm_total": len(a_by), "control_total": len(c_by), "min_n": min_n,
        "arm": _side_summary(a_rows, min_n),
        "control": _side_summary(c_rows, min_n),
        # 配对（同日同标的）：30d 谁命中 / 逐日方向翻转谁多 / HOLD 谁多——都是"两臂不一致的那些对"上的符号检验
        "paired_30d": _discordant(hits),
        "paired_churn": _discordant([(a_churn[k], c_churn[k]) for k in sorted(set(a_churn) & set(c_churn))]),
        "paired_hold": _discordant([(a_by[k].verdict == "HOLD", c_by[k].verdict == "HOLD") for k in keys]),
    }


def sign_test_p(b: int, c: int) -> Optional[float]:
    """精确二项符号检验（双侧，H0: 不一致对里 b 与 c 各占一半）。b + c = 0 → None。
    配对对照只看"两臂结果不同的那些对"，共同的市场环境（同日同标的）天然抵消。"""
    import math
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    return round(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), 4)


CHURN_MAX_GAP_DAYS = 4   # 相邻两个决议日相隔 ≤4 天才算"逐日"对（周末 3、含一天假日 4；更远的断档不比）


def direction_churn(rows: List[VerdictReview]) -> Dict[Tuple[str, str], bool]:
    """逐日方向翻转：同一标的相邻两个决议日的方向类（EXPECTED_DIRECTION；UNCLEAR=none）是否不同。
    键 = (asset, 后一天)。两侧传同一批 (date, asset) 时键集相同，可逐键配对。
    用途：同一标的的输入逐日只有微小变化，裁决方向的逐日翻转里有一部分是纯抽样噪声；
    多票聚合应该把这部分压下去——这是"稳定度更高"唯一能在两臂间同口径比较的量
    （对照臂每天只有 1 票，没有臂内重复可比）。"""
    from datetime import date as _date
    by_asset: Dict[str, List[VerdictReview]] = {}
    for r in rows:
        by_asset.setdefault(r.asset, []).append(r)
    out: Dict[Tuple[str, str], bool] = {}
    for asset, rs in by_asset.items():
        rs = sorted(rs, key=lambda x: x.date)
        for prev, cur in zip(rs, rs[1:]):
            if (_date.fromisoformat(cur.date) - _date.fromisoformat(prev.date)).days > CHURN_MAX_GAP_DAYS:
                continue
            out[(asset, cur.date)] = (EXPECTED_DIRECTION.get(prev.verdict, UNCLEAR_DIRECTION)
                                      != EXPECTED_DIRECTION.get(cur.verdict, UNCLEAR_DIRECTION))
    return out


def _discordant(pairs: List[Tuple[bool, bool]]) -> Dict[str, Any]:
    """[(臂, 对照)] 布尔对 → 仅臂为真 / 仅对照为真 / 都真 / 都假 + 符号检验 p。"""
    b = sum(1 for x, y in pairs if x and not y)
    c = sum(1 for x, y in pairs if y and not x)
    return {"n": len(pairs), "arm_only": b, "control_only": c,
            "both": sum(1 for x, y in pairs if x and y),
            "neither": sum(1 for x, y in pairs if not x and not y),
            "p": sign_test_p(b, c)}


def _fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _fmt_pp(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:+.1f}pp"


def _fmt_ci(ci: Optional[List[float]], pp: bool = False) -> str:
    if not ci:
        return "—"
    return f"{ci[0] * 100:+.1f} ~ {ci[1] * 100:+.1f}pp" if pp else f"{ci[0] * 100:.0f}–{ci[1] * 100:.0f}%"


def _fmt_rate(c: Dict[str, Any]) -> str:
    return f"{c['k']}/{c['n']} = {_fmt_pct(c['rate'])}（95% CI {_fmt_ci(c['ci95'])}）" if c["n"] else "—"


def _paired_row(label: str, d: Dict[str, Any]) -> str:
    p = "—" if d["p"] is None else f"{d['p']:.3f}"
    return (f"| {label} | {d['n']} | {d['arm_only']} | {d['control_only']} | {d['both']} | {d['neither']} | {p} |")


def arm_report_lines(name: str, cmp_: Dict[str, Any], stab: Dict[str, Any]) -> List[str]:
    """实验臂复盘报告正文（markdown 行）。只含聚合，不含 symbol / 逐行裁决 / 金额。"""
    A, C, P = cmp_["arm"], cmp_["control"], cmp_["paired_30d"]
    min_n = cmp_["min_n"]
    lines = [
        f"## 臂内稳定度（{name} · 有逐票记录的 {stab['n']} 个 标的×日）\n",
        f"臂标签：{'、'.join(f'{k} × {v}' for k, v in stab['arm_tags'].items()) or '—'}"
        "（同一目录里出现不止一种说明中途改过 N，别把它们当同一臂读）\n",
    ]
    if not stab["n"]:
        lines.append("_无逐票记录_\n")
    else:
        lines += ["| 指标 | 数 |", "|---|---|",
                  f"| 全票一致 | {_fmt_rate(stab['unanimous'])} |",
                  f"| 方向翻转（N 票方向类不全相同，A4 口径，A4 基线 26.7%） | {_fmt_rate(stab['flip'])} |",
                  f"| 无多数 → HOLD（split） | {_fmt_rate(stab['split'])} |"]
        lines.append(f"\n非全票一致的行 **{stab['n'] - stab['unanimous']['k']}** 条"
                     f"（H2 功效闸：判读日 < {min_n} 条判\"功效不足\"，既不算通过也不算否定）。")
        lines.append(f"\n**HOLD 占比**：单票 {_fmt_pct(stab['hold_share_votes'])} → 聚合后 "
                     f"{_fmt_pct(stab['hold_share_final'])}；同日同标的对照臂 {_fmt_pct(stab['hold_share_control'])}。\n")
        lines += ["### 票数一致度 vs 30d 命中（分裂度带不带信息）\n",
                  "| 一致度 | 行数 | 30d 成熟可评分 | 30d 命中率 |", "|---|---|---|---|"]
        label = {"unanimous": "全票一致", "majority": "多数（非全票）", "split": "无多数 → HOLD"}
        for t in ARM_TIERS:
            c = stab["tiers"][t]
            hit = _fmt_pct(c["hit_rate_30d"]) if c["hit_rate_30d"] is not None else f"n<{min_n} 不展示"
            lines.append(f"| {label[t]} | {c['n']} | {c['n_scored_30d']} | {hit} |")

    bench = "、".join(A.get("benchmark_symbols") or C.get("benchmark_symbols") or []) or "未配置"
    lines += [
        f"\n## 配对对照（同日同标的 {cmp_['matched']} 个；臂共 {cmp_['arm_total']}、对照臂共 {cmp_['control_total']}）\n",
        "> 对照臂 = 同日同标的的 `.backtest` 舰队行（单次抽样）。两臂输入相同（同 market_data / regime / 中性持仓 / "
        "macro_view），差别只在抽样次数与聚合。基率口径同 live 命中率页（同侧样本里永远喊本行方向的傻瓜）。\n",
        f"| | 实验臂 | 对照臂 |", "|---|---|---|",
        f"| 行数 | {A['n_rows']} | {C['n_rows']} |",
        f"| 30d 成熟可评分 n | {A['n_scored_30d']} | {C['n_scored_30d']} |",
    ]
    if A["suppressed"] or C["suppressed"]:
        lines.append(f"| 30d 命中 / 基率 / 提升 / CI | n<{min_n} 不展示（红线 #2） | n<{min_n} 不展示 |")
    lines += [
        f"| 30d 命中率 | {_fmt_pct(A['hit_rate_30d'])} | {_fmt_pct(C['hit_rate_30d'])} |",
        f"| 同尺子基率 | {_fmt_pct(A['base_rate_30d'])} | {_fmt_pct(C['base_rate_30d'])} |",
        f"| 提升 | {_fmt_pp(A['lift_30d'])} | {_fmt_pp(C['lift_30d'])} |",
        f"| 命中率 95% CI | {_fmt_ci(A['ci95_30d'])} | {_fmt_ci(C['ci95_30d'])} |",
        f"| 提升 95% CI（Wilson 区间减基率） | {_fmt_ci(A['lift_ci95_30d'], pp=True)} | {_fmt_ci(C['lift_ci95_30d'], pp=True)} |",
        f"| HOLD 占比 | {_fmt_pct(A['hold_share'])} | {_fmt_pct(C['hold_share'])} |",
        f"| 照做净收益率（30d，扣往返成本，名义加权；n 有金额） | "
        f"{_fmt_pp(A['followed_net_return_30d'])}（n={A['followed_n_30d']}） | "
        f"{_fmt_pp(C['followed_net_return_30d'])}（n={C['followed_n_30d']}） |",
        "\n### 配对检验（同日同标的；只看两臂结果不同的那些对，精确符号检验双侧 p）\n",
        "| 比较 | 对数 | 仅实验臂为真 | 仅对照臂为真 | 都真 | 都假 | p |", "|---|---|---|---|---|---|---|",
        _paired_row("30d 命中（两臂都已成熟可评分）", P),
        _paired_row("逐日方向翻转（相邻决议日方向类不同；越少越稳）", cmp_["paired_churn"]),
        _paired_row("HOLD（越少越不囤）", cmp_["paired_hold"]),
        "",
        f"### 按 verdict 分（超额 = 标的 − 基准 {bench} 同窗口收益，verdict 无关；n<{min_n} 的格子不展示数字）\n",
        "| Verdict | 臂 N | 臂 30d 命中 | 臂 提升 | 臂 超额均值 | 臂 照做净收益率 | 对照 N | 对照 30d 命中 | 对照 提升 | 对照 超额均值 | 对照 照做净收益率 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in [v for v in VERDICT_BUCKETS if v in A["by_verdict"] or v in C["by_verdict"]]:
        cells: List[str] = []
        for side in (A, C):
            c = side["by_verdict"].get(v)
            if not c:
                cells += ["0", "—", "—", "—", "—"]
                continue
            cells += [str(c["n"]), _fmt_pct(c.get("hit_rate_30d")), _fmt_pp(c.get("lift_30d")),
                      _fmt_pp(c.get("excess_mean_30d")), _fmt_pp(c.get("followed_net_return_30d"))]
        lines.append(f"| {v} | " + " | ".join(cells) + " |")
    lines.append(
        "\n> 判据（experiments/sealed_vote/README.md，开跑前冻结）：H1 整侧 30d 提升 CI 下界 > 0；或 H2 配对逐日方向翻转"
        "显著更少（符号检验 p<0.05）且 HOLD 不显著更多——过其一才提 live 切换提案（另一个 PR，用户签字）。"
        "相邻交易日的 30d 窗口高度重叠、同日标的共享市场环境，真实独立样本远少于 n，CI 偏窄、p 偏小。\n")
    return lines


# =====================================================================
# path_review 纯核
# =====================================================================

WINDOWS = ("30d", "60d", "90d")


SHAPE_WINDOW = "90d"


SHAPE_CLASSES = ("dip_then_up", "up_no_dip", "pop_then_down", "down_no_pop")


def realized_shape(real: Dict[str, Any], atr_pct: Optional[float]) -> Optional[str]:
    """与预测同口径的实际形状分类（单位 = 决策日 ATR%）。"""
    if "fwd_90d" not in real or "min_90d" not in real or not atr_pct:
        return None
    up = real["fwd_90d"] > 0
    dipped = real["min_90d"] <= -atr_pct
    popped = real["max_90d"] >= atr_pct
    if up:
        return "dip_then_up" if dipped else "up_no_dip"
    return "pop_then_down" if popped else "down_no_pop"


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------
@dataclass
class PathReview:
    date: str
    asset: str
    regime: str
    source: str                       # live / recompute
    current_price: Optional[float]
    windows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 每窗: {fwd_real, in_band(P10-P90), below_current(实际), p_below(预测),
    #        err_vs_median(实际−预测中位, pp)}
    shape_pred: Dict[str, float] = field(default_factory=dict)
    shape_real: Optional[str] = None
    shape_prob_score: Optional[float] = None   # 预测分布给实际类的概率
    shape_top1_hit: Optional[bool] = None
    trough_pred_days: Optional[int] = None
    trough_real_days: Optional[int] = None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize_path_reviews(reviews: List[PathReview]) -> Dict[str, Any]:
    # 周末重复快照（基准=周五收盘）不进校准；jsonl 行保留（D8）
    total = len(reviews)
    reviews = [r for r in reviews if not is_closed_weekend(r.asset, r.date)]
    summ: Dict[str, Any] = {"n": len(reviews), "weekend_dup_excluded": total - len(reviews),
                            "windows": {}, "shape": {}}
    for w in WINDOWS:
        rows = [r.windows[w] for r in reviews if w in r.windows]
        if not rows:
            continue
        n = len(rows)
        cov = sum(1 for x in rows if x["in_band"]) / n
        # p_below 校准：Brier vs 基率 Brier（越低越好）
        briers = [(x["p_below"] - (1.0 if x["below_current"] else 0.0)) ** 2 for x in rows]
        base = sum(1 for x in rows if x["below_current"]) / n
        base_briers = [(base - (1.0 if x["below_current"] else 0.0)) ** 2 for x in rows]
        errs = [x["err_vs_median"] for x in rows]
        summ["windows"][w] = {
            "n": n,
            "band_coverage": round(cov, 3),          # 目标 ~0.80
            "p_below_brier": round(float(np.mean(briers)), 4),
            "base_rate_brier": round(float(np.mean(base_briers)), 4),
            "median_abs_err_pp": round(float(np.median([abs(e) for e in errs])), 2),
            "median_err_pp": round(float(np.median(errs)), 2),  # 系统性偏差（+=预测偏保守）
        }
    sh = [r for r in reviews if r.shape_real]
    if sh:
        n = len(sh)
        summ["shape"] = {
            "n": n,
            "prob_score_mean": round(float(np.mean([r.shape_prob_score for r in sh])), 4),
            "uniform_baseline": 0.25,
            "top1_hit": round(sum(1 for r in sh if r.shape_top1_hit) / n, 3),
            "trough_mae_days": round(float(np.mean([
                abs(r.trough_real_days - r.trough_pred_days)
                for r in sh
                if r.trough_real_days is not None and r.trough_pred_days is not None
            ])), 1) if any(
                r.trough_real_days is not None and r.trough_pred_days is not None
                for r in sh
            ) else None,
        }
    # 按 regime 分桶（解读"哪个 regime 的路径分布最可信"）
    summ["by_regime"] = {}
    for rg in sorted({r.regime for r in reviews}):
        rows = [r.windows.get("90d") for r in reviews
                if r.regime == rg and "90d" in r.windows]
        if rows:
            summ["by_regime"][rg] = {
                "n": len(rows),
                "band_coverage_90d": round(
                    sum(1 for x in rows if x["in_band"]) / len(rows), 3),
            }
    return summ



__all__ = [
    "HIT_WINDOWS", "CONTAMINATION_CUTOFF", "EXPECTED_DIRECTION",
    "UNCLEAR_DIRECTION", "VERDICT_BUCKETS", "DEGRADED_EMPTY_INPUTS",
    "wilson_ci", "is_degraded_input", "is_forced_hold_review",
    "ROUNDTRIP_COST_BPS", "DEFAULT_ROUNDTRIP_COST_BPS", "roundtrip_cost_rate", "counterfactual_pnl",
    "VerdictReview", "K_FLAT", "FLAT_CEILING_PCT",
    "_flat_band", "_is_hit", "_summarize_bucket",
    "summarize_verdict_reviews", "_bucket_lines",
    "CALIBRATION_BINS", "CALIBRATION_MIN_N", "calibration_from_pairs", "calibration_summary",
    "neutrality_summary", "_calibration_lines", "_neutrality_lines",
    "LOOKUP_MIN_N", "LOOKUP_FORCED_CONF", "build_confidence_lookup", "merge_confidence_lookup",
    "is_forced_or_low_confidence", "confidence_display",
    "WINDOWS", "SHAPE_WINDOW", "SHAPE_CLASSES",
    "realized_shape", "PathReview", "summarize_path_reviews",
]
