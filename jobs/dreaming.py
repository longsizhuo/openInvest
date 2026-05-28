"""OpenClaw 风格 Dreaming 三阶段 - 把短期信号整合成长期 insights

每天凌晨 03:00 跑（jobs/dreaming.yml）。

**学习信号（2026-05-26 换源）**：从"委员会自己的 verdict vs 事后实际盘涨跌"
学习，**不再**学用户的实际成交。原因：用户交易量小（个位数～十几笔），从成交里
学不到东西；但委员会每天对每个资产都出 verdict，每条 verdict + 事后行情 = 一个
天然样本，与交易量彻底解耦。数据源是 jobs/verdict_review.py 产出的
verdict_review.jsonl（已 proxy-aware 黄金 CNY/克 + forward window 成熟度过滤）。

阶段：
  Light Sleep  — 读 verdict_review.jsonl + 决议日 regime → .dreams/short-term-recall.json
  REM Sleep    — 按 (asset, verdict, regime) 聚合事后命中率 → .dreams/candidates.json
  Deep Sleep   — 阈值门 (score≥0.8 / count≥3) → 可选 LLM 验伪 →
                  insights/*.md + MEMORY.md + DREAMS.md

**HOLD 机会成本感知**：HOLD 命中用波动率感知阈值（"没动"=小于该资产窗口正常波动，
非写死 3%）。没命中再分方向：市场涨=踏空(missed_up，反保守告诫)、市场跌=躲跌
(avoided_down，中性)。踏空频繁的 HOLD 模式固化成 caution insight，主动压低 CIO 的
HOLD 倾向。详见 docs/wiki/03-dreaming.md。

输入：
  - memory/.dreams/verdict_review.jsonl  (委员会 verdict + 事后行情，由 verdict_review 产)
  - utils.exchange_fee.get_history_data  (决议日 regime 分类用)
输出：
  - memory/.dreams/short-term-recall.json
  - memory/.dreams/candidates.json
  - memory/.dreams/events.jsonl       (审计)
  - memory/insights/<topic>.md         (Deep 通过)
  - memory/DREAMS.md                   (人类可读叙事)

默认零 LLM 成本（纯统计阈值门 + 模板化叙事）。
P1-3: 设 INVEST_DREAMING_LLM_VERIFY=1 后，Deep Sleep 在写 insights 前会过一次
廉价 LLM（DeepSeek-Chat）验伪——挑出"统计 ≥ 阈值但很可能是 spurious correlation"
的候选拒绝。env off 时行为完全不变。
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

from core.consolidation_lock import (
    rollback_consolidation_lock,
    try_acquire_consolidation_lock,
)
from core.memory_store import MemoryStore
from core.regime import classify_regime
from utils.exchange_fee import get_history_data
from utils.market_metrics import compute_metrics

# Light Sleep 摄入最近 N 天的 verdict。生产默认 90（只学近期行为）；跑历史 backtest
# 训练集（如 Phase 1.5 的 2023-2024 leak-free 窗口）时，数据比 wall-clock"现在"老得多，
# 需用 INVEST_DREAMING_LOOKBACK_DAYS 调大（如 99999）把整个历史窗口纳入，否则全被滤掉。
#
# Step 3b: 从 config 读取，set_config_override() 实时生效。
# 模块级变量保留向后兼容，但函数内每次调用读 config。
LOOKBACK_DAYS = int(os.getenv("INVEST_DREAMING_LOOKBACK_DAYS", "90"))
WINDOWS = [7, 30]         # 回看交易后 N 天的市场表现
MIN_RECALL = 3            # 一个 pattern 至少出现 3 次


def _get_dreaming_config():
    """读 dreaming tunable config（实时，支持 set_config_override）。"""
    from core.config import load_config
    return load_config().dreaming


def _get_macro_buckets():
    """读 macro bucket 分桶阈值（实时，支持 set_config_override）。"""
    from core.config import load_config
    return load_config().macro_buckets
MIN_SCORE = 0.8           # Deep Sleep 阈值（OpenClaw 同款）— 仅供文档，实际从 locked config 读取
# caution lift-based 评分参数（2026-05-27 ADR 008，原理正确修正，非 reward hacking）：
# - CAUTION_MIN_BASE_DOWN：该 regime 的 30d 真实下行基率必须 ≥ 此值，否则"踏空"只是
#   单向上涨基率假象（Phase1.5 牛市 / post-cutoff 急跌后 V 反弹都是 base_down≈0）。
# - CAUTION_LIFT_FULL：lift（HOLD missed_up − regime base_up）达到此值算满分 quality。
CAUTION_MIN_BASE_DOWN = 0.15
CAUTION_LIFT_FULL = 0.20

# 旧（v1 写死 2 个 symbol，fork 用户持 AAPL/510300/BTC-USD 时 outcomes 完全空）：
#   ASSET_PRICE_SYMBOL = {"GOLD-CNY": "GC=F", "NDQ.AX": "NDQ.AX"}
# 新：从 holding.symbol → yfinance ticker 动态查
#   - 优先看 holding 自己的 yfinance_proxy 字段（黄金 GOLD-CNY → GC=F 这种 proxy）
#   - 否则 holding.symbol 直接当 yfinance ticker（510300.SS / AAPL / BTC-USD 都直接能用）

def _resolve_price_symbol(symbol: str, store: MemoryStore) -> Optional[str]:
    """把 trade.symbol 解析成 yfinance ticker。

    Light Sleep 给每笔历史交易补市场表现时调。fork 用户持任意 symbol 都不再
    被静默跳过——只要 portfolio.md 里有这条 holding（就算 strategy.target_assets
    里没列），也能拿到 outcomes。
    """
    if not symbol:
        return None
    # 1) 在 portfolio holdings / strategy target_assets 里找 yfinance_proxy
    portfolio = store.read("portfolio") or {}
    strategy = store.read("strategy") or {}
    candidates: List[Dict[str, Any]] = []
    candidates.extend(portfolio.get("holdings") or [])
    candidates.extend(strategy.get("target_assets") or [])
    for h in candidates:
        if str(h.get("symbol", "")) == symbol:
            proxy = str(h.get("yfinance_proxy") or "").strip()
            if proxy:
                return proxy
            return symbol  # 没 proxy 就直接当 yfinance ticker
    # 2) 没找到也 fallback 直接当 ticker —— yfinance 返回空时 _market_outcome 会
    #    自然返回 None，不会报错
    return symbol

# 上下文指标符号（每笔交易都拉一遍）
CONTEXT_SYMBOLS = {
    "vix": "^VIX",
    "tnx": "^TNX",
    "usdcny": "USDCNY=X",
}


# ----------------------------------------------------------------------
# Light Sleep — 摄入交易 + 同期市场上下文
# ----------------------------------------------------------------------

def _safe_close(df: pd.DataFrame, on_or_before: str) -> Optional[float]:
    """返回 <= 指定日期的最后一行 Close；没有则 None"""
    if df.empty:
        return None
    cutoff = pd.to_datetime(on_or_before)
    sub = df[df.index <= cutoff]
    if sub.empty:
        return None
    return float(sub["Close"].iloc[-1])


def _market_outcome(symbol: str, trade_date: str, days_ahead: int) -> Optional[float]:
    """交易日 close 到 N 天后 close 的涨跌幅（百分比）"""
    df = get_history_data(symbol, "2y")
    base = _safe_close(df, trade_date)
    if base is None or base <= 0:
        return None
    end_date = (datetime.strptime(trade_date[:10], "%Y-%m-%d")
                + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    end = _safe_close(df, end_date)
    if end is None:
        return None
    return (end / base - 1) * 100


def _decision_regime(asset: str, decision_date: str) -> Optional[str]:
    """决议日的市场 regime（committee 同款 core.regime.classify_regime）。

    截断到 decision_date 当天为止的行情算 metrics，避免穿越未来。拿不到 →
    None（该样本不按 regime 细分，进 "any" 桶）。
    """
    try:
        df = get_history_data(asset, "2y")
        if df is None or df.empty:
            return None
        df_cut = df[df.index <= pd.to_datetime(decision_date)]
        if len(df_cut) < 30:  # 数据太薄算不出可信 regime
            return None
        metrics = compute_metrics(df_cut)
        regime = classify_regime(metrics, symbol=asset).get("regime")
        return regime if regime and regime != "unknown" else None
    except Exception as e:  # noqa: BLE001
        log.warning(f"_decision_regime({asset},{decision_date}) 退化 None: {e}")
        return None


def light_sleep(store: MemoryStore) -> List[Dict[str, Any]]:
    """读 verdict_review.jsonl（委员会建议 vs 事后实际盘），每条补决议日 regime。

    2026-05-26 换源：用户交易量小（19 笔），从成交学不到东西；改成学"委员会自己
    出的 verdict + 事后行情命中"——与交易量解耦，委员会每天对每个资产都出 verdict。
    数据源是 jobs/verdict_review.py 产出的 verdict_review.jsonl（已 proxy-aware
    黄金 CNY/克 + forward window 成熟度过滤）。
    """
    path = store.root / ".dreams" / "verdict_review.jsonl"
    cutoff = (datetime.now() - timedelta(days=_get_dreaming_config().lookback_days)).strftime("%Y-%m-%d")
    signals: List[Dict[str, Any]] = []

    if not path.exists():
        store.dream_event({"phase": "light_sleep", "signals_collected": 0,
                           "mode": "verdict_outcome", "note": "no_verdict_review_jsonl"})
        store.write_dream_state("short-term-recall", {"signals": [],
                                "generated_at": datetime.now().isoformat()})
        return signals

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(r.get("date", "")) < cutoff:
            continue
        if not r.get("hits"):  # 没有任何成熟窗口 → 无可学 outcome，跳过
            continue
        # regime 优先取 verdict_review.jsonl 已留痕的 regime_at_decision（review 时
        # 截断算好，一致且省重复拉行情）；老记录没有该字段时回退现算。
        regime = r.get("regime_at_decision") or _decision_regime(r["asset"], r["date"])
        signals.append({
            "decision_date": r["date"],
            "asset": r["asset"],
            "verdict": r["verdict"],
            "confidence": r.get("confidence"),
            "regime": [regime] if regime else [],
            # crash 样本免责标记：留痕进 signal，但 rem_sleep 不纳入模式学习
            "regime_crash": regime == "crash",
            "outcomes": r.get("actual_returns", {}),  # {"1d":..,"7d":..,"30d":..}（小数）
            "hits": r.get("hits", {}),
            "directions": r.get("directions", {}),  # {"7d":"up/down/flat","30d":..} 原始市场方向（算 regime 基率 + caution lift）
            "macro_shock": bool((r.get("macro_shock") or {}).get("detected")),
            "source": r.get("source", "live"),
        })

    store.write_dream_state("short-term-recall", {"signals": signals,
                                                    "generated_at": datetime.now().isoformat()})
    store.dream_event({"phase": "light_sleep", "signals_collected": len(signals),
                       "mode": "verdict_outcome"})
    return signals


# ----------------------------------------------------------------------
# REM Sleep — 跨笔聚合找模式
# ----------------------------------------------------------------------

def _classify_regime(ctx: Dict[str, float]) -> Tuple[str, ...]:
    """把上下文离散化为 regime tag（用于聚合）

    VIX/TNX 分桶阈值从 config 读取，set_config_override() 实时生效。
    """
    buckets = _get_macro_buckets()
    tags = []
    if "vix" in ctx:
        if ctx["vix"] < buckets.vix_low:
            tags.append("vix_low")
        elif ctx["vix"] < buckets.vix_high:
            tags.append("vix_mid")
        else:
            tags.append("vix_high")
    if "tnx" in ctx:
        if ctx["tnx"] < buckets.tnx_low:
            tags.append("tnx_low")
        elif ctx["tnx"] < buckets.tnx_high:
            tags.append("tnx_mid")
        else:
            tags.append("tnx_high")
    return tuple(sorted(tags))


def rem_sleep(store: MemoryStore, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (asset, verdict, regime) 聚合委员会决策的事后命中率。

    命中（hit）直接取 verdict_review 预算好的方向判定（HOLD 用波动率感知的"没动"阈值、
    BUY→涨、SELL→跌 等），不在这里重算。**极限情况免责只走一条**（留痕在 signal 里但
    不进模式学习）：决议日 regime==crash（双触发器，已验证 0 误报；crash 期市场脱离基本面
    乱跳，事后涨跌不该归因到委员会判断质量）。
    窗口只看 7d/30d（WINDOWS），不看 1d（1d 内几乎不动，HOLD 会被自动判命中，是 horizon 假象）。

    2026-05-27：macro_shock（VIX/TNX/USDCNY abs 阈值）免责**已退役**——它误报 20%、
    `abs()` 双向连"VIX 下行/市场转好"都误杀，把本该参与学习的样本剔掉反而保护过度保守。
    免责统一收敛到 crash regime 一条。

    下游约定：crash 样本的 regime_at_decision 已写进 verdict_review.jsonl，未来 v4
    训练集重建（Phase 2）须同样按 regime_at_decision=="crash" 排除（留痕不删）。

    HOLD 的机会成本感知（2026-05-26，与用户讨论后定）：把 HOLD 的事后结果拆成
    - flat（命中）: 市场确实没动 → HOLD 真的对了
    - missed_up（踏空）: 没命中 + 市场涨了 → 该出手没出手，**反保守的告诫信号**
    - avoided_down（躲跌）: 没命中 + 市场跌了 → 揣着子弹躲过下跌，中性不罚
    据此给 HOLD 候选标 kind：missed_up 占比高 → "caution"（压低 CIO 的 HOLD 倾向），
    否则 "reliable"（震荡市里真稳的 HOLD，可加权）。方向区分避免把"躲过下跌的 HOLD"
    误当踏空骂。
    """
    buckets: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        if not s.get("verdict"):
            continue
        # 免责只走 crash regime 一条（macro_shock 免责已退役，见 docstring）。
        if s.get("regime_crash"):  # crash 期市场脱离基本面，免责，不进模式学习
            continue
        key = (s["asset"], s["verdict"], tuple(sorted(s.get("regime", []))))
        buckets[key].append(s)

    # regime 基率（lift-based caution 评分用）：每 (asset, regime, window) 统计**所有 verdict**
    # 的原始市场方向 up/down/flat（verdict 无关）。base_up = 该 regime 30d 涨的比例，
    # base_down = 跌的比例。caution lift = HOLD missed_up − base_up；base_down 是"下行存在门"。
    regime_base: Dict[Tuple, Dict[str, int]] = defaultdict(lambda: {"up": 0, "down": 0, "n": 0})
    for s in signals:
        if not s.get("verdict") or s.get("regime_crash"):
            continue
        reg_t = tuple(sorted(s.get("regime", [])))
        for window in _get_dreaming_config().windows:
            d = (s.get("directions") or {}).get(f"{window}d")
            if d is None:
                continue
            b = regime_base[(s["asset"], reg_t, window)]
            b["n"] += 1
            if d == "up":
                b["up"] += 1
            elif d == "down":
                b["down"] += 1

    candidates: List[Dict[str, Any]] = []
    for (asset, verdict, regime), items in buckets.items():
        if len(items) < _get_dreaming_config().min_recall:
            continue
        is_hold = verdict.upper() == "HOLD"
        for window in _get_dreaming_config().windows:
            wk = f"{window}d"
            pairs = [
                (i["hits"].get(wk), i["outcomes"].get(wk))
                for i in items
                if wk in i.get("hits", {}) and i["hits"].get(wk) is not None
            ]
            if len(pairs) < _get_dreaming_config().min_recall:
                continue
            n = len(pairs)
            hit_rate = sum(1 for h, _ in pairs if h) / n
            rets = [r for _, r in pairs if r is not None]
            avg_return = (sum(rets) / len(rets) * 100) if rets else 0.0  # 小数→%

            cand = {
                "asset": asset,
                "verdict": verdict,
                "regime": list(regime),
                "window_days": window,
                "count": n,
                "hit_rate": round(hit_rate, 3),
                "avg_return_pct": round(avg_return, 2),
                "kind": "reliable",
            }
            # 附 regime 基率（caution lift 评分用）
            rb = regime_base.get((asset, regime, window))
            if rb and rb["n"] > 0:
                cand["base_up"] = round(rb["up"] / rb["n"], 3)
                cand["base_down"] = round(rb["down"] / rb["n"], 3)
                cand["base_n"] = rb["n"]
            if is_hold:
                # 没命中(非 flat) 里，涨的=踏空、跌的=躲跌
                missed_up = sum(1 for h, r in pairs if not h and r is not None and r > 0)
                avoided_down = sum(1 for h, r in pairs if not h and r is not None and r < 0)
                cand["missed_up_rate"] = round(missed_up / n, 3)
                cand["avoided_down_rate"] = round(avoided_down / n, 3)
                # 踏空比命中还频繁 → 这是个"该出手却 HOLD"的告诫模式
                if cand["missed_up_rate"] > hit_rate:
                    cand["kind"] = "caution"
            candidates.append(cand)

    store.write_dream_state("candidates", {"candidates": candidates,
                                              "generated_at": datetime.now().isoformat()})
    store.dream_event({"phase": "rem_sleep", "candidates": len(candidates)})
    return candidates


# ----------------------------------------------------------------------
# Deep Sleep — 阈值门 + （可选）LLM 验伪 + 写 insights
# ----------------------------------------------------------------------

LLM_VERIFY_SYSTEM_PROMPT = """\
你是金融模式审稿人。下面是一系列"用户历史交易行为 vs 市场结果"的统计候选。
每个候选都已经通过统计阈值门（命中率 + 样本量 + 平均收益）。

但**统计阈值不能区分**：
1. 真实可学习的行为模式 — 应该 KEEP 写入长期记忆
2. 虚假相关性（spurious correlation） — 应该 REJECT，不污染未来决策

虚假相关性的常见特征：
- 样本量勉强（count 接近 MIN_RECALL）+ 命中率刚好擦边（hit_rate 0.6-0.7）
- regime 标签太具体导致样本被切薄（例 "vix_low + tnx_mid + asset 三联组合"）
- avg_return_pct 绝对值很小（< 1%）—— 可能纯噪音
- 同一资产的对称模式都通过（"买金赚 / 卖金亏"+"卖金赚 / 买金亏" 同时通过 → 多半是回测过拟合）

真实模式的特征：
- 样本量充分（count ≥ 6）+ 命中率明显（≥ 0.75）
- regime 简单或宽泛（vix_low 单标签）
- 收益方向和动作意图一致（买入后正收益 / 卖出后负收益）

你的任务：对每个候选输出 KEEP 或 REJECT，给一句话理由（中文，≤30 字）。

输出格式（严格 JSON，无其他文字）：
{
  "verdicts": [
    {"id": 0, "decision": "KEEP", "reason": "样本充分命中明确"},
    {"id": 1, "decision": "REJECT", "reason": "regime 三联标签样本被切薄"}
  ]
}
"""


def _llm_verify_candidates(
    candidates: List[Dict[str, Any]],
) -> Tuple[List[bool], List[Dict[str, Any]]]:
    """让 LLM 给每个候选输出 KEEP/REJECT。返回 (kept_mask, raw_verdicts)

    失败时 fallback 全部 KEEP（保守策略：宁可放过 spurious 也不漏 real pattern；
    LLM 只是额外过滤层，统计阈值才是底线）。

    成本：单次 LLM 调用，输入 ~500-2000 token / 输出 ~200-500 token，
    DeepSeek-Chat 价格 ≈ ¥0.001-0.005 一次。每天最多一次。
    """
    if not candidates:
        return [], []

    # 缩简候选 payload，不浪费 token
    minimal = [
        {
            "id": i,
            "asset": c["asset"],
            "action": _label(c),  # 候选存的是 "verdict" 键，无 "action"；_label 兼容两者
            "regime": c["regime"],
            "window_days": c["window_days"],
            "count": c["count"],
            "hit_rate": c["hit_rate"],
            "avg_return_pct": c["avg_return_pct"],
            "score": c["score"],
        }
        for i, c in enumerate(candidates)
    ]

    from core.config import get_locked
    min_recall = _get_dreaming_config().min_recall
    _, locked_dreaming, _, _ = get_locked()
    min_score = locked_dreaming.min_score
    user_payload = (
        f"统计阈值: hit_rate≥0.5 隐含; count≥{min_recall}; score≥{min_score}\n"
        f"候选数: {len(candidates)}\n"
        f"候选 JSON:\n{json.dumps(minimal, ensure_ascii=False, indent=2)}"
    )

    # 统一从 utils.llm 读 LLM 配置（默认 DeepSeek，可通过 LLM_* env 换千问/智谱）
    from utils.llm import get_llm_config_safe, needs_thinking_disabled
    api_key, base_url, model, _provider = get_llm_config_safe()
    if not api_key:
        log.warning("LLM_API_KEY / DEEPSEEK_API_KEY 未设，跳过 LLM 验伪 (全部 KEEP)")
        return [True] * len(candidates), []

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        # DeepSeek v4 默认 thinking 模式且不兼容 response_format，需 disable thinking；
        # 千问/智谱/OpenAI 等不需要这个 extra_body
        extra_body = {}
        if needs_thinking_disabled(model):
            extra_body["thinking"] = {"type": "disabled"}
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": LLM_VERIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.1,  # 验伪要稳定，不要发散
            timeout=60,
            response_format={"type": "json_object"},
            extra_body=extra_body or None,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        verdicts = parsed.get("verdicts") or []
    except Exception as e:  # noqa: BLE001  网络/解析任一失败 → 全 KEEP fallback
        log.warning(f"Dreaming LLM 验伪失败 ({type(e).__name__}: {e})，fallback 全 KEEP")
        return [True] * len(candidates), []

    # 把 verdicts 映射到 mask
    mask = [True] * len(candidates)
    for v in verdicts:
        try:
            idx = int(v.get("id", -1))
            decision = str(v.get("decision", "KEEP")).upper().strip()
            if 0 <= idx < len(candidates) and decision == "REJECT":
                mask[idx] = False
        except (ValueError, TypeError):
            continue

    return mask, verdicts


def _label(c: Dict[str, Any]) -> str:
    """候选的动作标签：verdict（新 verdict-outcome 路径）优先，回落 action（旧成交路径）。"""
    return c.get("verdict") or c.get("action") or "?"


def _score(c: Dict[str, Any]) -> float:
    """综合评分。

    verdict-outcome 路径（带 'verdict' 字段）：纯可靠性评分 = 命中率 0.7 + 样本量 0.3。
      不再用 abs(avg_return) 加分——对 HOLD 而言"市场动得越大"恰恰说明 HOLD 越错，
      用绝对收益加分会反向奖励坏的 HOLD 模式。命中率已是方向正确性的唯一可信度量。
    旧成交路径（带 'action'）：保持原公式（命中率 0.5 + 收益绝对值 0.3 + 样本量 0.2），
      不影响既有 LLM-验伪契约测试。
    """
    sample = min(c["count"] / 10.0, 1.0)
    if "verdict" in c:
        if c.get("kind") == "caution":
            # lift-based caution 评分（2026-05-27 ADR 008）。旧公式用绝对 missed_up_rate，
            # 会把"单向上涨 regime 里 HOLD 必然踏空"当强信号（Phase1.5 假 caution 即此）。
            # 试金石：新公式同时拒绝 Phase1.5 假 caution（base_down≈0）和 post-cutoff 急跌
            # caution（V 反弹 base_down≈0），只在"真有下行风险 + HOLD 比基率更踏空"时接受。
            base_up = c.get("base_up")
            base_down = c.get("base_down")
            if base_up is None or base_down is None:
                return 0.0  # 无 regime 基率 → 无法判真伪 → 安全休眠
            if base_down < CAUTION_MIN_BASE_DOWN:
                return 0.0  # regime 无真实下行 → "踏空"是单向基率假象 → 非 caution
            lift = c.get("missed_up_rate", 0.0) - base_up
            if lift <= 0:
                return 0.0  # HOLD 没比该 regime 基率更频繁踏空 → 非真信号
            quality = min(lift / CAUTION_LIFT_FULL, 1.0)
            return round(quality * 0.7 + sample * 0.3, 3)
        # reliable：方向判断越准越有价值 → 用 hit_rate（不变）
        return round(c["hit_rate"] * 0.7 + sample * 0.3, 3)
    avg = min(abs(c["avg_return_pct"]) / 5.0, 1.0)
    return round(c["hit_rate"] * 0.5 + avg * 0.3 + sample * 0.2, 3)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()


def _prune_rejected_from_candidates(
    store: MemoryStore, rejected: List[Dict[str, Any]],
) -> None:
    """从 candidates.json 移除被 LLM 验伪 REJECT 的条目。

    用 (asset, verdict, regime, window_days) 作为 identity key 匹配。
    """
    pool = store.read_dream_state("candidates")
    if not pool or "candidates" not in pool:
        return

    # 构建 rejected 的 identity set
    def _key(c: Dict[str, Any]) -> tuple:
        return (c.get("asset"), c.get("verdict"),
                tuple(c.get("regime", [])), c.get("window_days"))

    rejected_keys = {_key(c) for c in rejected}
    original_count = len(pool["candidates"])
    pool["candidates"] = [
        c for c in pool["candidates"] if _key(c) not in rejected_keys
    ]
    pruned = original_count - len(pool["candidates"])
    if pruned > 0:
        store.write_dream_state("candidates", pool)
        log.info(f"candidates.json: 移除 {pruned} 条 LLM REJECT 条目")


def deep_sleep(store: MemoryStore, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """阈值门通过的写 insights/*.md + 更新 MEMORY.md + 追加 DREAMS.md

    P1-3: 阈值门后再加一道可选 LLM 验伪（INVEST_DREAMING_LLM_VERIFY=1 启用）。
    """
    from core.config import get_locked
    _, locked_dreaming, _, _ = get_locked()
    accepted: List[Dict[str, Any]] = []
    for c in candidates:
        score = _score(c)
        if score < locked_dreaming.min_score or c["count"] < _get_dreaming_config().min_recall:
            continue
        c["score"] = score
        accepted.append(c)

    if not accepted:
        store.dream_event({"phase": "deep_sleep", "accepted": 0,
                          "note": "no_candidate_passed_threshold"})
        return []

    # P1-3 LLM 验伪（可选）：默认 off，env 开 INVEST_DREAMING_LLM_VERIFY=1 启用
    # 设计意图：统计阈值（命中率 + 样本量）能挡掉随机噪音，但挡不掉
    # "样本被切薄的虚假相关性"。让一个廉价 LLM 看完所有候选一次性给意见，
    # 把"看着像但其实是过拟合"的候选 REJECT 掉。LLM 不能改原数据，只能否决。
    if os.getenv("INVEST_DREAMING_LLM_VERIFY", "0") == "1":
        keep_mask, verdicts = _llm_verify_candidates(accepted)
        kept = [c for c, k in zip(accepted, keep_mask) if k]
        rejected = [c for c, k in zip(accepted, keep_mask) if not k]
        rejected_count = len(rejected)
        store.dream_event({
            "phase": "deep_sleep_llm_verify",
            "input_count": len(accepted),
            "kept": len(kept),
            "rejected": rejected_count,
            "verdicts": verdicts[:20],  # 限长，防 events.jsonl 爆
        })
        log.info(
            f"Dreaming LLM 验伪: {len(accepted)} 候选 → "
            f"KEEP {len(kept)} / REJECT {rejected_count}",
        )
        # 从 candidates.json 移除 REJECT 条目，防止下次 deep_sleep 重复评估
        if rejected:
            _prune_rejected_from_candidates(store, rejected)
        accepted = kept
        if not accepted:
            store.dream_event({"phase": "deep_sleep", "accepted": 0,
                              "note": "all_rejected_by_llm_verify"})
            return []

    insights_dir = store.root / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    # 按需懒加载 InsightsDB（DB 路径与 dreaming job 解耦；测试时传 db=None 跳过）
    from db.insights_db import InsightsDB
    try:
        _insights_db: Optional[InsightsDB] = InsightsDB()
    except Exception as e:
        log.warning(f"InsightsDB 初始化失败（降级：只写 .md）: {e}")
        _insights_db = None

    for c in accepted:
        regime_tag = "_".join(c["regime"]) or "any"
        label = _label(c)
        is_caution = c.get("kind") == "caution"
        kind_tag = "caution" if is_caution else "reliable"
        slug = _slugify(f"{c['asset']}_{label}_{regime_tag}_{c['window_days']}d_{kind_tag}")
        if is_caution:
            missed = c.get("missed_up_rate", 0.0)
            title = f"⚠️ 告诫: {regime_tag} 里对 {c['asset']} HOLD 频繁踏空"
            interp = (
                f"历史上当市场处于 **{regime_tag}** 时，委员会对 {c['asset']} 给出 **HOLD**，"
                f"但 {c['window_days']} 天后有 **{missed*100:.0f}%** 的情况市场明显上涨"
                f"（期间平均涨跌 {c['avg_return_pct']:+.2f}%，n={c['count']}）——**子弹揣在兜里踏空了**。\n\n"
                f"> **行动建议**：该 regime 下应**降低 HOLD 倾向**，更积极考虑 BUY/ACCUMULATE。"
                f"这不是噪音——是委员会过度保守、错过上涨的实证模式。"
            )
        else:
            title = f"长期洞察: 委员会对 {c['asset']} 给出 {label} 在 {regime_tag} 市场环境下"
            interp = (
                f"历史上当市场处于 **{regime_tag}** 时，委员会对 {c['asset']} 给出 **{label}**，"
                f"{c['window_days']} 天后这个判断的命中率是 **{c['hit_rate']*100:.0f}%**"
                f"（n={c['count']}，期间实际平均涨跌 {c['avg_return_pct']:+.2f}%）。\n\n"
                f"> 命中率高 = 该模式经过事后验证，可加权。"
            )
        body = f"""# {title}

## 统计

- 样本数: {c['count']}
- {c['window_days']}天后命中率: {c['hit_rate']*100:.1f}%
- 平均 {c['window_days']}天后实际涨跌: {c['avg_return_pct']:+.2f}%
{f"- 踏空率(HOLD 但市场涨): {c.get('missed_up_rate',0)*100:.1f}% / 躲跌率: {c.get('avoided_down_rate',0)*100:.1f}%" if is_caution or label == "HOLD" else ""}
- 综合评分: {c['score']:.3f}

## 解读

{interp}

> 这条由 Dreaming Deep Sleep 从 verdict_review（委员会建议 vs 实际盘）自动提炼，
> "没动"阈值按资产波动率定（非写死），经 MEMORY.md 索引注入决策上下文。
"""
        # 双写：.md 文件（人类可读副本，渐进迁移保留）+ SQLite（SQL 查询后端）
        store.write(f"insights/{slug}", "insight", c, body)
        if _insights_db is not None:
            try:
                _insights_db.upsert_from_candidate(slug, c, body)
            except Exception as e:
                log.warning(f"insights SQLite 写入失败（slug={slug}）: {e}")
        store.dream_event({"phase": "deep_sleep", "accepted": slug, "score": c["score"]})

    # 更新 MEMORY.md 索引
    _update_memory_index(store, accepted)

    # 追加 DREAMS.md 叙事日记
    _append_dreams_diary(store, accepted)

    return accepted


def _candidate_slug(c: Dict[str, Any]) -> str:
    regime_tag = "_".join(c["regime"]) or "any"
    return _slugify(f"{c['asset']}_{_label(c)}_{regime_tag}_{c['window_days']}d")


def _update_memory_index(store: MemoryStore, accepted: List[Dict[str, Any]]) -> None:
    index_path = store.root / "MEMORY.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    insight_lines = []
    for c in accepted:
        slug = _candidate_slug(c)
        regime_str = "_".join(c["regime"]) or "any"
        insight_lines.append(
            f"- [insights/{slug}.md](insights/{slug}.md) — "
            f"{c['asset']} / {_label(c)} / {regime_str} / "
            f"{c['window_days']}d / hit={c['hit_rate']*100:.0f}% / score={c['score']:.2f}"
        )
    insight_block = (
        "\n## 长期洞察 (Deep Sleep 写入 - 自动维护)\n\n"
        + "\n".join(insight_lines) + "\n"
    )

    if "## 长期洞察 (Deep Sleep" in existing:
        # 替换旧块（到下一个 `## ` 或文件结尾）
        existing = re.sub(
            r"\n## 长期洞察 \(Deep Sleep.*?(?=\n## |\Z)",
            insight_block,
            existing,
            flags=re.DOTALL,
        )
    else:
        existing = existing.rstrip() + "\n" + insight_block
    index_path.write_text(existing, encoding="utf-8")


def _append_dreams_diary(store: MemoryStore, accepted: List[Dict[str, Any]]) -> None:
    """模板化叙事 - 不依赖 LLM"""
    today = datetime.now().strftime("%Y-%m-%d")
    diary_path = store.root / "DREAMS.md"
    is_new = not diary_path.exists()

    lines = [f"\n## {today} 梦日记\n"]
    if not accepted:
        lines.append("- 今晚平静，没有新的洞察通过阈值门。\n")
    else:
        lines.append(f"- 今晚 Deep Sleep 处理了 {len(accepted)} 条新洞察：\n")
        _verb_map = {"bought": "买入", "sold": "卖出", "BUY": "买入", "ACCUMULATE": "加仓",
                     "HOLD": "持有", "TRIM": "减仓", "SELL": "卖出"}
        for c in accepted:
            regime_tag = "/".join(c["regime"]) or "任意"
            label = _label(c)
            verb = _verb_map.get(label, label)
            outcome_word = "上涨" if c["avg_return_pct"] > 0 else "下跌"
            lines.append(
                f"  - 市场 {regime_tag} 时委员会判 {verb} {c['asset']}，"
                f"{c['window_days']}天后实际{outcome_word} "
                f"{abs(c['avg_return_pct']):.1f}% "
                f"(命中率 {c['hit_rate']*100:.0f}%, 评分 {c['score']:.2f})\n"
            )

    with open(diary_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write("# Dreams 梦日记\n\n这里记录 Dreaming Deep Sleep 每日的反思。\n")
        f.writelines(lines)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def run() -> Dict[str, Any]:
    """跑三阶段 dreaming，带 consolidation lock 防止多进程并发撕裂数据

    锁仿 Claude Code v2.1.88 leaked 的 src/services/autoDream/consolidationLock.ts
    （PID + mtime 文件锁，60min stale guard）
    """
    store = MemoryStore()
    prior = try_acquire_consolidation_lock(store.root)
    if prior is None:
        return {"status": "skipped", "reason": "consolidation_lock_held"}

    try:
        store.dream_event({"phase": "start", "lock_acquired": True})
        signals = light_sleep(store)
        candidates = rem_sleep(store, signals)
        accepted = deep_sleep(store, candidates)
        store.dream_event({"phase": "end", "accepted": len(accepted)})
        return {
            "status": "success",
            "signals": len(signals),
            "candidates": len(candidates),
            "accepted_insights": len(accepted),
        }
    except Exception as e:
        # 出错就把 mtime 倒回去，下次还能跑
        rollback_consolidation_lock(store.root, prior)
        store.dream_event({"phase": "error", "error": str(e)})
        raise


if __name__ == "__main__":
    print(run())
