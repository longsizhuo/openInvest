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


# ---- 模型分桶（model_era）：换模型后新旧账分开算 ----
# 2026-10-18 生产委员会 LLM 从 deepseek-v4-flash 换成 claude-sonnet-5-5（#483）。复盘页 / 公开 JSON /
# 校准 / 反事实把两种模型的裁决混在一起算 → 实验在换模型后没法继续。model_era = "<谁跑的>:<模型名>"：
#   Direct 路径 → "direct:<模型>"（Input Health 的 model，落盘时刻的全局 LLM 配置）
#   Coordinator → "<provider>:<模型>"（Input Health 的 provider + host 传给 save_committee 的 model，
#                 没传就是 "<provider>:unknown"，如 "hermes:claude-haiku-5-5" / "claude:unknown"）
#   老存档没有 Input Health 块 → 只能按日期归：决议日 ≤ MODEL_ERA_CUTOFF = 换模型前唯一的 DeepSeek，
#                 否则 "unknown"（不猜）
# 单一可信源在这里：verdict_review（写 jsonl）与 scripts/export_accuracy（读 jsonl，老行没有 model_era 字段时重算）共用。
MODEL_ERA_CUTOFF = "2026-10-18"
PRE_HEALTH_ERA = "direct:deepseek-v4-flash(pre-health)"
UNKNOWN_ERA = "unknown"
MODEL_ERA_MIN_N = 30      # 红线 #2 同阈值：子桶 30d 已成熟可评分 < 30 只给 n
DEFAULT_LOOKUP_MODEL = "deepseek-v4-flash"   # 包内默认查表只含舰队样本，舰队钉 DeepSeek（docs/wiki/12 8.4）


def model_era_of(input_health: Optional[Dict[str, Any]], date: str, provider: Optional[str] = None) -> str:
    """(Input Health, 决议日[, Coordinator 的 Provider 行]) → model_era。纯函数。

    provider：老存档没有 Input Health 块、但 transcript 有 "**Provider**: X (skill mode)" 行 = Coordinator（X 扮演角色，
    模型名当时不落盘）→ "X:unknown"，不能按日期算成 DeepSeek（评审 #497：否则它们混进 Sonnet 前的“旧模型”基线）。"""
    h = input_health or {}
    if not h:
        if provider:
            return f"{str(provider).strip().lower()}:unknown"
        # A calendar cutoff is not evidence of the model used by this run.
        return UNKNOWN_ERA
    who = (str(h.get("provider") or "").strip().lower() or "coordinator") \
        if h.get("entry") == "coordinator" else "direct"
    return f"{who}:{str(h.get('model') or '').strip() or 'unknown'}"


def model_key(name: Optional[str]) -> str:
    """模型名 → 比较用的键（小写，去掉 "(pre-health)" 后缀）。config 里的原始模型名也走它（可能自带 ":"，不能当 era 拆）。"""
    return str(name or "").strip().removesuffix("(pre-health)").strip().lower()


def era_model(era: str) -> str:
    """model_era → 模型键（去掉 "<谁>:" 前缀）。查表同口径、四臂混模型提示用。"""
    return model_key(era.split(":", 1)[1] if ":" in era else era)


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
    # 派生字段：谁用哪个模型跑的这条裁决（见上 model_era_of）。构造时留空即按 input_health + date 派生；
    # 落进 jsonl 供下游分桶（export_accuracy 读老 jsonl 没有此键时按同一函数重算）。
    model_era: str = ""

    def __post_init__(self) -> None:
        if not self.model_era:
            self.model_era = model_era_of(self.input_health, self.date)


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


def summarize_by_model(reviews: List[VerdictReview], *, min_n: int = MODEL_ERA_MIN_N) -> Dict[str, Any]:
    """按 model_era 分子桶（给 holdout 用）：各自 n、命中、基率/提升/CI、反事实（=_summarize_bucket）+ 校准 + 中性度。

    换了模型，新模型的账不该和旧模型的账混成一个命中率。子桶的闸按**30d 已成熟可评分行数**（有 hits["30d"]）：
    < min_n 的子桶**只给 n**（不出命中/基率/反事实/校准/中性度——红线 #2；注意总行数 ≥30 但还没成熟时
    同样没有可展示的数，所以闸不能只看总行数）。校准 / 中性度只用该子桶的 live 行（与全局口径一致）。
    子桶按最早决议日排序（旧模型在前）。
    """
    out: Dict[str, Any] = {}
    eras = sorted({r.model_era for r in reviews},
                  key=lambda e: (min(r.date for r in reviews if r.model_era == e), e))
    for era in eras:
        rows = [r for r in reviews if r.model_era == era]
        n_mat = sum(1 for r in rows if "30d" in r.hits)
        if n_mat < min_n:
            out[era] = {"n": len(rows), "n_matured_30d": n_mat, "rates_suppressed_sub30": True,
                        "live_count": sum(1 for r in rows if r.source == "live"),
                        "backtest_count": sum(1 for r in rows if r.source == "backtest")}
            continue
        live = [r for r in rows if r.source == "live"]
        out[era] = {**_summarize_bucket(rows, suppress_rates=False), "n_matured_30d": n_mat,
                    "calibration": calibration_summary(live), "neutrality": neutrality_summary(live)}
    return out


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
        "holdout": {**_summarize_bucket(holdout, suppress_rates=len(holdout) < 30),
                    "by_model": summarize_by_model(holdout)},
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



def _model_lines(by_model: Dict[str, Any]) -> List[str]:
    """报告页「按模型」一节：先一张索引表（每个 model_era 一行），再给数够的子桶各出一份完整分节。
    n<30 的子桶只给 n（summarize_by_model 已把数字剥掉）。"""
    lines = ["\n## 按模型（holdout 内按 model_era 分桶；换模型后新旧账分开算，不混）\n",
             "> model_era = 谁跑的:模型名。direct = Direct 路径（模型 = 落盘时刻的全局 LLM 配置）；"
             "hermes / claude 等 = Coordinator 的 host（模型名由 host 传给 save_committee，没传 = unknown）；"
             "`(pre-health)` = 老存档没有 Input Health 块，按决议日 ≤ "
             f"{MODEL_ERA_CUTOFF} 归 DeepSeek（有 `Provider: X (skill mode)` 行的是 Coordinator，归 X:unknown）。30d 已成熟可评分 < {MODEL_ERA_MIN_N} 的子桶只给 n（红线 #2）。\n",
             "| model_era | N | 30d 已成熟 | 30d 命中率 | 95% CI |", "|---|---|---|---|---|"]
    for era, b in by_model.items():
        if b.get("rates_suppressed_sub30"):
            lines.append(f"| {era} | {b['n']} | {b['n_matured_30d']} | n<{MODEL_ERA_MIN_N} 只给 n | — |")
            continue
        w = b["by_window"].get("30d", {})
        lines.append(f"| {era} | {b['n']} | {b['n_matured_30d']} | {_fmt_pct(w.get('hit_rate'))} | "
                     f"{_fmt_ci(w.get('ci95'))} |")
    if len(by_model) < 2:
        return lines + ["\n_只有一个 model_era，细节同上 Holdout 一节。_\n"]
    for era, b in by_model.items():
        if b.get("rates_suppressed_sub30"):
            continue
        scope = f" · {era}"
        lines += _bucket_lines(f"按模型 · {era}", b)
        lines += _calibration_lines(b["calibration"], scope)
        neu = b["neutrality"]
        lines += (_neutrality_lines(neu, scope) if neu["n"] >= MODEL_ERA_MIN_N
                  else [f"\n## 裁决中性度（{era}）\n", f"📊 live 样本量 {neu['n']} < {MODEL_ERA_MIN_N} —— 只给 n。\n"])
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
    out["diagnostic_only"] = True
    out["assumption"] = "Legacy assumption: CIO confidence equals directional correctness probability; not a defined forecast event"
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


def _calibration_lines(cal: Dict[str, Any], scope: str = "") -> List[str]:
    if cal.get("diagnostic_only"):
        return ["\n## 概率校准\n", "暂无具有明确事件、期限和概率声明的合格预测。旧自报分诊断仅供内部审计，不作为概率或策略收益证据。\n"]
    lines = [f"\n## 置信度校准（CIO 自报 vs {cal.get('window', '30d')} 实际命中 · live 干净样本 n={cal['n']}{scope}）\n"]
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


def _neutrality_lines(neu: Dict[str, Any], scope: str = "") -> List[str]:
    lines = [f"\n## 裁决中性度（裁决分布 vs 同样本市场 {neu.get('window', '30d')} 实际方向 · live 干净样本 n={neu['n']}{scope}）\n"]
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
                            fleet: List[VerdictReview] = (), *,
                            model: Optional[str] = None) -> Dict[str, Any]:
    """按 verdict 的 30d 查表。样本 = live（source=="live"）+ 舰队，都要非污染、30d 已成熟、
    非周末重复（is_closed_weekend），不按自报分剔除（≤0.4 只在展示时打标）。HOLD 的 rate =
    30 天涨跌留在 flat band 内的比例（= hits["30d"]）；有方向的 = 方向判对比例。
    market_flat = 同一批样本（不分 verdict）30 天落在 flat band 内的比例，给 HOLD 当基率对照。
    每格带来源构成 n_live / n_fleet。n<30 → rate=None。

    model（当前 config 的模型名）给了 → 只收 era_model(model_era) == model 的行：换模型后，旧模型的
    30d 命中率不能当新模型的"同类决议之后怎样"给用户看。不够 30 的格子照旧 rate=None，由
    merge_confidence_lookup 退回默认表并注明。None → 不过滤（默认表生成器 / 老调用方）。"""
    want = model_key(model)

    def _ok(r: VerdictReview) -> bool:
        return (not r.contaminated and "30d" in r.hits and r.verdict in EXPECTED_DIRECTION
                and not is_closed_weekend(r.asset, r.date)
                and (not want or era_model(r.model_era) == want))

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
        **({"model": model} if want else {}),
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
    # 本机表是按哪个模型建的（build_confidence_lookup 的 model）+ 默认表（只含舰队，舰队钉 DeepSeek）是哪个模型：
    # 退到默认表时展示端据此注明"不是当前模型的样本"。本机表没标模型（老文件 / 测试）→ 不注明，行为不变。
    meta = {k: v for k, v in (("model", tables["local"].get("model")),
                              ("default_model", tables["default"].get("model") or DEFAULT_LOOKUP_MODEL)) if v}
    return {"by_verdict": out, **(meta if meta.get("model") else {})}


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
    model, dmodel = (lookup or {}).get("model"), (lookup or {}).get("default_model")
    if cell.get("from") == "default":
        note = (f"默认表，{dmodel} 舰队样本，当前 {model} 样本不足"
                if model and dmodel and model_key(model) != model_key(dmodel) else "默认表")
    else:
        note = "含纸面舰队样本" if cell.get("n_fleet") else "本机样本"
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


def aggregate_votes(votes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """N 次独立抽样 → 一条聚合裁决（纯函数，无 IO；sealed 臂的写侧 scripts/backtest_committee 与读侧
    vote_stability 共用这一份，多数判定只在这里定义一次）。

    votes: [{"verdict": str, "confidence": float, "alloc": float}, ...]，按抽样顺序。
    - verdict：严格多数（票数 > N/2；N=3 即 ≥2 票相同）。没有多数（N=3 三者各异、N=4 的 2:2 …）
      → HOLD 且 split=True。
    - confidence := 最大票占比（N=3：1.0 / 0.67；三者各异 0.33），即用投票分裂度代替 CIO 自报分。
    - alloc := 多数票那几次金额的中位数；HOLD（含 split）恒 0（HOLD 不带建议金额）。
    - flip：N 次裁决的方向类（EXPECTED_DIRECTION；UNCLEAR=none）不全相同——与 A4
      （experiments/verdict_variance）的翻转口径同一把尺子，数字可直接对比。
    - rep：代表票下标 = 多数票里最早的一次（split 取 0）；落盘的 transcript 正文取它。
    """
    import statistics
    n = len(votes)
    if n < 1:
        raise ValueError("aggregate_votes 需要至少 1 票")
    top_verdict, top = Counter(v["verdict"] for v in votes).most_common(1)[0]
    split = top * 2 <= n
    verdict = "HOLD" if split else top_verdict
    majority = [i for i, v in enumerate(votes) if v["verdict"] == top_verdict]
    alloc = 0 if verdict == "HOLD" else round(statistics.median(votes[i]["alloc"] for i in majority))
    dirs = {EXPECTED_DIRECTION.get(v["verdict"], UNCLEAR_DIRECTION) for v in votes}
    return {
        "verdict": verdict,
        "confidence": round(top / n, 2),
        "alloc": alloc,
        "split": split,
        "flip": len(dirs) > 1,
        "rep": 0 if split else majority[0],
    }


def _vote_tier(votes: List[str]) -> str:
    """全票一致 / 严格多数 / 无严格多数（聚合成 HOLD）。多数判定直接用 aggregate_votes，不另写一份。"""
    if len(set(votes)) == 1:
        return "unanimous"
    agg = aggregate_votes([{"verdict": v, "alloc": 0} for v in votes])
    return "split" if agg["split"] else "majority"


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
    """与 summarize_verdict_reviews 的 holdout 同口径：非污染、非休市周末重复；另外，实验臂行的 macro_view
    与对照臂同日同标的那份不是同一份（Input Health 的 macro_shared 为 False：对照臂当天被续跑跳过、或中途失败重跑）
    就不是“同一份输入”的配对，剔掉并在臂一览里计数。没有这个键（对照臂行 / 早期存档）按未知保留。"""
    return (not r.contaminated and not is_closed_weekend(r.asset, r.date)
            and (is_model_arm_row(r) or (r.input_health or {}).get("macro_shared") is not False))


# 模型臂（B4 之外的第五类实验臂，--sonnet-arm）：同一批标的、live 配置，只把 LLM 换了。它的 Macro 视图由**那个模型自己**生成
# （换模型的一部分），所以 macro_shared 恒为 False——但那是设计，不是“输入不是同一份”的事故，不能像其他臂那样按它剔行。
# 比较口径：Macro 不共享，对照臂与模型臂的差 = 整条流水线换模型（Macro + Quant + Risk + CIO 全是新模型）的总效应。
SONNET_ARM_TAG = "sonnet_live_cfg"   # Input Health 的 arm 标签；写侧 scripts/backtest_committee 与这里共用
MODEL_ARM_TAGS = (SONNET_ARM_TAG,)


def is_model_arm_row(r: VerdictReview) -> bool:
    return (r.input_health or {}).get("arm") in MODEL_ARM_TAGS


def macro_unshared_count(rows: List[VerdictReview]) -> int:
    return sum(1 for r in rows if (r.input_health or {}).get("macro_shared") is False and not is_model_arm_row(r))


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


ARM_ORDER = ("sealed", "debate", "macrosoft")
ARM_LABELS = {
    "control": "对照 1r×1（现有舰队产物）",
    "sealed": "sealed 1r×N票（密封投票）",
    "debate": "debate 4r×1（live 日报配置）",
    "macrosoft": "macro-soft 1r×1（CIO 规则 2 打折不否决）",
    "sonnet": "sonnet 1r×1（live 配置，LLM 换 Sonnet；Macro 由 Sonnet 自己生成）",
}
MODEL_ARM = "sonnet"   # 模型臂：单列「模型对照」，不进四臂并排 / H1–H3
DIRECTIONAL_BUY = ("BUY", "ACCUMULATE")


def arm_rounds_summary(arm: List[VerdictReview]) -> Dict[str, Any]:
    """debate 臂：实际辩论轮数与收敛率（Input Health 的 final_round / converged；没有的行不算）。"""
    got = [((r.input_health or {}).get("final_round"), (r.input_health or {}).get("converged")) for r in arm]
    got = [(int(fr), bool(cv)) for fr, cv in got if isinstance(fr, int)]
    n = len(got)
    return {"n": n, "mean_rounds": round(sum(fr for fr, _ in got) / n, 2) if n else None,
            "converged": _rate_ci(sum(1 for _, cv in got if cv), n)}


def _mean_ci(xs: List[float]) -> Optional[Dict[str, float]]:
    """均值与正态近似 95% CI（n ≥ 2）。n 小或偏态时偏窄——调用方先过 n ≥ min_n 闸。"""
    import math
    import statistics
    if len(xs) < 2:
        return None
    m, sd = statistics.fmean(xs), statistics.stdev(xs)
    half = 1.96 * sd / math.sqrt(len(xs))
    return {"mean": round(m, 4), "lo": round(m - half, 4), "hi": round(m + half, 4)}


def macro_soft_h3(arm: List[VerdictReview], control: List[VerdictReview], *,
                  min_n: int = ARM_MIN_N) -> Dict[str, Any]:
    """H3（macro-soft 臂）：被"解锁"的买入——臂给了 BUY/ACCUMULATE、而对照臂同日同标的没有买——
    事后值不值：30d 照做净收益率（扣往返成本、对比留现金，复用 #442）与相对基准的 30d 超额
    （取对照臂同日同标的行的 excess_returns，verdict 无关，同一标的同一窗口两臂本就相同）。
    每个数字按自己的分母做 n<min_n 抑制。达标 = 成熟解锁行 n ≥ min_n 且 超额均值 95% CI 下界 > 0 且 照做净收益率 > 0。"""
    c_by = {(r.date, r.asset): r for r in control if _arm_row_ok(r)}
    rows = [r for r in arm if _arm_row_ok(r)]
    directional = [r for r in rows if r.verdict in DIRECTIONAL_BUY]
    unlocked = [r for r in directional
                if (r.date, r.asset) in c_by and c_by[(r.date, r.asset)].verdict not in DIRECTIONAL_BUY]
    matured = [r for r in unlocked if "30d" in r.actual_returns]
    ex = [c_by[(r.date, r.asset)].excess_returns["30d"] for r in matured
          if "30d" in c_by[(r.date, r.asset)].excess_returns]
    rets = [r.actual_returns["30d"] for r in matured]
    followed = _summarize_bucket(unlocked, suppress_rates=False)
    f_n = followed.get("followed_n_30d", 0)
    f_net = followed.get("followed_net_return_30d") if f_n >= min_n else None
    ex_ci = _mean_ci(ex) if len(ex) >= min_n else None
    soft_vetoed = [r for r in rows if (r.input_health or {}).get("macro_signal") == "risk_off"
                   and isinstance((r.input_health or {}).get("macro_strength"), (int, float))
                   and (r.input_health or {}).get("macro_strength") >= 8]
    n_risk_off = sum(1 for r in rows if (r.input_health or {}).get("macro_signal") == "risk_off")
    if len(matured) < min_n or ex_ci is None or f_net is None:
        status = "insufficient"
    else:
        status = "pass" if (ex_ci["lo"] > 0 and f_net > 0) else "fail"
    return {
        "rows": len(rows), "directional": len(directional), "unlocked": len(unlocked),
        "agree_with_control": sum(1 for r in directional
                                  if (r.date, r.asset) in c_by and c_by[(r.date, r.asset)].verdict in DIRECTIONAL_BUY),
        "matured": len(matured), "min_n": min_n,
        "mean_return_30d": round(sum(rets) / len(rets), 4) if len(rets) >= min_n else None,
        "excess_30d": ex_ci, "followed_net_return_30d": f_net, "followed_n_30d": f_n,
        "n_macro_risk_off": n_risk_off, "n_still_vetoed_strength_ge_8": len(soft_vetoed),
        "benchmark_symbols": followed.get("benchmark_symbols", []),
        "status": status,
    }


def side_by_side(control: List[VerdictReview], arms: Dict[str, List[VerdictReview]], *,
                 min_n: int = ARM_MIN_N) -> Dict[str, Any]:
    """所有在场的臂（含对照臂）同时具备的 (日期, 标的) 键上并排——每一侧是同一批输入、同一批市场结果。"""
    sides_rows = {"control": {(r.date, r.asset): r for r in control if _arm_row_ok(r)}}
    sides_rows.update({n: {(r.date, r.asset): r for r in rows if _arm_row_ok(r)} for n, rows in arms.items()})
    keys = sorted(set.intersection(*[set(d) for d in sides_rows.values()])) if sides_rows else []
    out: Dict[str, Any] = {"keys": len(keys), "min_n": min_n, "sides": {}}
    for n, by in sides_rows.items():
        rows = [by[k] for k in keys]
        side = _side_summary(rows, min_n)
        churn = direction_churn(rows)
        side["churn"] = _rate_ci(sum(churn.values()), len(churn))
        side["directional_rows"] = sum(1 for r in rows if r.verdict in EXPECTED_DIRECTION and r.verdict != "HOLD")
        out["sides"][n] = side
    return out


_SHARED_INPUT_NOTE = (
    "> 对照臂 = 同日同标的的 `.backtest` 舰队行（rounds=1、单次抽样）。两臂的 market_data / regime / 中性持仓 / "
    "macro_view 逐字相同，差别只在该臂自己的那个变量。基率口径同 live 命中率页（同侧样本里永远喊本行方向的傻瓜）。\n")


def arm_report_lines(name: str, cmp_: Dict[str, Any], stab: Optional[Dict[str, Any]], *,
                     input_note: str = _SHARED_INPUT_NOTE) -> List[str]:
    """单个实验臂 vs 对照臂的报告片段（markdown 行）。只含聚合，不含 symbol / 逐行裁决 / 金额。
    stab=None（debate / macro-soft 没有逐票记录）→ 不出臂内稳定度一节。"""
    A, C, P = cmp_["arm"], cmp_["control"], cmp_["paired_30d"]
    min_n = cmp_["min_n"]
    lines: List[str] = []
    if stab is not None:
        lines += [
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
        f"\n## 配对对照（{name} vs 对照臂 · 同日同标的 {cmp_['matched']} 个；臂共 {cmp_['arm_total']}、对照臂共 {cmp_['control_total']}）\n",
        *([input_note] if input_note else []),
        f"| | {name} | 对照臂 |", "|---|---|---|",
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
        f"\n### 配对检验（{name} vs 对照臂；同日同标的，只看两臂结果不同的那些对，精确符号检验双侧 p）\n",
        f"| 比较 | 对数 | 仅{name}为真 | 仅对照臂为真 | 都真 | 都假 | p |", "|---|---|---|---|---|---|---|",
        _paired_row("30d 命中（两臂都已成熟可评分）", P),
        _paired_row("逐日方向翻转（相邻决议日方向类不同；越少越稳）", cmp_["paired_churn"]),
        _paired_row("HOLD（越少越不囤）", cmp_["paired_hold"]),
        "",
        f"### 按 verdict 分（超额 = 标的 − 基准 {bench} 同窗口收益，verdict 无关；n<{min_n} 的格子不展示数字）\n",
        f"| Verdict | {name} N | {name} 30d 命中 | {name} 提升 | {name} 超额均值 | {name} 照做净收益率 | 对照 N | 对照 30d 命中 | 对照 提升 | 对照 超额均值 | 对照 照做净收益率 |",
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
    return lines


def _era_counts(rows: List[VerdictReview]) -> str:
    """一批行的 model_era 计数（"direct:xxx × 12、direct:yyy × 3"）。"""
    c = Counter(r.model_era for r in rows)
    return "、".join(f"{e} × {n}" for e, n in sorted(c.items())) or "—"


def arms_report_lines(arms: Dict[str, List[VerdictReview]], control: List[VerdictReview], *,
                      min_n: int = ARM_MIN_N) -> List[str]:
    """全部实验臂的复盘报告（markdown 行）：臂一览 → 四臂并排 → 每臂 vs 对照臂 → debate 轮数 → macro-soft H3。
    arms: {"sealed": rows, "debate": rows, "macrosoft": rows}（没跑的臂不要放进来）。"""
    names = [n for n in ARM_ORDER if n in arms]
    model_rows = arms.get(MODEL_ARM)   # 模型臂（--sonnet-arm）：不进四臂并排 / H1–H3，单列「模型对照」
    cols = ["control"] + names
    ctl = [r for r in control if _arm_row_ok(r)]
    lines = ["## 臂一览\n", "| 臂 | 配置 | 模型（model_era × 行数） | 臂内行数 | macro 与对照不是同一份而剔除的行 |",
             "|---|---|---|---|---|",
             f"| control | {ARM_LABELS['control']} | {_era_counts(ctl)} | {len(ctl)} | — |"]
    lines += [f"| {n} | {ARM_LABELS[n]} | {_era_counts(arms[n])} | {len(arms[n])} | {macro_unshared_count(arms[n])} |"
              for n in names]
    if model_rows is not None:
        lines.append(f"| {MODEL_ARM} | {ARM_LABELS[MODEL_ARM]} | {_era_counts(model_rows)} | {len(model_rows)} | "
                     "n/a（模型臂的 Macro 本就由它自己生成，不按 macro_shared 剔行） |")
    # 「不止一个模型」只看对照臂 + 四臂：模型臂换模型是它的设计变量，不算混用
    all_models = [era_model(r.model_era) for rows in [ctl, *(arms[n] for n in names)] for r in rows]
    models = set(all_models) - {"unknown"}      # 没记录模型的行（模型名落盘前的老存档）不算一个“模型”
    if len(models) > 1:
        lines.append("\n> ⚠️ 臂内 / 臂间用了不止一个模型（" + "、".join(sorted(models)) + "）：同日同标的配对里模型不同 = 多了一个混淆变量，"
                     "判读时按上表分开看，别把它们当同一臂读。\n")
    if "unknown" in all_models:
        lines.append(f"\n> 另有 {all_models.count('unknown')} 行没有记录模型（老存档 / host 没传），上表标 unknown，不参与上面的混用判断。\n")
    if names:
        lines += _four_arm_lines(arms, control, names, cols, min_n)
    if model_rows is not None:
        lines += _model_arm_lines(model_rows, control, ctl, min_n)
    return lines


def _four_arm_lines(arms: Dict[str, List[VerdictReview]], control: List[VerdictReview],
                    names: List[str], cols: List[str], min_n: int) -> List[str]:
    """四臂并排 → 每臂 vs 对照臂 → debate 轮数 → macro-soft H3 → 判据脚注（原 arms_report_lines 主体，行为不变）。"""
    sb = side_by_side(control, {n: arms[n] for n in names}, min_n=min_n)
    lines: List[str] = []
    lines += [
        f"\n## 并排（所有在场的臂同时具备的 {sb['keys']} 个 标的×日；同一批输入、同一批市场结果）\n",
        "| | " + " | ".join(c for c in cols) + " |", "|---|" + "---|" * len(cols)]

    def row(label: str, fn) -> None:
        lines.append(f"| {label} | " + " | ".join(fn(sb["sides"][c]) for c in cols) + " |")

    row("30d 成熟可评分 n", lambda s: str(s["n_scored_30d"]))
    if any(s["suppressed"] for s in sb["sides"].values()):
        lines.append(f"| 30d 命中 / 基率 / 提升 / CI | " + " | ".join(
            f"n<{min_n} 不展示" if sb["sides"][c]["suppressed"] else "已展示" for c in cols) + " |")
    row("30d 命中率", lambda s: _fmt_pct(s["hit_rate_30d"]))
    row("同尺子基率", lambda s: _fmt_pct(s["base_rate_30d"]))
    row("提升", lambda s: _fmt_pp(s["lift_30d"]))
    row("提升 95% CI", lambda s: _fmt_ci(s["lift_ci95_30d"], pp=True))
    row("HOLD 占比", lambda s: _fmt_pct(s["hold_share"]))
    row("非 HOLD 裁决行数", lambda s: str(s["directional_rows"]))
    row("逐日方向翻转率（越低越稳）", lambda s: _fmt_rate(s["churn"]))
    row("照做净收益率（30d，n 有金额）",
        lambda s: f"{_fmt_pp(s['followed_net_return_30d'])}（n={s['followed_n_30d']}）")
    lines.append("")

    cmps = {n: arm_vs_control(arms[n], control, min_n=min_n) for n in names}
    for n in names:
        has_votes = any(arm_vote_verdicts(r) for r in arms[n])
        lines.append("")
        lines += arm_report_lines(n, cmps[n], vote_stability(arms[n], control, min_n=min_n) if has_votes else None)

    if "debate" in arms:
        d = arm_rounds_summary(arms["debate"])
        lines += ["", "## debate 臂：辩论实际跑了几轮（上限 4）\n",
                  f"有轮数记录的 {d['n']} 行：平均 {d['mean_rounds']} 轮；提前收敛（连续 2 轮意见稳定）{_fmt_rate(d['converged'])}。\n"]
    if "macrosoft" in arms:
        h = macro_soft_h3(arms["macrosoft"], control, min_n=min_n)
        ex = h["excess_30d"]
        bench = "、".join(h["benchmark_symbols"]) or "未配置"
        status = {"pass": "达标", "fail": "未达标", "insufficient": f"成熟解锁行不足（需 ≥{min_n}）"}[h["status"]]
        lines += [
            "", "## H3：macro-soft 臂“解锁”的买入（臂给了 BUY/ACCUMULATE、对照臂同日同标的没有买）\n",
            "| 项 | 数 |", "|---|---|",
            f"| 臂内 BUY/ACCUMULATE 行 | {h['directional']}（对照臂也买 {h['agree_with_control']}，解锁 {h['unlocked']}） |",
            f"| Macro risk_off 的行 / 其中 STRENGTH ≥ 8 仍否决的行 | {h['n_macro_risk_off']} / {h['n_still_vetoed_strength_ge_8']} |",
            f"| 解锁行 30d 成熟 n | {h['matured']} |",
            f"| 解锁行 30d 平均收益 | {_fmt_pct(h['mean_return_30d'])} |",
            f"| 解锁行 30d 平均超额（基准 {bench}；对照臂同日同标的行） | "
            + ("—" if ex is None else f"{ex['mean'] * 100:+.2f}pp（95% CI {ex['lo'] * 100:+.2f} ~ {ex['hi'] * 100:+.2f}pp）") + " |",
            f"| 解锁行 30d 照做净收益率（扣往返成本，名义加权，n 有金额） | {_fmt_pp(h['followed_net_return_30d'])}（n={h['followed_n_30d']}） |",
            f"| **H3** | **{status}** |", ""]
    lines.append(
        "\n> 判据（experiments/sealed_vote/README.md，开跑前冻结，判读 +65 天）：H1 sealed 整侧 30d 提升 CI 下界 > 0；"
        "H2 sealed 配对逐日方向翻转显著更少（符号检验 p<0.05）且 HOLD 不显著更多；H3 macro-soft 解锁行成熟 n ≥ 30 且 30d 超额 95% CI 下界 > 0 "
        "且照做净收益率 > 0。debate 臂回答“辩论有没有害”（与对照臂比 HOLD / 翻转 / 提升）。"
        "过其一才提 live 切换提案（另一个 PR，用户签字）。相邻交易日的 30d 窗口高度重叠、同日标的共享市场环境，"
        "真实独立样本远少于 n，CI 偏窄、p 偏小。\n")
    return lines


def _model_arm_lines(rows: List[VerdictReview], control: List[VerdictReview],
                     ctl: List[VerdictReview], min_n: int) -> List[str]:
    """「模型对照」一节：模型臂 vs 对照臂，同日同标的配对（命中 / 超额 / HOLD 占比 / 照做净收益率）。"""
    models = "、".join(sorted({era_model(r.model_era) for r in rows})) or "?"
    ctl_models = "、".join(sorted({era_model(r.model_era) for r in ctl})) or "?"
    note = (f"> 口径：模型臂 = 同一批标的、同一份 market_data / regime / 中性持仓、同一套 live 配置（rounds=1、单次抽样），"
            f"只把 LLM 换成 {models}（对照臂 {ctl_models}）。**Macro 视图不共享**：模型臂的 Macro 由它自己的模型生成（换模型的一部分），"
            "所以 Input Health 的 macro_shared 恒为 False——但本节**不因此剔行**（其余臂会剔）。"
            "因此差异 = 整条流水线（Macro + Quant + Risk + CIO）一起换模型的总效应，不能拆给某一个角色。"
            "基率口径同 live 命中率页。\n")
    cmp_ = arm_vs_control(rows, control, min_n=min_n)
    return ["", f"## 模型对照（{MODEL_ARM} 臂 vs 对照臂 · 换 LLM 的总效应）\n", note,
            "臂内稳定度（逐票翻转 / 一致率 / 无多数）：**n/a**——模型臂是单次抽样（1 票），没有逐票记录；"
            "下面「逐日方向翻转」是跨日配对口径，仍然适用。\n",
            *arm_report_lines(MODEL_ARM, cmp_, None, input_note="")]


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
    forecast_qualification: Dict[str, Any] = field(default_factory=dict)


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
    "summarize_verdict_reviews", "summarize_by_model", "_bucket_lines", "_model_lines",
    "MODEL_ERA_CUTOFF", "PRE_HEALTH_ERA", "UNKNOWN_ERA", "MODEL_ERA_MIN_N", "DEFAULT_LOOKUP_MODEL",
    "model_era_of", "era_model", "model_key",
    "CALIBRATION_BINS", "CALIBRATION_MIN_N", "calibration_from_pairs", "calibration_summary",
    "neutrality_summary", "_calibration_lines", "_neutrality_lines",
    "LOOKUP_MIN_N", "LOOKUP_FORCED_CONF", "build_confidence_lookup", "merge_confidence_lookup",
    "is_forced_or_low_confidence", "confidence_display",
    "WINDOWS", "SHAPE_WINDOW", "SHAPE_CLASSES",
    "realized_shape", "PathReview", "summarize_path_reviews",
]
