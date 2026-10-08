"""jobs/event_watch —— 第一层：盘中事件感知 + 邮件通知 + 触发委员会

每 30m cron 跑一次：
1. 拉用户上下文（holdings ∪ target_assets）
2. 构 query → fetch_all 多源新闻
3. event_normalizer.normalize → 结构化事件
4. dedup（同 url 跳过）+ event_store.upsert_event
5. 新入库事件交 services/event_trigger（与 agent 投喂 ingest_event 共用同一道闸）：
   severity ≥ mid + stance ≠ neutral + 影响到关注 symbol → 冷却/日上限 →
   POST /api/committee/run（触发委员会重跑）+ send_event_alert（digest 邮件）

环境变量：
  INVEST_EVENT_MIN_SEVERITY    默认 mid（trigger 阈值）
  INVEST_EVENT_API_URL         触发 committee 的 endpoint，默认 http://127.0.0.1:8765
  INVEST_EVENT_DRY_RUN         真值则不发邮件不触委员会，只入库

CLI:
    python -m jobs.event_watch                # 单次跑
    python -m jobs.event_watch --dry-run      # 只入库
    python -m jobs.event_watch --recall NDQ.AX  # 测 RAG 召回
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openinvest.db.event_store import EventStore
from openinvest.services.embeddings import DEFAULT_DIM
from openinvest.services.event_normalizer import NormalizedEvent, normalize
from openinvest.services.event_trigger import trigger_for_new_events
from openinvest.services.news_sources import fetch_all
from openinvest.services.news_sources.rss_feed import load_feeds

log = logging.getLogger(__name__)

# "是否持金"语义白名单（合理常量，不是用户持仓硬编码——持仓列表始终动态读 PM）：
# 金期货 / 美澳中各地金 ETF。命中任一才追加黄金常驻 queries（anti-noise：
# 不持金的用户不抓金新闻）。
_GOLD_PROXY_SYMBOLS = {"GC=F", "GLD", "IAU", "PMGOLD.AX", "518880.SS"}
# 黄金常驻 queries（待办5）：央行购金 / ETF 流向是金价"基本面"，但属低频宏观事件，
# 普通 per-symbol query 抓不到，需要显式关键词
_GOLD_STANDING_QUERIES = ["central bank gold purchases", "gold ETF flows"]

# 宏观常驻 queries（issue #211）：per-symbol query（"AAPL news"）搜到的是行情评论，
# 搜不到数据发布本身——CPI 公布当天固定管道 0 条入库就是这个缺口炸的。这几个是
# 美国经济日历里最常移动市场的定期发布，跟持仓无关、每轮都搜。
# ponytail: 不按经济日历日程加权触发时机（那需要额外接一个日历数据源判断"今天
# 是不是发布日"），固定每轮都搜——多花几次搜索配额换召回，比精确日历调度便宜。
# 真需要再加日历加权。
_MACRO_STANDING_QUERIES = [
    "CPI inflation report",
    "Fed FOMC rate decision",
    "US non-farm payrolls jobs report",
    "PPI producer price index",
]


def _load_user_context() -> Dict[str, Any]:
    """从 PortfolioManager 抓 holdings / target_assets

    返回 dict 即可，不要把 PM 实例直接传出去（避免 caller 误改持仓）
    """
    try:
        from openinvest.core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
    except Exception as e:
        log.warning(f"PortfolioManager 不可用，event_watch 用空上下文: {e}")
        return {"holdings": [], "watching": [], "queries": []}

    holdings = [h.get("symbol") for h in pm.holdings.all() if h.get("symbol")]
    watching = [a.get("symbol") for a in (pm.strategy.get("target_assets") or [])
                if a.get("symbol")]

    # 构造给 ddgs 用的 query 列表：
    # 每个 watched/held symbol 一条 + 默认 macro keyword
    queries: List[str] = []
    for sym in (set(holdings) | set(watching)):
        queries.append(f"{sym} news")
    queries.extend(_MACRO_STANDING_QUERIES)  # 与持仓无关，每轮都搜（issue #211）
    # 持金/关注金 → 追加黄金常驻 queries（央行购金等低频宏观事件靠关键词才抓得到）
    if (set(holdings) | set(watching)) & _GOLD_PROXY_SYMBOLS:
        queries.extend(_GOLD_STANDING_QUERIES)

    return {
        "holdings": holdings,
        "watching": watching,
        "queries": list(dict.fromkeys(queries)),  # 去重保序
    }


# #153②：RSS 泛头条预过滤的 symbol 别名表——RSS feed 不分 symbol 全量入库时，
# 用廉价子串匹配拦在 LLM 归一化之前。别名覆盖常见英文/中文写法；未知 symbol
# 回退用 ticker 主干（"GC=F"→"gc=f" 与 "gc"）。macro 关键词保泛市场事件不被误杀。
_SYMBOL_ALIASES: Dict[str, List[str]] = {
    "GC=F": ["gold", "xau", "bullion", "黄金", "金价"],
    "510300.SS": ["csi 300", "csi300", "沪深300", "沪深 300", "a-share", "a股",
                  "china stock", "chinese stock", "chinese equit", "shanghai composite"],
    "NDQ.AX": ["nasdaq", "nasdaq 100", "ndq", "纳指", "纳斯达克", "asx"],
    "BTC-USD": ["bitcoin", "btc", "比特币"],
    "ETH-USD": ["ethereum", "eth", "以太坊"],
}
_MACRO_KEYWORDS = [
    "fed", "fomc", "rate cut", "rate hike", "interest rate", "cpi", "inflation",
    "tariff", "treasury", "recession", "pboc", "央行", "加息", "降息", "关税", "通胀",
]


def _rss_prefilter(items: List["RawNewsItem"], watched: List[str]) -> List["RawNewsItem"]:
    """只过滤泛市场 wire 条目——rss:* 与 akshare:*（ddgs/yfinance 通道本就按持仓定向）。"""
    terms: List[str] = [k.lower() for k in _MACRO_KEYWORDS]
    for sym in watched:
        terms.extend(a.lower() for a in _SYMBOL_ALIASES.get(sym, []))
        root = sym.split(".")[0].split("=")[0].lower()
        terms.extend({sym.lower(), root} if len(root) >= 2 else {sym.lower()})
    kept = []
    for it in items:
        if not it.src_name.startswith(("rss:", "akshare:")):
            kept.append(it)
            continue
        hay = f"{it.title} {it.snippet}".lower()
        if any(t in hay for t in terms):
            kept.append(it)
    return kept


def run(
    *,
    dry_run: bool = False,
    custom_rss_feeds: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """job entry。返回汇总 dict。"""
    if os.getenv("INVEST_EVENT_DRY_RUN"):
        dry_run = True

    ctx = _load_user_context()
    watched = list(set(ctx["holdings"]) | set(ctx["watching"]))
    log.info(f"[event_watch] watched={watched}, queries={len(ctx['queries'])}")

    # load_feeds = 包内默认 + 用户级额外源（INVEST_HOME/rss_feeds.yml，
    # MCP add_news_source 管理）——加过的源自动进抓取，无需改配置
    rss_feeds = custom_rss_feeds if custom_rss_feeds is not None else load_feeds()

    from openinvest.core.config import load_config
    cfg = load_config()

    # A 股 symbol 自动激活中文快讯源（akshare）——海外用户零调用（#153）
    has_cn = any(sym.upper().endswith((".SS", ".SZ", ".BJ")) for sym in watched)
    raw_items = fetch_all(
        queries=ctx["queries"],
        symbols=watched,
        rss_feeds=rss_feeds,
        max_per_source=cfg.event.max_per_source,
        cn_wire=has_cn,
    )
    log.info(f"[event_watch] fetched {len(raw_items)} raw items")
    if cfg.event.rss_prefilter_enabled:
        before = len(raw_items)
        raw_items = _rss_prefilter(raw_items, watched)
        log.info(f"[event_watch] rss 预过滤: {before} → {len(raw_items)}"
                 f"（拦下 {before - len(raw_items)} 条与持仓/macro 无关的泛头条）")
    if not raw_items:
        return {"status": "ok", "fetched": 0, "new_events": 0, "triggered": 0}

    # 入库前先按 url 跳过 sources 已见的（省 LLM token）
    store = EventStore(embedding_dim=DEFAULT_DIM)
    unseen_items = [it for it in raw_items if not store.is_seen_url(it.url)]
    log.info(f"[event_watch] unseen={len(unseen_items)} / total={len(raw_items)}")
    if not unseen_items:
        return {"status": "ok", "fetched": len(raw_items), "new_events": 0, "triggered": 0}

    normalized: List[NormalizedEvent] = normalize(unseen_items)
    log.info(f"[event_watch] normalized {len(normalized)} events")

    # 入库 + 收集新事件
    new_events: List[Dict[str, Any]] = []
    for ne in normalized:
        was_new, eid = store.upsert_event(ne.event, embedding=ne.embedding)
        if ne.raw_item:
            store.add_source(
                eid,
                src_name=ne.raw_item.src_name,
                url=ne.raw_item.url,
                title=ne.raw_item.title,
                snippet=ne.raw_item.snippet,
                fetched_at=ne.raw_item.fetched_at,
            )
        if was_new:
            new_events.append({**ne.event, "event_id": eid})

    # 闸 + 冷却/日上限 + 触发 + 报警：与 agent 投喂门共用（services/event_trigger）。
    # 事件已入库，触发环节出错只记日志，不能让 job 失败把入库成果一起算成失败
    try:
        trig = trigger_for_new_events(
            new_events, store=store, watched=ctx["holdings"] + ctx["watching"],
            targets=ctx["watching"], dry_run=dry_run,
        )
    except Exception as e:
        log.warning(f"[event_watch] 触发闸失败（事件已入库）: {type(e).__name__}: {e}")
        trig = {"triggered": 0, "affected_symbols": [], "committee_symbols": [],
                "committee_task_id": None, "trigger_error": f"{type(e).__name__}: {e}"}
    log.info(f"[event_watch] triggerable={trig['triggered']} committee={trig['committee_symbols']}")

    return {
        "status": "ok",
        "fetched": len(raw_items),
        "new_events": len(normalized),
        **trig,
        "dry_run": dry_run,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只入库，不发邮件 / 不触委员会")
    parser.add_argument("--recall", metavar="SYMBOL", help="测试 event_store.recall 召回（不抓新源）")
    args = parser.parse_args()

    if args.recall:
        store = EventStore(embedding_dim=DEFAULT_DIM)
        events = store.recall(args.recall)
        import json
        print(json.dumps(events, ensure_ascii=False, indent=2, default=str))
        return 0

    out = run(dry_run=args.dry_run)
    import json
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
