"""确定性 entity→symbol 映射层（issue #26）

LLM 标注不可靠（实测：NDQ.AX 在 4,350 条事件里被标 affected_symbols 的次数=0，
LLM 只标成分股；央行购金类宏观事件常不标 GC=F）→ 用纯代码规则兜底。两层：

1. **实体 → canonical symbol**（通用常量，词边界正则）：
   事件归一化时兜底补 affected_symbols。黄金映射从 event_normalizer 迁入，
   新增主要指数。canonical 指数（^NDX 等）是中性事实，不绑任何用户。

2. **用户标的 → 代理匹配集合** `proxy_symbols_for()`：
   召回/过滤时，用户持仓 ticker 扩展为 {自身} ∪ {它追踪的 canonical}。
   内置"知名 ETF→指数"白名单（通用金融知识，同 rss_feeds.yml 性质，非用户
   调参）+ strategy.target_assets 可选 `tracks:` 字段覆盖/扩展。
   **驱动标的永远来自调用方传入的 symbol——本模块不持有任何用户持仓列表。**

红线：词边界正则（\\bgold\\b 不命中 goldman sachs）；通用代码禁止出现
"某个用户个人持有的标的"作为驱动逻辑——白名单条目必须是通用金融知识。
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 第一层：实体 → canonical symbol（事件归一化兜底）
# ---------------------------------------------------------------------------
ENTITY_CANONICAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # 黄金（2026-06 待办5 引入，自 event_normalizer 迁入）
    (re.compile(r"\bgold\b|\bbullion\b|\bxau\b"), "GC=F"),
    # 主要指数（issue #26）：LLM 提到指数时往往只标成分股
    (re.compile(r"\bnasdaq(?:[ -]?100)?\b|\bndx\b"), "^NDX"),
    (re.compile(r"\bs&p ?500\b|\bspx\b"), "^GSPC"),
    (re.compile(r"\bdow jones\b|\bdjia\b"), "^DJI"),
]


def canonical_symbols_for_entities(entities: Iterable[str]) -> List[str]:
    """entities 命中第一层映射 → canonical symbol 列表（保序去重）。纯代码规则，零 LLM。"""
    joined = " ".join(str(e).lower() for e in entities if str(e).strip())
    out: List[str] = []
    for pattern, sym in ENTITY_CANONICAL_PATTERNS:
        if pattern.search(joined) and sym not in out:
            out.append(sym)
    return out


# ---------------------------------------------------------------------------
# 第二层：用户标的 → 代理匹配集合（召回/过滤侧）
# ---------------------------------------------------------------------------
# 知名 ETF/代理 → 它追踪的 canonical symbol。通用金融知识白名单——新条目的
# 准入标准是"任何用户持有该 ticker 都成立"，不是"某个用户恰好持有"。
TRACKING_WHITELIST: Dict[str, FrozenSet[str]] = {
    # 纳指 100
    "NDQ.AX": frozenset({"^NDX"}),
    "QQQ": frozenset({"^NDX"}),
    "QQQM": frozenset({"^NDX"}),
    # 标普 500
    "SPY": frozenset({"^GSPC"}),
    "VOO": frozenset({"^GSPC"}),
    "IVV": frozenset({"^GSPC"}),
    # 黄金（与 jobs/event_watch._GOLD_PROXY_SYMBOLS 同源语义）
    "GLD": frozenset({"GC=F"}),
    "IAU": frozenset({"GC=F"}),
    "PMGOLD.AX": frozenset({"GC=F"}),
    "518880.SS": frozenset({"GC=F"}),
}


def proxy_symbols_for(
    symbol: str,
    *,
    tracks: Optional[Iterable[str] | str] = None,
) -> FrozenSet[str]:
    """用户标的的代理匹配集合：{自身} ∪ {追踪的 canonical}。

    tracks: strategy.target_assets 里该资产的可选 `tracks` 字段（str 或 list），
    用户显式声明优先——白名单覆盖不到的新 ETF 由用户自己配，零代码改动。
    symbol 为空 → 空集合（graceful）。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return frozenset()
    out = {s}
    out |= TRACKING_WHITELIST.get(s, frozenset())
    if tracks:
        if isinstance(tracks, str):
            tracks = [tracks]
        out |= {str(t).strip().upper() for t in tracks if str(t).strip()}
    return frozenset(out)


__all__ = [
    "ENTITY_CANONICAL_PATTERNS",
    "TRACKING_WHITELIST",
    "canonical_symbols_for_entities",
    "proxy_symbols_for",
]
