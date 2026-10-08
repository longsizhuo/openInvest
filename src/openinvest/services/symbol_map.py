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


# 港股 ticker 归一化：HKEX 用补零到 5 位的写法（如 01234.HK），yfinance 用 4 位
# （1234.HK）。LLM 归一化器两种都吐——events.db 里同一标的两种写法长期并存，而
# 召回/触发两处都是精确字符串交集匹配 → 补零那批对委员会完全不可见（曾因此丢过
# 高严重度事件）。
# 规则：去前导零后不足 4 位补到 4 位；本身 >4 位（如 80737.HK 人民币柜台）原样保留。
_HK_TICKER_RE = re.compile(r"^0*(\d{1,5})\.HK$", re.I)


def normalize_symbol(symbol: str) -> str:
    """把 ticker 归一成 yfinance canonical 写法。非港股原样返回（仅 strip + upper）。"""
    s = str(symbol or "").strip().upper()
    m = _HK_TICKER_RE.match(s)
    if not m:
        return s
    digits = m.group(1)
    return f"{digits.zfill(4) if len(digits) < 4 else digits}.HK"


# 同一家公司的跨市场代码：港股上市 ↔ 美股 ADR / OTC。LLM 归一化器常用美股/OTC 代码给
# 港股公司打标（打标 OTC 代码的事件对持港股的用户召回 / 触发 / EVENT_STANCE 全不可见）。
# 通用金融知识批量表，准入标准同 TRACKING_WHITELIST：任何用户持有该 ticker 都成立。
HK_CROSS_LISTINGS: Dict[str, FrozenSet[str]] = {
    "0700.HK": frozenset({"TCEHY", "TCTZF"}),   # Tencent
    "9988.HK": frozenset({"BABA", "BABAF"}),    # Alibaba
    "3690.HK": frozenset({"MPNGY", "MPNGF"}),   # Meituan
    "1810.HK": frozenset({"XIACY", "XIACF"}),   # Xiaomi
    "1024.HK": frozenset({"KUASF"}),            # Kuaishou
    "9618.HK": frozenset({"JD"}),               # JD.com
    "9888.HK": frozenset({"BIDU"}),             # Baidu
    "9999.HK": frozenset({"NTES"}),             # NetEase
    "1211.HK": frozenset({"BYDDY", "BYDDF"}),   # BYD
    "2015.HK": frozenset({"LI"}),               # Li Auto
    "9866.HK": frozenset({"NIO"}),              # NIO
    "9868.HK": frozenset({"XPEV"}),             # XPeng
    "9961.HK": frozenset({"TCOM"}),             # Trip.com
    "9626.HK": frozenset({"BILI"}),             # Bilibili
    "2318.HK": frozenset({"PNGAY", "PIAIF"}),   # Ping An
    "1299.HK": frozenset({"AAGIY", "AAIGF"}),   # AIA
    "0388.HK": frozenset({"HKXCY", "HKXCF"}),   # HKEX
    "2020.HK": frozenset({"ANPDY", "ANPDF"}),   # Anta
    "0175.HK": frozenset({"GELYY", "GELYF"}),   # Geely
    "0992.HK": frozenset({"LNVGY", "LNVGF"}),   # Lenovo
}
_ALIAS_TO_HK: Dict[str, str] = {a: hk for hk, als in HK_CROSS_LISTINGS.items() for a in als}


def listing_aliases(symbol: str) -> FrozenSet[str]:
    """同一家公司的全部上市代码（双向：港股 ↔ ADR/OTC），不在表里 → {自身}；空 → 空集合。
    只含"同一资产"，不含跟踪关系（ETF→指数走 proxy_symbols_for）。"""
    s = normalize_symbol(symbol)
    if not s:
        return frozenset()
    hk = _ALIAS_TO_HK.get(s, s)
    return frozenset({s, hk} | HK_CROSS_LISTINGS.get(hk, frozenset()))


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
    # 沪深 300（常见 ETF，沪深两所）
    "510300.SS": frozenset({"000300.SS"}),
    "510310.SS": frozenset({"000300.SS"}),
    "510330.SS": frozenset({"000300.SS"}),
    "159919.SZ": frozenset({"000300.SS"}),
    "159925.SZ": frozenset({"000300.SS"}),
}


def proxy_symbols_for(
    symbol: str,
    *,
    tracks: Optional[Iterable[str] | str] = None,
) -> FrozenSet[str]:
    """用户标的的代理匹配集合：{自身} ∪ {同公司跨市场代码} ∪ {追踪的 canonical}。

    tracks: strategy.target_assets 里该资产的可选 `tracks` 字段（str 或 list），
    用户显式声明优先——白名单覆盖不到的新 ETF 由用户自己配，零代码改动。
    symbol 为空 → 空集合（graceful）。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return frozenset()
    out = {s} | listing_aliases(s)
    out |= TRACKING_WHITELIST.get(s, frozenset())
    if tracks:
        if isinstance(tracks, str):
            tracks = [tracks]
        out |= {str(t).strip().upper() for t in tracks if str(t).strip()}
    return frozenset(out)


# A 股指数（沪 000xxx / 深 399xxx）与场内基金（沪 5xxxxx / 深 15–18xxxx）代码段
_A_SHARE_NON_STOCK = re.compile(r"^(?:000\d{3}|5\d{5})\.SS$|^(?:399\d{3}|1[5-8]\d{4})\.SZ$")


def is_single_stock(symbol: str, *, tracks: Optional[Iterable[str] | str] = None) -> bool:
    """单一公司股票？指数(^) / 期货·汇率(=) / 加密(-USD) / 跟踪型 ETF（白名单或 tracks 声明）/
    A 股指数·基金代码段 → False。
    ponytail: 纯代码规则不联网查 quoteType；白名单外的港美 ETF 会被当成单股，有误判再补白名单。"""
    s = normalize_symbol(symbol)
    if not s or s.startswith("^") or "=" in s or s.endswith(("-USD", "-USDT")):
        return False
    if proxy_symbols_for(s, tracks=tracks) - listing_aliases(s):
        return False
    return not _A_SHARE_NON_STOCK.match(s)


__all__ = [
    "ENTITY_CANONICAL_PATTERNS",
    "HK_CROSS_LISTINGS",
    "TRACKING_WHITELIST",
    "canonical_symbols_for_entities",
    "is_single_stock",
    "listing_aliases",
    "normalize_symbol",
    "proxy_symbols_for",
]
