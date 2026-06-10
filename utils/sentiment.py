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
import urllib.request
from typing import Optional, Tuple

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
        from utils.exchange_fee import get_history_data
        df = get_history_data("^VIX", VIX_PERIOD)
    except Exception as e:  # noqa: BLE001
        log.warning(f"VIX 行情拉取失败 graceful: {type(e).__name__}: {e}")
        return None
    if df is None or df.empty or "Close" not in df:
        return None
    closes = df["Close"].dropna()
    if len(closes) < 20:  # 样本太少分位无意义
        return None
    last = float(closes.iloc[-1])
    pct = float((closes <= last).mean())  # 真百分位排名（≤ 当前的比例）
    return last, pct


def _vix_label(pct: float) -> str:
    """VIX 自身分位 → 恐慌贪婪标签（高 VIX = 恐慌）。阈值走 config (sentiment 节)。"""
    from core.config import load_config
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


def _event_stance_line(event_brief: str) -> Optional[str]:
    """从已召回的 event_brief 文本数 risk/opportunity 标签，给一个净情绪聚合行。

    纯字符串计数（event layer 已有的 stance），不发起任何新 IO / LLM。
    """
    if not event_brief:
        return None
    risk = event_brief.count("[risk/")
    opp = event_brief.count("[opportunity/")
    if risk == 0 and opp == 0:
        return None
    net = "risk" if risk > opp else "opportunity" if opp > risk else "neutral"
    return f"EVENT_STANCE: net {net} (risk={risk} opportunity={opp}, 来自近期事件层)"


def _cnn_enabled() -> bool:
    return os.getenv("INVEST_SENTIMENT_CNN_ENABLED", "true").lower() not in {
        "0", "false", "no", "off",
    }


def build_sentiment_brief(event_brief: str = "", *, cnn_enabled: Optional[bool] = None) -> str:
    """组装确定性市场情绪表盘文本。

    VIX 分位是保底；VIX 都拿不到 → 返回 ""（保持 graceful loader 契约）。
    CNN 默认尝试，失败静默跳过。event_brief 非空时附净情绪聚合行（纯计数）。

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

    from core.config import load_config
    if pct >= load_config().sentiment.vix_defense_quantile:
        lines.append(
            "INDEP_DEFENSE_FLAG: on  "
            "# VIX 处近2年高位=市场恐慌，独立于 MA regime 的快速崩盘哨兵；"
            "加仓需谨慎，优先考虑防御（regime 可能仍滞后显示 uptrend）"
        )
    else:
        lines.append("INDEP_DEFENSE_FLAG: off")

    return "\n".join(lines)


__all__ = ["build_sentiment_brief", "fetch_cnn_fear_greed"]
