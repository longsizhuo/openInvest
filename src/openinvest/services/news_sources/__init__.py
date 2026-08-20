"""services/news_sources —— 多源新闻抓取，事件层 fetch 阶段统一入口

五个独立源（任一失败不连坐）：
- ddgs_news.py     DuckDuckGo news（宽泛查询，覆盖 macro / sector）
- searxng_news.py  自托管 searxng 元搜索（聚合多引擎 news 类目，中文覆盖好、
                   无外部限流；SEARXNG_URL 未配置时静默关闭）
- rss_feed.py      Reuters / BBC / FT / 财新 等 RSS（高质量主流财经）
- yfinance_news.py yf.Ticker(sym).news（按 symbol 直接关联）
- akshare_news.py  东财/新浪 7×24 中文快讯（watched 含 A 股时自动开）

统一返回 RawNewsItem dataclass，给 event_normalizer 进 LLM 归一化。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class RawNewsItem:
    """归一化后的原始新闻条目，给 event_normalizer 用"""
    src_name: str                  # 'ddgs' / 'rss:reuters' / 'yfinance:NDQ.AX'
    title: str
    url: str
    snippet: str
    text: str = ""                 # 抓到的正文（可空）
    published_at: Optional[str] = None  # ISO 8601
    fetched_at: str = ""
    raw_meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_all(
    *,
    queries: Optional[List[str]] = None,
    symbols: Optional[List[str]] = None,
    rss_feeds: Optional[List[Dict[str, str]]] = None,
    max_per_source: int = 20,
    cn_wire: bool = False,
    timeout_sec: float = 30.0,
) -> List[RawNewsItem]:
    """并发拉所有源。任一源失败 log warning + 继续，不影响其他源。

    Args:
        queries:          给 ddgs_news 的关键词列表（如 ['Fed rate', 'NDQ.AX']）
        symbols:          给 yfinance_news 的 ticker 列表
        rss_feeds:        给 rss_feed 的 feed 列表 [{"name": "reuters", "url": "..."}]
        cn_wire:          拉中文快讯源（akshare 东财/新浪 7×24）——event_watch 在
                          watched 含 A 股 symbol 时自动开
        max_per_source:   每个源最多返回多少条
        timeout_sec:      单源超时
    """
    from openinvest.services.news_sources.ddgs_news import fetch_ddgs_news
    from openinvest.services.news_sources.rss_feed import fetch_rss
    from openinvest.services.news_sources.yfinance_news import fetch_yfinance_news

    tasks: List[Dict[str, Any]] = []
    if queries:
        for q in queries:
            tasks.append({
                "fn": fetch_ddgs_news,
                "kwargs": {"query": q, "max_results": max_per_source},
                "label": f"ddgs:{q[:30]}",
            })
        # searxng 与 ddgs 吃同一组 queries；未配 SEARXNG_URL 时整体跳过
        from openinvest.services.news_sources.searxng_news import (
            fetch_searxng_news,
            searxng_base_url,
        )
        if searxng_base_url():
            for q in queries:
                tasks.append({
                    "fn": fetch_searxng_news,
                    "kwargs": {"query": q, "max_results": max_per_source},
                    "label": f"searxng:{q[:30]}",
                })
    if cn_wire:
        from openinvest.services.news_sources.akshare_news import fetch_cn_wire
        tasks.append({
            "fn": fetch_cn_wire,
            "kwargs": {"max_items": max_per_source},
            "label": "akshare:cn_wire",
        })
    if symbols:
        for sym in symbols:
            tasks.append({
                "fn": fetch_yfinance_news,
                "kwargs": {"symbol": sym, "max_items": max_per_source},
                "label": f"yfinance:{sym}",
            })
    if rss_feeds:
        for feed in rss_feeds:
            tasks.append({
                "fn": fetch_rss,
                "kwargs": {"name": feed["name"], "url": feed["url"],
                           "max_items": max_per_source},
                "label": f"rss:{feed['name']}",
            })

    if not tasks:
        return []

    all_items: List[RawNewsItem] = []
    # ponytail: 故意不用 `with ThreadPoolExecutor(...)` —— 它退出时 shutdown(wait=True)
    # 会 join 卡死的 worker，下面 as_completed 的 timeout 就白设了。2026-07-30 18:00
    # 事故：akshare cn_wire（无 timeout 可传）卡死 → fetch_all 永不返回 → event_watch
    # 这个实例永不结束 → APScheduler max_instances=1 把之后每次触发全 skip，
    # 新闻哨兵静默死了 21 天（只在日志里每 30 分钟 WARNING 一次，没人看）。
    # 代价：卡死那条线程泄漏（非 daemon，进程退出靠 systemd SIGKILL 收尾）。
    # 上限：真根治要给每个源塞可用的 timeout，akshare 不暴露 → 先按泄漏处理。
    pool = ThreadPoolExecutor(max_workers=min(8, len(tasks)))
    try:
        futures = {pool.submit(t["fn"], **t["kwargs"]): t["label"] for t in tasks}
        # PR #5 Copilot CR 修复: as_completed(timeout=) 整体 wall-clock 用完后抛
        # concurrent.futures.TimeoutError，原版没 catch → 一个 slow source 拖
        # 整个 fetch_all 抛异常丢光其他源 result。改为 try/except + 把没完成的
        # 那些 future cancel 掉，已收到的 result 保留返回。
        try:
            for fut in as_completed(futures, timeout=timeout_sec):
                label = futures[fut]
                try:
                    items = fut.result()
                    all_items.extend(items)
                    log.info(f"[news_sources] {label} → {len(items)} items")
                except Exception as e:
                    log.warning(f"[news_sources] {label} 失败: {type(e).__name__}: {e}")
        except FuturesTimeoutError:
            unfinished = [futures[f] for f in futures if not f.done()]
            log.warning(
                f"[news_sources] fetch_all 超时 ({timeout_sec}s)，"
                f"未完成 sources={unfinished}；保留已收到 {len(all_items)} 条继续",
            )
    finally:
        # wait=False：不等卡死的 worker；cancel_futures=True：还没起跑的直接取消
        pool.shutdown(wait=False, cancel_futures=True)

    # 同 url 去重（保留先到的）
    seen_urls = set()
    dedup: List[RawNewsItem] = []
    for it in all_items:
        if it.url in seen_urls:
            continue
        seen_urls.add(it.url)
        dedup.append(it)
    log.info(f"[news_sources] 总计 {len(all_items)} 条 → 去重后 {len(dedup)} 条")
    return dedup
