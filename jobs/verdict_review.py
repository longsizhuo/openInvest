"""Verdict 后验复盘 — 算 1d/7d/30d 命中率 + 区分宏观突变 vs 模型差。

输入：memory/.committee/<date>/<symbol>.md（含 macro_context_at_decision）
     memory/.backtest/<date>/<symbol>.md（backtest 产生的，同 schema）
输出：docs/verdict_accuracy.md (gitignored, 含真数字给本地分析用)
     memory/.dreams/verdict_review.jsonl（结构化结果，给 dreaming 用）

命中率定义：
- BUY / ACCUMULATE → 后续涨 >0% = hit
- SELL / TRIM → 后续跌 <0% = hit
- HOLD → 波动 |return| < 3% = hit (说明"无操作"是对的)

上下文归因（A1 增强）：
- 事后 30 天内 VIX 变化 > 30% → 标记 "macro_shock"，verdict 错也免责
- TNX 变化 > 50bp → 同上
- 这样 README 上能展示："60% 命中率，剔除 macro_shock 后 75%"
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

from core.memory_store import MemoryStore  # noqa: E402

# 命中率窗口：天级 / 周级 / 月级，给短期+中期反馈
HIT_WINDOWS = [1, 7, 30]

# 记忆穿越 cutoff：决议日 ≤ 此日 = 落在 LLM 训练知识窗口内，"预测"实为记忆回放（非业绩）。
# 单一可信源（机器强制，不靠记忆）——backtest_committee 落盘 `**Contaminated**` 标记 + 本文件
# 分桶都 import 这个常量，绝不让两处各自硬编码 "2024-12-31" 漂移（见 CLAUDE.md 机器强制原则）。
CONTAMINATION_CUTOFF = "2024-12-31"

# 宏观突变阈值（剔除黑天鹅时用）
MACRO_SHOCK_THRESHOLDS = {
    "vix_pct_change": 0.30,        # VIX 变化 > 30% 算突变
    "tnx_bp_change": 50,            # TNX 变化 > 50 bp
    "usdcny_pct_change": 0.03,      # 人民币 ±3%
}

# verdict → 期望方向
EXPECTED_DIRECTION = {
    "BUY": "up",
    "ACCUMULATE": "up",
    "SELL": "down",
    "TRIM": "down",
    "HOLD": "flat",  # 期望波动小
}


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


# ---------- 解析 ----------

def _parse_committee_file(path: Path) -> Optional[Dict[str, Any]]:
    """从 committee md 文件抽 verdict + macro snapshot"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    verdict_m = re.search(r"\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s+([\d.]+)\)", text)
    if not verdict_m:
        return None
    macro_m = re.search(r"## Macro Context Snapshot.*?```json\n(.*?)\n```", text, re.DOTALL)
    macro = {}
    if macro_m:
        try:
            macro = json.loads(macro_m.group(1))
        except json.JSONDecodeError:
            pass
    # 新文件带 `**Symbol**: GC=F`（真实 yfinance symbol）。旧文件没有 → None，
    # 由 review_all 用 holdings 转义名映射兜底。
    sym_m = re.search(r"\*\*Symbol\*\*:\s*(\S+)", text)
    return {
        "verdict": verdict_m.group(1).upper(),
        "confidence": float(verdict_m.group(2)),
        "macro_at_decision": macro,
        "symbol": sym_m.group(1) if sym_m else None,
    }


# ---------- 事后涨跌 ----------

# 计价口径 = CNY/克 的代理资产（symbol 是 USD/oz 的 GC=F，但用户持的是积存金）
_GOLD_PROXY_KINDS = {"gold_cny_per_gram"}


def _closes(symbol: str):
    """拉 symbol 近 1 年日线（DataFrame，index=日期）。失败/空返回 None。"""
    from utils.exchange_fee import get_history_data
    try:
        df = get_history_data(symbol, "1y")
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        log.warning("_closes(%s) 失败: %s", symbol, e)
        return None


def _close_on_or_after(df, day) -> Optional[float]:
    """取 index 日期 >= day 的第一根 Close。

    决议日锚点：day=决议日 → 拿"决议日或下一交易日"收盘。
    窗口到期点：day=决议日+window → 拿"到期日或之后第一交易日"收盘。
    **关键**：若 day 落在未来（行情还没到那天），>= 过滤后为空 → 返回 None，
    自动过滤未成熟窗口。旧 bug 用 `<= target 的最后一根` 在 target 未来时
    会塌缩成"今天的收盘"，把只过了 3 天的样本标成 30d 收益。
    """
    sub = df[df.index.date >= day]
    if sub.empty:
        return None
    return float(sub["Close"].iloc[0])


def _window_return(
    symbol: str, holding: Optional[Dict[str, Any]], decision_date: str, window_days: int,
) -> Optional[float]:
    """决议日 → D+window 累计涨跌，**按 holding 的计价口径**算。

    - gold_cny_per_gram（积存金，symbol=GC=F 是 USD/oz 代理）：用户真实收益 =
      CNY/克收益 = (GC=F × USDCNY) 的比值。直接拿 GC=F USD/oz 会漏掉人民币
      汇率漂移，系统性偏置黄金命中率信号（与 Event Watch 邮件同一类单位错配）。
    - 其余（NDQ.AX 原生 AUD 等）：原生币种比值即用户体验收益，无需换算。

    成熟度：D+window 落在未来 → 任一端 _close_on_or_after 返回 None → 整体 None。
    """
    try:
        d = datetime.strptime(decision_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    target = d + timedelta(days=window_days)
    proxy_kind = (holding or {}).get("proxy_kind", "direct")

    if proxy_kind in _GOLD_PROXY_KINDS:
        gc = _closes(symbol)         # GC=F USD/oz
        fx = _closes("USDCNY=X")     # 人民币汇率
        if gc is None or fx is None:
            return None
        gc_s, gc_e = _close_on_or_after(gc, d), _close_on_or_after(gc, target)
        fx_s, fx_e = _close_on_or_after(fx, d), _close_on_or_after(fx, target)
        if None in (gc_s, gc_e, fx_s, fx_e) or gc_s * fx_s <= 0:
            return None
        return (gc_e * fx_e) / (gc_s * fx_s) - 1.0

    df = _closes(symbol)
    if df is None:
        return None
    start, end = _close_on_or_after(df, d), _close_on_or_after(df, target)
    if start is None or end is None or start <= 0:
        return None
    return (end / start) - 1.0


# ---------- 宏观突变检测 ----------

def _detect_macro_shock(
    macro_at_decision: Dict[str, float],
    decision_date: str,
    window_days: int,
) -> Dict[str, Any]:
    """对比决议时和 D+window 后的 macro 快照，标记是否发生突变。

    ⚠️ 2026-05-27：本检测结果**已不再用于 Dreaming 免责**（rem_sleep 只按
    regime==crash 免责）。原因：VIX/TNX/USDCNY 的 abs 阈值在低波动牛市里误报
    ~20%，且 `abs()` 双向连"VIX 下行/市场转好"也误杀。函数与 macro_shock 字段
    保留，仅作历史/参考（verdict_review 报告里仍统计展示），不参与样本剔除。
    """
    from utils.exchange_fee import get_history_data
    shock: Dict[str, Any] = {"detected": False, "drivers": []}

    def _get_close_on(symbol: str, date_str: str) -> Optional[float]:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            df = get_history_data(symbol, "1y")
            if df.empty:
                return None
            df_at = df[df.index.date <= d]
            if df_at.empty:
                return None
            return float(df_at["Close"].iloc[-1])
        except Exception:
            return None

    target_date = (datetime.strptime(decision_date, "%Y-%m-%d").date()
                   + timedelta(days=window_days)).strftime("%Y-%m-%d")

    # VIX
    vix_at = macro_at_decision.get("vix")
    vix_after = _get_close_on("^VIX", target_date)
    if vix_at and vix_after:
        change = abs(vix_after / vix_at - 1)
        if change > MACRO_SHOCK_THRESHOLDS["vix_pct_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"VIX {vix_at:.1f} → {vix_after:.1f} ({change*100:+.0f}%)")

    # TNX (单位是 % 数字，bp = 0.01)
    tnx_at = macro_at_decision.get("tnx")
    tnx_after = _get_close_on("^TNX", target_date)
    if tnx_at and tnx_after:
        bp_change = abs(tnx_after - tnx_at) * 100
        if bp_change > MACRO_SHOCK_THRESHOLDS["tnx_bp_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"TNX {tnx_at:.2f}% → {tnx_after:.2f}% ({bp_change:+.0f}bp)")

    # USDCNY
    usdcny_at = macro_at_decision.get("usdcny")
    usdcny_after = _get_close_on("USDCNY=X", target_date)
    if usdcny_at and usdcny_after:
        change = abs(usdcny_after / usdcny_at - 1)
        if change > MACRO_SHOCK_THRESHOLDS["usdcny_pct_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"USDCNY {usdcny_at:.3f} → {usdcny_after:.3f} ({change*100:+.1f}%)")

    return shock


# ---------- hit 判定 ----------

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
DEFAULT_DAILY_VOL_PCT = 2.0  # atr_pct 拉取失败时的兜底日波动


def _atr_pct(symbol: str) -> float:
    """资产的 14 日 ATR 占价格百分比（日波动幅度的度量）。拉不到 → 兜底。"""
    df = _closes(symbol)
    if df is None:
        return DEFAULT_DAILY_VOL_PCT
    try:
        from utils.market_metrics import compute_metrics
        atr = compute_metrics(df).get("atr_pct")
        return float(atr) if atr and atr > 0 else DEFAULT_DAILY_VOL_PCT
    except Exception:
        return DEFAULT_DAILY_VOL_PCT


# regime 计算复用一个 MarketStore 连接（避免每条 review 新开 sqlite 连接）
_REGIME_STORE = None


def _decision_regime(symbol: str, decision_date: str) -> Optional[str]:
    """决议日（截断到当天，防穿越）的市场 regime —— committee 同款 core.regime。

    给样本打 regime_at_decision 标记：crash 期间市场脱离基本面乱跳，事后涨跌不该
    归因到委员会判断质量，下游（Dreaming rem_sleep / v4 训练集）据此免责剔除。
    拿不到 → None（不打标）。

    **窗口口径（2026-05-27 修）**：直接读 DB 全历史 → 按 decision_date 截断 → 再
    tail(730)，**不调 live get_history_data**（它 tail(730) 在 cutoff 之前，对历史
    决议日窗口被锚死在最近 730 行、cutoff 后只剩 ~半年 → MA120/MA250 算不出 →
    一半样本误标 unknown，污染 jsonl 并可能让真 crash 日被错标 unknown 而漏掉免责）。
    与 backtest patch 的窗口逻辑一致，保证 committee 看到的 regime == 这里复盘的 regime。
    """
    global _REGIME_STORE
    try:
        import pandas as pd
        from utils.market_metrics import compute_metrics
        from core.regime import classify_regime
        if _REGIME_STORE is None:
            from db.market_store import MarketStore
            _REGIME_STORE = MarketStore()
        df = _REGIME_STORE.get_history_df(symbol, days=100000)  # 全历史
        if df is None or df.empty:
            return None
        df = df[df.index <= pd.to_datetime(decision_date)].tail(730)
        if len(df) < 30:
            return None
        regime = classify_regime(compute_metrics(df), symbol=symbol).get("regime")
        return regime if regime and regime != "unknown" else None
    except Exception as e:  # noqa: BLE001
        log.warning("_decision_regime(%s,%s) → None: %s", symbol, decision_date, e)
        return None


_ATR_CACHE: Dict[str, float] = {}


def _atr_pct_cached(symbol: str) -> float:
    """_atr_pct 的进程内缓存（atr 用当前 1y 数据算，同 symbol 全程不变，避免每条 review 重拉行情）。"""
    if symbol not in _ATR_CACHE:
        _ATR_CACHE[symbol] = _atr_pct(symbol)
    return _ATR_CACHE[symbol]


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


# ---------- symbol 反转义 ----------

def _sanitize(symbol: str) -> str:
    """与 core/committee.py 写文件时一致的转义（= / . → _）。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)


def _build_symbol_resolver() -> Dict[str, Dict[str, Any]]:
    """建 {转义文件名 → holding dict} 映射，给旧文件（无 **Symbol** 行）兜底。

    committee 文件名做了有损转义（GC=F→GC_F、NDQ.AX→NDQ_AX），无法反推。
    用 PortfolioManager 全量持仓（含 tracking-only）建映射拿回真实 symbol +
    proxy_kind（决定事后收益按 USD/oz 还是 CNY/克算）。
    """
    try:
        from core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
        return {_sanitize(h["symbol"]): h for h in pm.holdings.all() if h.get("symbol")}
    except Exception as e:  # noqa: BLE001
        log.warning("_build_symbol_resolver 退化空: %s: %s", type(e).__name__, e)
        return {}


# ---------- 主流程 ----------

def review_one(
    committee_dir: Path,
    stem: str,
    resolver: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[VerdictReview]:
    """对单个 verdict 文件做事后 review。

    stem: committee 文件名去掉 .md（可能是转义名 GC_F，也可能是 NDQ.AX）。
    resolver: 转义名 → holding 映射，用于旧文件拿回真实 symbol + proxy_kind。
    """
    decision_date = committee_dir.name  # YYYY-MM-DD
    path = committee_dir / f"{stem}.md"
    parsed = _parse_committee_file(path)
    if not parsed:
        return None

    resolver = resolver if resolver is not None else _build_symbol_resolver()
    holding = resolver.get(stem)
    # 真实 symbol 优先级：文件内 **Symbol** 行 > holdings 映射 > 转义还原启发式
    real_symbol = parsed.get("symbol") or (holding.get("symbol") if holding else None)
    if not real_symbol and "_" in stem:
        # 已不持有的历史资产（如卖掉的 ASIA.AX）：试把 _ 还原成 .（最常见的
        # 交易所后缀转义），用 yfinance 验证确有数据才采用，否则继续放弃——
        # 宁可跳过也不瞎猜出脏 symbol 污染学习信号。
        candidate = stem.replace("_", ".")
        if _closes(candidate) is not None:
            real_symbol = candidate
    if not real_symbol:
        log.warning("review_one 跳过：无法解析真实 symbol（stem=%s, dir=%s）", stem, decision_date)
        return None
    # holding 没在映射里但文件给了真实 symbol → 再按真实 symbol 查一次（拿 proxy_kind）
    if holding is None and resolver:
        holding = next((h for h in resolver.values() if h.get("symbol") == real_symbol), None)

    rv = VerdictReview(
        date=decision_date,
        asset=real_symbol,
        verdict=parsed["verdict"],
        confidence=parsed["confidence"],
        expected_direction=EXPECTED_DIRECTION.get(parsed["verdict"], "flat"),
        macro_at_decision=parsed["macro_at_decision"],
        source="backtest" if "backtest" in str(committee_dir) else "live",
        # 决议日落在 LLM 训练窗口 → 记忆穿越，下游分桶/Dreaming 据此剔出业绩统计。
        # ISO 日期字典序比较等价于时间序，无需 parse。
        contaminated=decision_date <= CONTAMINATION_CUTOFF,
    )

    # 波动率阈值按资产定（HOLD 的"没动"判定 + 方向分类共用同一个 flat band）。
    # 改为对所有 verdict 都算 atr（带缓存）：directions 是 verdict 无关的"市场到底涨没涨"，
    # 必须和 HOLD 用同一条 flat band 才能让下游 regime 基率与 missed_up/avoided_down 口径一致。
    atr = _atr_pct_cached(real_symbol)
    for window in HIT_WINDOWS:
        ret = _window_return(real_symbol, holding, decision_date, window)
        if ret is not None:
            rv.actual_returns[f"{window}d"] = round(ret, 4)
            flat_th = _flat_band(atr, window) if atr is not None else 0.03
            rv.hits[f"{window}d"] = _is_hit(parsed["verdict"], ret, flat_th)
            # 原始市场方向（verdict 无关）：给 Dreaming 算 regime 基率
            rv.directions[f"{window}d"] = (
                "up" if ret > flat_th else ("down" if ret < -flat_th else "flat")
            )

    # 宏观突变检测只在 30d 窗口已成熟时算（否则会拿今天的 macro 冒充 D+30）
    if "30d" in rv.actual_returns:
        rv.macro_shock = _detect_macro_shock(
            parsed["macro_at_decision"], decision_date, window_days=30
        )

    # 决议日 regime 标记（crash 样本留痕但下游免责）。截断到决议日，无穿越。
    rv.regime_at_decision = _decision_regime(real_symbol, decision_date)

    return rv


def review_all(*, include_backtest: bool = True, include_live: bool = True) -> List[VerdictReview]:
    """扫所有历史 verdict 做 review"""
    store = MemoryStore()
    reviews: List[VerdictReview] = []

    sources: List[Path] = []
    if include_live and (store.root / ".committee").exists():
        sources.extend(sorted((store.root / ".committee").iterdir()))
    if include_backtest and (store.root / ".backtest").exists():
        sources.extend(sorted((store.root / ".backtest").iterdir()))

    # glob 出每个日期目录里实际存在的 *.md。stem 可能是转义名（GC_F），靠 review_one
    # 内部用文件里的 **Symbol** 行 + holdings 映射拿回真实 symbol（GC=F）再拉行情。
    # 旧 bug：直接拿 stem（GC_F/NDQ_AX）喂 yfinance → 全 404 → actual_returns 全空。
    resolver = _build_symbol_resolver()
    seen: set = set()  # (date, real_symbol) 去重，防同一 verdict 因转义产生双份
    for date_dir in sources:
        if not date_dir.is_dir():
            continue
        for md_file in date_dir.glob("*.md"):
            rv = review_one(date_dir, md_file.stem, resolver)
            if not rv:
                continue
            dedup_key = (rv.date, rv.asset, rv.source)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            reviews.append(rv)
    return reviews


# ---------- 报告生成 ----------

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
            # 剔除 macro shock 后再算
            non_shock = [r.hits[key] for r in reviews
                         if key in r.hits and not r.macro_shock.get("detected")]
            if non_shock:
                entry["hit_rate_excl_macro_shock"] = round(sum(non_shock) / len(non_shock), 3)
                entry["n_excl_shock"] = len(non_shock)
        bucket["by_window"][key] = entry

    for verdict in ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"]:
        subset = [r for r in reviews if r.verdict == verdict]
        if not subset:
            continue
        entry = {"n": len(subset),
                 "avg_confidence": round(sum(r.confidence for r in subset) / len(subset), 3)}
        if not suppress_rates:
            for window in HIT_WINDOWS:
                key = f"{window}d"
                hits = [r.hits[key] for r in subset if key in r.hits]
                if hits:
                    entry[f"hit_rate_{key}"] = round(sum(hits) / len(hits), 3)
        bucket["by_verdict"][verdict] = entry

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


def summarize(reviews: List[VerdictReview]) -> Dict[str, Any]:
    """按 contaminated（决议日 ≤ CONTAMINATION_CUTOFF）**强制分桶**聚合。

    机器强制（不靠记忆）：holdout（cutoff 之后，干净业绩）与 contaminated（落在 LLM 训练
    窗口，记忆穿越非业绩）**绝不合并成一个命中率**——本函数不产出任何跨桶 union 数字，
    两桶各自独立 `_summarize_bucket`。partition assert 守"每条 review 非此即彼，无遗漏无重叠"。
    holdout 桶 n<30 按红线 #2 不出命中率切片；contaminated 桶数字带 note 标注"含记忆穿越,非业绩"。
    """
    holdout = [r for r in reviews if not r.contaminated]
    contaminated = [r for r in reviews if r.contaminated]
    assert len(holdout) + len(contaminated) == len(reviews), \
        "contaminated 分桶必须无遗漏无重叠（每条 review 非 holdout 即 contaminated）"

    return {
        "total": len(reviews),
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
              "| Verdict | N | 平均 confidence | 1d hit | 7d hit | 30d hit |",
              "|---|---|---|---|---|---|"]
    for v_key, v in bucket["by_verdict"].items():
        lines.append(
            f"| {v_key} | {v['n']} | {v['avg_confidence']:.2f} | "
            f"{v.get('hit_rate_1d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_7d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_30d', 0)*100:.0f}% |"
        )
    return lines


def write_report(reviews: List[VerdictReview], summary: Dict[str, Any]) -> Path:
    """输出 markdown 报告到 docs/verdict_accuracy.md (gitignore 自动保护)。

    holdout 与 contaminated 两桶**分节展示，绝不合并成一个命中率**（机器强制，见 summarize）。
    诚实解读只基于 holdout（真业绩）。
    """
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "verdict_accuracy.md"

    holdout = summary["holdout"]
    contaminated = summary["contaminated"]
    lines = [
        "# Verdict Accuracy Report",
        f"\n*Generated: {datetime.now().isoformat(timespec='seconds')}*",
        f"\n**总 verdict 数**: {summary['total']}  "
        f"(holdout {holdout['n']} + contaminated {contaminated['n']}, cutoff {summary['cutoff']})",
        "\n> 🔒 机器强制分桶：holdout（cutoff 之后，干净业绩）与 contaminated（决议日落在 LLM "
        "训练窗口，记忆穿越非业绩）**分别统计，绝不合并成一个命中率**。",
    ]
    lines += _bucket_lines("Holdout（干净业绩 · cutoff 之后）", holdout)
    lines += _bucket_lines("Contaminated（记忆穿越 · cutoff 及之前）", contaminated,
                           note=contaminated.get("note"))

    # 诚实解读：仅基于 holdout（真业绩）。holdout 被 sub30 抑制或无方向性样本时退化提示。
    lines.append("\n## 诚实解读（仅基于 holdout 干净样本）\n")
    by_v = holdout["by_verdict"]
    if holdout.get("rates_suppressed_sub30") or not by_v:
        lines.append(f"holdout 样本不足（n={holdout['n']}），暂不做方向性命中率解读，"
                     "建议跑 90+ 天后再正式评估。\n")
    else:
        directional_n = sum(by_v.get(v, {}).get("n", 0) for v in
                            ["BUY", "ACCUMULATE", "SELL", "TRIM"])
        if directional_n == 0:
            lines.append("⚠️ holdout 内**没有任何方向性 verdict**（BUY/ACCUMULATE/SELL/TRIM 全 0）。"
                         "系统过度保守，不构成可操作 alpha。\n")
        else:
            directional_hits_30d = sum(
                by_v.get(v, {}).get("hit_rate_30d", 0) * by_v.get(v, {}).get("n", 0)
                for v in ["BUY", "ACCUMULATE", "SELL", "TRIM"]
            )
            directional_rate = directional_hits_30d / directional_n
            lines.append(f"### holdout 方向性 verdict 真实命中率：{directional_rate*100:.1f}% "
                         f"(n={directional_n})\n")
            lines.append("**说明**：剔除 HOLD（命中率被'波动 <flat band 算 hit'灌水）后的真实 alpha 信号。\n")
            if directional_rate < 0.5:
                lines.append("🔴 **低于随机**：方向性判断比抛硬币还差。\n")
            elif directional_rate < 0.6:
                lines.append("🟡 **接近随机**：微弱信号，样本量不足以确认。\n")
            else:
                lines.append("🟢 **高于随机**：方向性判断有真实 alpha，继续积累样本验证。\n")

    lines.append("\n## 已知 backtest 局限\n")
    lines.append("- portfolio_summary 在 backtest 模式是 mock 的'中性持仓'，LLM 看不到当时真实持仓状态")
    lines.append("- prior_insights 在 backtest 时为空（防穿越），失去 Dreaming 长期模式增强")
    lines.append("- 新闻/宏观叙事不在 tool 里（防 DDGS 时间泄露），Macro Strategist 仅靠数值指标")
    lines.append("- contaminated 桶决议日落在 LLM 训练窗口内，是记忆回放不是预测，**不可作为业绩证据**")
    lines.append("- 这些限制让 holdout 结果是 LLM 能力的**下限**估计，实盘可能更好（也可能更差）")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_jsonl(reviews: List[VerdictReview]) -> Path:
    """落 jsonl 给 Dreaming 后续 mining 用"""
    store = MemoryStore()
    out = store.root / ".dreams" / "verdict_review.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in reviews:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    return out


def run() -> Dict[str, Any]:
    """job entry"""
    reviews = review_all()
    if not reviews:
        return {"status": "skipped", "reason": "no committee verdicts to review"}
    summary = summarize(reviews)
    md_path = write_report(reviews, summary)
    jsonl_path = write_jsonl(reviews)
    return {
        "status": "ok",
        "summary": summary,
        "report": str(md_path),
        "jsonl": str(jsonl_path),
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
