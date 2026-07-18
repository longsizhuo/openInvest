"""jobs/event_watch —— 第一层：盘中事件感知 + 邮件通知 + 触发委员会

每 30m cron 跑一次：
1. 拉用户上下文（holdings ∪ target_assets）
2. 构 query → fetch_all 多源新闻
3. event_normalizer.normalize → 结构化事件
4. dedup（同 url 跳过）+ event_store.upsert_event
5. 对 newly_inserted 中 severity ≥ mid 且影响到关注 symbol 的：
   → POST /api/committee/run（触发委员会重跑）
   → send_event_alert（digest 邮件）

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
from openinvest.services.event_notifier import send_event_alert
from openinvest.services.news_sources import fetch_all
from openinvest.services.news_sources.rss_feed import load_default_feeds

log = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 1, "mid": 2, "high": 3}

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


def _holdings_snapshot(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """给邮件正文用：每个受影响 symbol 当前 units / 现价 / pnl

    现价必须经 get_quote(holding) 拿——它按 holding 的 proxy_kind/cost_currency
    把行情换算成与 avg_cost 同币种、同单位的价格（如黄金积存金 GC=F USD/oz
    经 /31.1035*USDCNY 反推成 CNY/克）。早期直接用 get_history_data 拿原始
    GC=F 价（USD/oz≈4523），跟 CNY/克 成本（≈1008）相除，把真实浮亏 -1% 算成
    了 +348%——币种/单位错配 bug，统一走 get_quote 后根除。
    """
    try:
        from openinvest.core.portfolio_manager import PortfolioManager
        from openinvest.utils.quotes import get_quote
        pm = PortfolioManager()
    except Exception:
        return {}

    snap: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        h = pm.holdings.find(sym)
        if not h:
            continue
        units = float(h.get("units", 0) or 0)
        avg_cost = float(h.get("avg_cost", 0) or 0)
        try:
            quote = get_quote(h)
        except Exception:
            quote = None
        entry: Dict[str, Any] = {"units": units}
        if quote is not None:
            # price 与 avg_cost 现在保证同币种同单位，相除才有意义
            entry["price"] = round(quote.price, 2)
            entry["currency"] = quote.currency
            # 追踪仓 / 无成本不算 P&L，只报现价（对齐 web_api._build_holding_v2）
            if not h.get("is_tracking_only") and units > 0:
                entry["mv"] = quote.price * units
                if avg_cost > 0:
                    entry["pnl_pct"] = (quote.price / avg_cost) - 1.0
        snap[sym] = entry
    return snap


def _trigger_committee(symbols: List[str], event_ids: List[str]) -> Optional[str]:
    """调本机 web_api /api/committee/run 触发委员会重跑。返回 task_id"""
    if not symbols:
        return None
    import requests
    from openinvest.core.config import load_config
    base = os.getenv("INVEST_EVENT_API_URL", "http://127.0.0.1:8765")
    # hub 开了 INVEST_API_TOKEN 时 loopback 也要带（#106 起 token 全域强制）
    _tok = os.getenv("INVEST_API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {_tok}"} if _tok else {}
    try:
        r = requests.post(
            f"{base.rstrip('/')}/api/committee/run",
            headers=headers,
            json={
                "symbols": symbols,
                "max_debate_rounds": load_config().event.max_rounds,
                "note": f"triggered by event_watch event_ids={','.join(event_ids[:4])}",
                "event_ids": event_ids,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("task_id")
    except Exception as e:
        log.warning(f"trigger committee 失败: {type(e).__name__}: {e}")
        return None


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

    rss_feeds = custom_rss_feeds if custom_rss_feeds is not None else load_default_feeds()

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
    min_sev = cfg.event.min_severity
    min_sev_rank = _SEVERITY_RANK.get(min_sev, 2)
    watched_set = {s.lower() for s in watched}

    triggerable_events: List[Dict[str, Any]] = []
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
        if not was_new:
            continue

        ev = ne.event
        sev_rank = _SEVERITY_RANK.get(ev["severity"], 1)
        if sev_rank < min_sev_rank:
            continue
        if ev["stance"] == "neutral":
            continue

        affected_lower = {s.lower() for s in (ev["affected_symbols"] or [])}
        if not (affected_lower & watched_set):
            continue

        # 这条事件值得触发
        ev_for_email = dict(ev)
        ev_for_email["event_id"] = eid
        ev_for_email["sources"] = store.get_sources(eid)
        triggerable_events.append(ev_for_email)

    log.info(f"[event_watch] triggerable={len(triggerable_events)}")

    if not triggerable_events:
        return {
            "status": "ok",
            "fetched": len(raw_items),
            "new_events": sum(1 for _ in normalized),
            "triggered": 0,
        }

    # 受影响 symbols 合并去重，只对真持仓 / 关注列表跑委员会。
    # 大小写不敏感匹配 + 映射回 canonical 写法——triggerable 判定（上方）就是
    # .lower() 交集，这里若用大小写敏感 `s in list`，LLM 归一化吐出 "ndq.ax"
    # 时邮件会发但委员会静默不触发（CR 命中；events.db 实查暂无变体，防御性守卫）。
    _canonical = {s.lower(): s for s in (ctx["holdings"] + ctx["watching"])}
    affected_syms = sorted({
        _canonical[s.lower()]
        for ev in triggerable_events for s in (ev["affected_symbols"] or [])
        if s.lower() in _canonical
    })

    task_id = None
    if not dry_run:
        task_id = _trigger_committee(
            symbols=affected_syms,
            event_ids=[ev["event_id"] for ev in triggerable_events],
        )
        if task_id:
            for ev in triggerable_events:
                store.mark_committee_task(ev["event_id"], task_id)

        try:
            send_event_alert(
                triggerable_events,
                committee_task_id=task_id,
                holdings_snapshot=_holdings_snapshot(affected_syms),
            )
        except Exception as e:
            log.warning(f"send_event_alert 失败: {type(e).__name__}: {e}")

    return {
        "status": "ok",
        "fetched": len(raw_items),
        "new_events": len(normalized),
        "triggered": len(triggerable_events),
        "committee_task_id": task_id,
        "affected_symbols": affected_syms,
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
