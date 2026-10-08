"""Verdict 后验复盘 — 算 1d/7d/30d 命中率 + 区分宏观突变 vs 模型差。

输入：memory/.committee/<date>/<symbol>.md（含 macro_context_at_decision）
     memory/.backtest/<date>/<symbol>.md（backtest 产生的，同 schema；仅 --include-backtest 手动全量时读；
         其中前瞻纸面舰队那部分每天另读一遍，只进 confidence_lookup，见 review_fleet）
     db/market_data.db（只读，不触网——2026-10 D4）
输出：docs/verdict_accuracy.md (gitignored, 含真数字给本地分析用)
     memory/.dreams/verdict_review.jsonl（结构化结果，给 dreaming 用）
     memory/.dreams/confidence_lookup.json（裁决旁展示的同类决议查表，D10 P1）

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
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openinvest.calc.symbols import is_closed_weekend
from openinvest.utils.symbols import safe_symbol
# 统计纯核已迁 jobs/review_calc（ADR-026）——导回保持历史导出面
from openinvest.jobs.review_calc import (  # noqa: F401
    CONTAMINATION_CUTOFF,
    EXPECTED_DIRECTION,
    FLAT_CEILING_PCT,
    HIT_WINDOWS,
    K_FLAT,
    VerdictReview,
    _bucket_lines,
    _flat_band,
    build_confidence_lookup,
    merge_confidence_lookup,
    _is_hit,
    _summarize_bucket,
)
from openinvest.jobs.review_calc import summarize_verdict_reviews as summarize  # noqa: F401
from openinvest.paths import INVEST_ROOT
ROOT = INVEST_ROOT
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

from openinvest.core.memory_store import MemoryStore  # noqa: E402
from openinvest.core.regime_probability import forward_return  # noqa: E402



# 宏观突变阈值（剔除黑天鹅时用）
MACRO_SHOCK_THRESHOLDS = {
    "vix_pct_change": 0.30,        # VIX 变化 > 30% 算突变
    "tnx_bp_change": 50,            # TNX 变化 > 50 bp
    "usdcny_pct_change": 0.03,      # 人民币 ±3%
}





# ---------- 解析 ----------

# 单一可信源上移 core/decision_ledger.py（decision accounting 同用，issue #133）。
# 返回 dict 是原格式超集（多 alloc_cny 键），本文件用法不变。
from openinvest.core.decision_ledger import parse_committee_file as _parse_committee_file  # noqa: E402


# ---------- 事后涨跌 ----------

# 计价口径 = CNY/克 的代理资产（symbol 是 USD/oz 的 GC=F，但用户持的是积存金）
_GOLD_PROXY_KINDS = {"gold_cny_per_gram"}


# market_data.db 读连接 + 全历史行情的进程内缓存（review_all 开头清空）
_STORE = None
_CLOSES_CACHE: Dict[str, Any] = {}


def _closes(symbol: str):
    """symbol 全历史日线（DataFrame，index=日期）。**只读 market_data.db**，进程内缓存。空 → None。

    2026-10 D4：不再走 get_history_data——它见库尾不是"今天"就去打 yfinance，一次复盘
    ~2,400 次调用（cron 跑在 UTC 周末时几乎全打网，且与 price_sentinel 同进程）。
    行情新鲜度归 daily_report / price_sentinel 的刷新，本 job 只消费库。
    缓存键=大写 symbol（同 get_history_data 口径）；scheduler 是常驻进程，
    review_all 每次开跑先清空，跨天不复用旧快照。
    """
    global _STORE
    sym = symbol.upper()
    if sym not in _CLOSES_CACHE:
        try:
            if _STORE is None:
                from openinvest.db.market_store import MarketStore
                _STORE = MarketStore()
            df = _STORE.get_history_df(sym, days=100000)  # 全历史
            _CLOSES_CACHE[sym] = None if df is None or df.empty else df
        except Exception as e:  # noqa: BLE001
            log.warning("_closes(%s) 失败: %s", symbol, e)
            _CLOSES_CACHE[sym] = None
    return _CLOSES_CACHE[sym]


def _window_return(
    symbol: str, holding: Optional[Dict[str, Any]], decision_date: str, window_days: int,
) -> Optional[float]:
    """决议日 → D+window 累计涨跌，**按 holding 的计价口径**算。

    - gold_cny_per_gram（积存金，symbol=GC=F 是 USD/oz 代理）：用户真实收益 =
      CNY/克收益 = (GC=F × USDCNY) 的比值。直接拿 GC=F USD/oz 会漏掉人民币
      汇率漂移，系统性偏置黄金命中率信号（与 Event Watch 邮件同一类单位错配）。
    - 其余（NDQ.AX 原生 AUD 等）：原生币种比值即用户体验收益，无需换算。

    口径（2026-10-07 #234-2）：委托 regime_probability.forward_return 单一可信源
    （path_review / intervention_review 同用）——base = 决议日**当日或之前**最后一根
    收盘，target = ≥ D+window 日历天首根收盘。旧实现 base 取"当日或之后首根"：
    周末/假日决议把周一跳空排除在命中归因之外（1d 窗还会塌成 base==target 恒 0），
    与它被验证 against 的概率表路径分布测的不是同一个量。
    成熟度：target 落在行情尾部之外 → None；过去侧：决议日早于数据首行 → None
    （issue #179 P1-A⑤ 护栏由 forward_return 的 i<0 分支承接）。
    """
    try:
        datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    proxy_kind = (holding or {}).get("proxy_kind", "direct")

    if proxy_kind in _GOLD_PROXY_KINDS:
        gc = _closes(symbol)         # GC=F USD/oz
        fx = _closes("USDCNY=X")     # 人民币汇率
        if gc is None or fx is None:
            return None
        # 两腿各自在本序列上锚定，(1+r_gc)(1+r_fx)-1 ≡ (gc_e·fx_e)/(gc_s·fx_s)-1
        r_gc = forward_return(symbol, decision_date, window_days, closes=gc["Close"])
        r_fx = forward_return("USDCNY=X", decision_date, window_days, closes=fx["Close"])
        if r_gc is None or r_fx is None:
            return None
        return (1.0 + r_gc) * (1.0 + r_fx) - 1.0

    df = _closes(symbol)
    if df is None:
        return None
    return forward_return(symbol, decision_date, window_days, closes=df["Close"])


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
    import pandas as pd
    shock: Dict[str, Any] = {"detected": False, "drivers": []}

    def _get_close_on(symbol: str, date_str: str) -> Optional[float]:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            df = _closes(symbol)  # 只读库 + 缓存（原每条决议 3 次全历史读 + 可能打网）
            if df is None:
                return None
            # date <= d 的根数（searchsorted 代替物化 index.date）
            i = df.index.searchsorted(pd.Timestamp(d, tz=df.index.tz) + pd.Timedelta(days=1))
            if i == 0:
                return None
            return float(df["Close"].iloc[i - 1])
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

DEFAULT_DAILY_VOL_PCT = 2.0  # atr_pct 拉取失败时的兜底日波动


def _atr_pct_asof(symbol: str, decision_date: str) -> float:
    """决议日 as-of 截断的 14 日 ATR%（issue #179 P1-A⑥）。拉不到 → 兜底。

    旧实现拿"当前"1y 数据算 ATR 给全部历史决议定 flat band：(a) 前视——历史决议
    用今天的波动率打分；(b) 标签漂移——run() 每次从 .md 全量重建 jsonl，同一决议
    的 hit/directions 会随当日 ATR 变化翻转，直接污染 #141 Brier 校准的标签稳定性。
    与 _decision_regime 同口径：DB 全历史 → 截断决议日 → tail(400) 算 ATR。
    """
    try:
        import pandas as pd
        from openinvest.utils.market_metrics import compute_metrics
        df = _closes(symbol)
        if df is None:
            return DEFAULT_DAILY_VOL_PCT
        df = df[df.index <= pd.to_datetime(decision_date)].tail(400)
        if len(df) < 20:
            return DEFAULT_DAILY_VOL_PCT
        atr = compute_metrics(df).get("atr_pct")
        return float(atr) if atr and atr > 0 else DEFAULT_DAILY_VOL_PCT
    except Exception as e:  # noqa: BLE001
        log.warning("_atr_pct_asof(%s,%s) 兜底: %s", symbol, decision_date, e)
        return DEFAULT_DAILY_VOL_PCT


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
    try:
        import pandas as pd
        from openinvest.utils.market_metrics import compute_metrics
        from openinvest.core.regime import classify_regime
        df = _closes(symbol)  # DB 全历史（缓存）
        if df is None:
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


def _atr_pct_cached(symbol: str, decision_date: str) -> float:
    """_atr_pct_asof 的进程内缓存（键=(symbol, 决议日)，避免每条 review 重拉行情）。"""
    key = f"{symbol}@{decision_date}"
    if key not in _ATR_CACHE:
        _ATR_CACHE[key] = _atr_pct_asof(symbol, decision_date)
    return _ATR_CACHE[key]






# ---------- symbol 反转义 ----------

def _sanitize(symbol: str) -> str:
    """与 core/committee.py 写文件时一致的转义（= / . → _）。"""
    return safe_symbol(symbol)


def _build_symbol_resolver() -> Dict[str, Dict[str, Any]]:
    """建 {转义文件名 → holding dict} 映射，给旧文件（无 **Symbol** 行）兜底。

    committee 文件名做了有损转义（GC=F→GC_F、NDQ.AX→NDQ_AX），无法反推。
    用 PortfolioManager 全量持仓（含 tracking-only）建映射拿回真实 symbol +
    proxy_kind（决定事后收益按 USD/oz 还是 CNY/克算）。
    """
    try:
        from openinvest.core.portfolio_manager import PortfolioManager
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
    *,
    regime: bool = True,
) -> Optional[VerdictReview]:
    """对单个 verdict 文件做事后 review。

    stem: committee 文件名去掉 .md（可能是转义名 GC_F，也可能是 NDQ.AX）。
    resolver: 转义名 → holding 映射，用于旧文件拿回真实 symbol + proxy_kind。
    regime: False 时不算 regime_at_decision（只要标签的调用方省掉一半耗时，见 review_fleet）。
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
        # 已不持有的历史资产（如已卖出的 XXX.AX）：试把 _ 还原成 .（最常见的
        # 交易所后缀转义），行情库里确有数据才采用，否则继续放弃——
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
        weekend_dup=is_closed_weekend(real_symbol, decision_date),
    )

    # 波动率阈值按资产定（HOLD 的"没动"判定 + 方向分类共用同一个 flat band）。
    # 改为对所有 verdict 都算 atr（带缓存）：directions 是 verdict 无关的"市场到底涨没涨"，
    # 必须和 HOLD 用同一条 flat band 才能让下游 regime 基率与 missed_up/avoided_down 口径一致。
    atr = _atr_pct_cached(real_symbol, decision_date)
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
    rv.regime_at_decision = _decision_regime(real_symbol, decision_date) if regime else None

    return rv


def review_all(*, include_backtest: bool = True, include_live: bool = True) -> List[VerdictReview]:
    """扫所有历史 verdict 做 review"""
    _CLOSES_CACHE.clear()  # 常驻 scheduler 进程：每次复盘重读库，不吃昨天的快照
    _ATR_CACHE.clear()
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
        f"(holdout {holdout['n']} + contaminated {contaminated['n']}"
        f" + 周末重复 {summary['weekend_dup_excluded']} 条不计, cutoff {summary['cutoff']})",
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


FLEET_START = "2026-07-24"  # 第一天 --prospective 纸面舰队；更早的 .backtest/<date> 是历史回填


def review_fleet() -> List[VerdictReview]:
    """前瞻纸面舰队 → 查表样本（只进 confidence_lookup，不进 jsonl / 命中率页 / 纪律台账）。

    舰队和历史回填共用 .backtest/，transcript 里没有来源标记，按两条认前瞻：决议日 ≥ FLEET_START，
    且文件在决议日当天或次日写出（--prospective 只跑今天；事后补跑的历史回测 mtime 远晚于决议日）。
    T2 试跑臂写 .backtest_t2conf/，不在这里。resolver={}：舰队是中性持仓，按标的原生计价打标签，
    不套本机持仓的积存金口径。标签和行情同 live：T2 30d 规则，只读 market_data.db、不触网。
    ponytail: 靠 mtime 认前瞻；备份还原没保留 mtime 时舰队样本整体排除（退回默认表），不会误收。
    ponytail: 每天把已成熟的舰队行全量重打标签（今天 ~1,600 行 ≈ 20 秒，每月 +~1,000 行）；
    慢到碍事时按 (date, symbol) 缓存已成熟的 30d 标签。
    """
    base = MemoryStore().root / ".backtest"
    matured_by = date.today() - timedelta(days=30)   # 更晚的决议 30d 窗口还没到，标签必为空
    out: Dict[Tuple[str, str], VerdictReview] = {}
    for d in sorted(base.iterdir()) if base.exists() else []:
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if not d.is_dir() or d.name < FLEET_START or day > matured_by:
            continue
        for md in sorted(d.glob("*.md")):
            written = datetime.fromtimestamp(md.stat().st_mtime).date()
            if not day <= written <= day + timedelta(days=1):
                continue
            rv = review_one(d, md.stem, {}, regime=False)
            if rv:
                out.setdefault((rv.date, rv.asset), rv)
    return list(out.values())


CONFIDENCE_LOOKUP = "confidence_lookup"  # .dreams/confidence_lookup.json（D10 P1 展示查表）
# 包内默认表：只用舰队样本生成（scripts/gen_confidence_lookup_default.py），给本机某 verdict n<30 时兜底
DEFAULT_LOOKUP_PATH = Path(__file__).with_name("confidence_lookup_default.json")


def write_confidence_lookup(reviews: List[VerdictReview]) -> Path:
    """每日复盘尾巴顺手刷新展示查表（纯算术）。样本 = live + 前瞻舰队。邮件/事件提醒/API 读它。"""
    data = {**build_confidence_lookup(reviews, review_fleet()),
            "generated_at": datetime.now().isoformat(timespec="seconds")}
    return MemoryStore().write_dream_state(CONFIDENCE_LOOKUP, data)


def _valid_lookup(read) -> Optional[Dict[str, Any]]:
    try:
        data = read()
    except Exception as e:  # noqa: BLE001
        log.warning("读查表失败，按缺表处理: %s", e)
        return None
    return data if isinstance(data, dict) and isinstance(data.get("by_verdict"), dict) else None


def load_confidence_lookup() -> Optional[Dict[str, Any]]:
    """读展示查表：本机（.dreams）某 verdict n≥30 用本机，否则用包内默认表（merge_confidence_lookup）。
    两边都没有/坏了 → None（展示按 n=0 显示"样本不足"，不阻断任何发送）。"""
    local = _valid_lookup(lambda: MemoryStore().read_dream_state(CONFIDENCE_LOOKUP))
    default = _valid_lookup(lambda: json.loads(DEFAULT_LOOKUP_PATH.read_text(encoding="utf-8")))
    return merge_confidence_lookup(local, default)


def run(*, include_backtest: bool = False) -> Dict[str, Any]:
    """job entry。cron 默认只复盘 live（D4 签字方案）。

    include_backtest=True 是研究用全量重建（.backtest 14 万+ 文件，数小时级），
    只手动跑：`python -m openinvest.jobs.verdict_review --include-backtest`。
    两种模式都整份覆盖 jsonl。
    """
    reviews = review_all(include_backtest=include_backtest)
    if not reviews:
        return {"status": "skipped", "reason": "no committee verdicts to review"}
    summary = summarize(reviews)
    md_path = write_report(reviews, summary)
    jsonl_path = write_jsonl(reviews)
    write_confidence_lookup(reviews)
    return {
        "status": "ok",
        "summary": summary,
        "report": str(md_path),
        "jsonl": str(jsonl_path),
    }


if __name__ == "__main__":
    result = run(include_backtest="--include-backtest" in sys.argv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
