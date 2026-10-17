"""verdict/path review 统计纯核（域绑定纯模块，ADR-026）

从 jobs/verdict_review.py 与 jobs/path_review.py 拆出的纯计算：命中判定、
分桶统计、路径形状分类、校准汇总。全部函数吃传入的 reviews 列表 / 数值，
零 IO——行情拉取（_closes/_window_return/_detect_macro_shock/realized_path）
与文件读写留在各自 shell。

两个 summarize 因重名改为 summarize_verdict_reviews / summarize_path_reviews；
shell 各自 `import ... as summarize` 保持历史调用面。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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

    return {
        "total": total,
        "weekend_dup_excluded": total - len(reviews),
        "cutoff": CONTAMINATION_CUTOFF,
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
    "LOOKUP_MIN_N", "LOOKUP_FORCED_CONF", "build_confidence_lookup", "merge_confidence_lookup",
    "is_forced_or_low_confidence", "confidence_display",
    "WINDOWS", "SHAPE_WINDOW", "SHAPE_CLASSES",
    "realized_shape", "PathReview", "summarize_path_reviews",
]
