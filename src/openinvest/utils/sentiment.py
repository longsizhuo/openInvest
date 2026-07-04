"""市场情绪表盘（确定性 shared 块）：VIX 历史分位（保底）+ CNN 恐慌贪婪（锦上添花，可降级）

对齐 TradingAgents「情绪分析师」维度。设计红线（用户 2026-06-09 确认）：

1. **VIX 历史分位是主信号** —— 纯确定性算（^VIX 已在抓），零外部依赖，永远可用。
   这是 sentiment_brief 的底座，CNN 挂了它照常工作。
2. **CNN Fear&Greed 是附加信息** —— 能抓到就加进 brief，抓不到（DNS / 断供 / 超时）
   就 graceful 跳过，**绝不让 CNN 成为单点故障**。env INVEST_SENTIMENT_CNN_ENABLED
   可硬关（默认开，但失败即静默降级，等价"prod 不可达时自动只用 VIX"）。
3. **INDEP_DEFENSE_FLAG** —— VIX 分位 ≥ 阈值 → on。这是**独立于 MA regime 的快速崩盘
   哨兵**：MA120 追不上 COVID 类崩盘（全程被分类 uptrend），而 VIX 实时反映恐慌。
   给 CIO 一个不挂 regime 锁的防御触发线索。

这些进决策的方式 = 确定性背景事实（像 regime_brief），agent/CIO 必须纳入并说明，
不是新增投票 agent 改方向。最终方向仍由概率表路径锚定 + CIO 否决权。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

VIX_PERIOD = "2y"  # 数据窗口定义（与 brief 文案"近2年分位"/price_quantile_2y 口径一致，非调参项）
# 恐慌/贪婪分档 + 快崩哨兵线 → core/config (sentiment 节)，defaults.yaml 可调，
# env INVEST_SENTIMENT_<KEY> 可覆盖

CNN_FNG_URL = "https://production.dataviz.cnn.com/index/fearandgreed/graphdata"
CNN_TIMEOUT_S = float(os.getenv("INVEST_SENTIMENT_CNN_TIMEOUT_S", "4"))


def _vix_percentile() -> Optional[Tuple[float, float]]:
    """返回 (vix_last, 近2年百分位 0-1)；无数据返回 None（graceful）。

    用 get_history_data → 自带 DB / CSV 兜底，yfinance 挂也能退化到本地行情库。
    """
    try:
        from openinvest.utils.exchange_fee import get_history_data
        df = get_history_data("^VIX", VIX_PERIOD)
    except Exception as e:  # noqa: BLE001
        log.warning(f"VIX 行情拉取失败 graceful: {type(e).__name__}: {e}")
        return None
    if df is None or df.empty or "Close" not in df:
        return None
    # 口径修正（2026-06-13）：get_history_data("^VIX","2y") 返回 get_history_df(默认730行)，
    # 此前 (closes <= last).mean() 对 730 行(≈2.9年)算分位 → 与回测/canonical 504 分叉。
    # 强制 tail(TRADING_DAYS_2Y=504)，与 validate_gold_defense rolling(504)、price_quantile_2y
    # 同口径（单一可信源 utils.market_metrics；transcript 4 点校验过生产=730 窗）。
    from openinvest.utils.market_metrics import TRADING_DAYS_2Y
    closes = df["Close"].dropna().tail(TRADING_DAYS_2Y)
    if len(closes) < 20:  # 样本太少分位无意义
        return None
    last = float(closes.iloc[-1])
    pct = float((closes <= last).mean())  # 真百分位排名（近2年内 ≤ 当前的比例）
    return last, pct


def _vix_label(pct: float) -> str:
    """VIX 自身分位 → 恐慌贪婪标签（高 VIX = 恐慌）。阈值走 config (sentiment 节)。"""
    from openinvest.core.config import load_config
    cfg = load_config().sentiment
    if pct >= cfg.vix_extreme_fear_q:
        return "extreme_fear"
    if pct >= cfg.vix_fear_q:
        return "fear"
    if pct <= cfg.vix_extreme_greed_q:
        return "extreme_greed"
    if pct <= cfg.vix_greed_q:
        return "greed"
    return "neutral"


def fetch_cnn_fear_greed(timeout: float = CNN_TIMEOUT_S) -> Optional[Tuple[int, str]]:
    """CNN Fear&Greed 当前值 (0-100, rating)。任何失败（DNS/超时/格式变）返回 None。

    **绝不抛异常** —— 调用方据此 graceful 跳过 CNN 行，VIX 分位照常输出。
    """
    try:
        req = urllib.request.Request(
            CNN_FNG_URL,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        fg = data.get("fear_and_greed") or {}
        score = fg.get("score")
        if score is None:
            return None
        rating = str(fg.get("rating", "") or "").strip().replace(" ", "_")
        return int(round(float(score))), rating or "unknown"
    except Exception as e:  # noqa: BLE001  绝不单点故障
        log.info(f"CNN F&G 不可达 graceful 跳过（VIX 分位仍正常）: {type(e).__name__}: {str(e)[:80]}")
        return None


# event_brief 头行解析（格式契约：core/committee_runner.py:format_event_brief 产出
# `[ts] [stance/severity] [SYM1, SYM2] (sources: ...)`，两处 docstring 互相引用；
# 改任何一边必须同步另一边 + tests/test_event_rag_resolve.py 的互解析回归测试）
_EVENT_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]*)\]\s+\[(?P<stance>risk|opportunity|neutral)/"
    r"(?P<sev>low|mid|high)\]\s+\[(?P<syms>[^\]]*)\]",
    re.MULTILINE,
)
_SEV_INDEX = {"low": 0, "mid": 1, "high": 2}
_STANCE_SIGN = {"opportunity": 1.0, "risk": -1.0, "neutral": 0.0}


def _parse_event_brief_entries(event_brief: str) -> List[Dict[str, Any]]:
    """解析 brief 头行 → [{ts, stance, sev, syms}]。纯文本解析，零新 IO。

    容错：ts 缺失/不可解析（LLM 透传的 ts 可能 naive/空/错标）→ ts=None，
    打分时按无衰减计（recall 7d 窗口兜底过权风险）。naive ts 当 UTC。
    """
    entries: List[Dict[str, Any]] = []
    for m in _EVENT_HEADER_RE.finditer(event_brief or ""):
        ts: Optional[datetime] = None
        raw_ts = m.group("ts").strip()
        if raw_ts:
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
        syms = {s.strip().upper() for s in m.group("syms").split(",") if s.strip()}
        entries.append({
            "ts": ts, "stance": m.group("stance"), "sev": m.group("sev"), "syms": syms,
        })
    return entries


def _stance_score(
    entries: List[Dict[str, Any]], *, now: Optional[datetime] = None,
) -> Tuple[float, int, int]:
    """加权净分 (score, risk计数, opportunity计数)。

    score = Σ sign × w(severity) × 0.5^(age_h/half_life)；opportunity 正、risk 负、
    neutral 0；age 负值（错标未来）clamp 0。**默认 config（等权 + half_life=0 禁用
    衰减）下 score ≡ opportunity计数 − risk计数，net 判定与旧纯计数逐位一致**——
    加权公式经 scripts/eval_event_stance.py 验证前保持禁用（2026-06-11 基线判
    INSUFFICIENT_DATA，ADR-010 rule 4 纪律）。
    """
    from openinvest.core.config import load_config
    cfg = load_config().sentiment
    weights = (cfg.event_stance_w_low, cfg.event_stance_w_mid, cfg.event_stance_w_high)
    half_life = cfg.event_stance_half_life_hours
    now = now or datetime.now(timezone.utc)
    score, risk, opp = 0.0, 0, 0
    for e in entries:
        sign = _STANCE_SIGN.get(e["stance"], 0.0)
        if e["stance"] == "risk":
            risk += 1
        elif e["stance"] == "opportunity":
            opp += 1
        if sign == 0.0:
            continue
        w = weights[_SEV_INDEX.get(e["sev"], 0)]
        if half_life > 0 and e["ts"] is not None:
            age_h = max(0.0, (now - e["ts"]).total_seconds() / 3600.0)
            w *= 0.5 ** (age_h / half_life)
        score += sign * w
    return score, risk, opp


def _format_stance_line(
    score: float, risk: int, opp: int, *, symbol: Optional[str] = None,
) -> Optional[str]:
    """净情绪行格式化。默认 config 下输出与旧纯计数版完全一致（不显示 score）。"""
    if risk == 0 and opp == 0:
        return None
    from openinvest.core.config import load_config
    cfg = load_config().sentiment
    band = cfg.event_stance_neutral_band
    net = "risk" if score < -band else "opportunity" if score > band else "neutral"
    weighted = (
        (cfg.event_stance_w_low, cfg.event_stance_w_mid, cfg.event_stance_w_high)
        != (1.0, 1.0, 1.0)
        or cfg.event_stance_half_life_hours > 0
    )
    label = f"EVENT_STANCE({symbol})" if symbol else "EVENT_STANCE"
    extra = f"score={score:+.1f}, " if weighted else ""
    suffix = "该资产相关事件" if symbol else "来自近期事件层"
    return f"{label}: net {net} ({extra}risk={risk} opportunity={opp}, {suffix})"


def _event_stance_line(event_brief: str) -> Optional[str]:
    """市场级净情绪聚合行。纯文本解析 + 算术，不发起任何新 IO / LLM。

    标准 brief（format_event_brief 产出）走头行解析 + 加权打分（默认配置 =
    纯计数行为）；非标准文本（手传 override）退化为旧纯计数。
    """
    if not event_brief:
        return None
    entries = _parse_event_brief_entries(event_brief)
    if entries:
        score, risk, opp = _stance_score(entries)
        return _format_stance_line(score, risk, opp)
    # 退化：解析不出头行（任意 override 文本）→ 旧纯计数口径
    risk = event_brief.count("[risk/")
    opp = event_brief.count("[opportunity/")
    return _format_stance_line(float(opp - risk), risk, opp)


def event_stance_line_for_symbol(
    event_brief: str, symbol: str, *, tracks: Optional[Any] = None,
) -> Optional[str]:
    """per-asset 净情绪行：统计头行 [syms] 与该 symbol 代理集合相交的事件。

    驱动标的来自调用方（run_committee_for_symbol 的 symbol 参数 ←
    strategy.target_assets 动态解析）——**本函数不持有任何标的列表**，
    任意 ticker 通用。issue #26：匹配用 services.symbol_map.proxy_symbols_for
    扩展（NDQ.AX 也命中标 ^NDX 的指数事件）；tracks = strategy 资产的可选
    `tracks` 字段透传。session 模式下 brief 是跨资产合并版，靠头行过滤在
    session / standalone 两种模式行为一致。无命中事件 → None（graceful）。
    """
    if not event_brief or not symbol:
        return None
    from openinvest.services.symbol_map import proxy_symbols_for
    targets = proxy_symbols_for(symbol, tracks=tracks)
    entries = [
        e for e in _parse_event_brief_entries(event_brief)
        if e["syms"] & targets
    ]
    if not entries:
        return None
    score, risk, opp = _stance_score(entries)
    return _format_stance_line(score, risk, opp, symbol=symbol)


def _cnn_enabled() -> bool:
    return os.getenv("INVEST_SENTIMENT_CNN_ENABLED", "true").lower() not in {
        "0", "false", "no", "off",
    }


def build_sentiment_brief(event_brief: str = "", *, cnn_enabled: Optional[bool] = None) -> str:
    """组装确定性市场情绪表盘文本。

    VIX 分位是保底；VIX 都拿不到 → 返回 ""（保持 graceful loader 契约）。
    CNN 默认尝试，失败静默跳过。event_brief 非空时附净情绪聚合行
    （默认=纯计数；severity 加权/时效衰减经 scripts/eval_event_stance.py 验证前禁用）。

    Returns:
        多行文本，或 "" 表示连 VIX 都没有（整块降级）。
    """
    vix = _vix_percentile()
    if vix is None:
        return ""
    vix_last, pct = vix
    label = _vix_label(pct)
    lines = [
        f"FEAR_GREED_GAUGE: VIX={vix_last:.1f} (近2年分位 {pct * 100:.0f}%) → {label}",
    ]

    use_cnn = _cnn_enabled() if cnn_enabled is None else cnn_enabled
    if use_cnn:
        cnn = fetch_cnn_fear_greed()
        if cnn is not None:
            lines.append(f"CNN_FNG: {cnn[0]} ({cnn[1]})")

    stance = _event_stance_line(event_brief)
    if stance:
        lines.append(stance)

    from openinvest.core.config import load_config
    if pct >= load_config().sentiment.vix_defense_quantile:
        lines.append(
            "INDEP_DEFENSE_FLAG: on  "
            "# VIX 处近2年高位=市场恐慌，独立于 MA regime 的快速崩盘哨兵；"
            "加仓需谨慎，优先考虑防御（regime 可能仍滞后显示 uptrend）"
        )
    else:
        lines.append("INDEP_DEFENSE_FLAG: off")

    return "\n".join(lines)


__all__ = [
    "build_sentiment_brief",
    "event_stance_line_for_symbol",
    "fetch_cnn_fear_greed",
]
