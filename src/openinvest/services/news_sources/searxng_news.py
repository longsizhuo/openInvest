"""searxng news 适配器 —— 自托管元搜索包成 RawNewsItem

价值定位（vs ddgs_news）：
- searxng 是自托管元搜索（聚合 Bing/Google/百度 等引擎的 news 类目），没有
  DDGS 那种被上游封锁/限流的单点风险，中文财经媒体覆盖也明显更好
- 全程内网调用（本机容器），零外部 API 成本、零外部限流

未配置 SEARXNG_URL 时整个源静默关闭——fork 用户没有 searxng 也零影响。
"""
from __future__ import annotations

import logging
import os
from typing import List
from urllib.parse import urlparse

import requests

from openinvest.services.news_sources import RawNewsItem

log = logging.getLogger(__name__)


def _normalize_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def searxng_base_url() -> str:
    """SEARXNG_URL 为空 = 源关闭（fetch_all 据此决定是否注册任务）。"""
    return os.getenv("SEARXNG_URL", "").strip().rstrip("/")


def fetch_searxng_news(
    query: str,
    *,
    max_results: int = 20,
    time_range: str = "",
) -> List[RawNewsItem]:
    """searxng news 类目搜索 → RawNewsItem 列表。

    time_range 默认不加：实测 news 类目引擎对 day 粒度支持极差（day=0 条 /
    week=6 条 / 不过滤=25 条）。新旧去重交给事件管道现有的 URL/claim 去重，
    与 RSS 源（feed 里同样带旧条目）同一套处理。
    任何异常 catch 后返回 []，上层 fetch_all 的失败隔离不受影响。
    """
    base = searxng_base_url()
    if not base:
        return []
    params = {"q": query, "format": "json", "categories": "news"}
    if time_range:
        params["time_range"] = time_range
    try:
        resp = requests.get(
            f"{base}/search",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
    except Exception as e:
        log.warning(f"searxng query {query!r} 失败: {e}")
        return []

    items: List[RawNewsItem] = []
    for r in results[:max_results]:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        domain = _normalize_domain(url)
        items.append(
            RawNewsItem(
                src_name=f"searxng:{domain}",
                title=title,
                url=url,
                snippet=_trim(r.get("content") or "", 260),
                published_at=(r.get("publishedDate") or "").strip() or None,
                raw_meta={
                    "query": query,
                    "domain": domain,
                    "engine": r.get("engine"),
                },
            )
        )
    return items


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."
