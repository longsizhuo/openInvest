"""DDGS news 适配器 —— DDGS 搜索包成 RawNewsItem 形式

不复制 _truth_score 那套打分（事件归一化阶段交给 flash LLM 抽 source_reliability 字段），
只做 DDGS 搜索（标题+摘要）。正文抓取链路已删（#153）：生产始终不抓正文
（event_watch 显式关、events.db 无正文列），trafilatura/readability 依赖随之出局。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from openinvest.services.news_sources import RawNewsItem

log = logging.getLogger(__name__)


def _normalize_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def fetch_ddgs_news(
    query: str,
    *,
    max_results: int = 20,
    region: str = "wt-wt",
    safesearch: str = "off",
) -> List[RawNewsItem]:
    """DDGS news 搜索 → RawNewsItem 列表。

    任何异常 catch 后返回 []，上层 fetch_all 已经 wrap 了，再 catch 也只是降噪。
    """
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("ddgs 未安装，跳过 ddgs_news")
        return []

    items: List[RawNewsItem] = []
    try:
        with DDGS() as ddgs:
            results = ddgs.news(
                query,
                region=region,
                safesearch=safesearch,
                max_results=max_results,
            ) or []
    except Exception as e:
        log.warning(f"DDGS query {query!r} 失败: {e}")
        return []

    for r in results:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        domain = _normalize_domain(url)
        published = (r.get("date") or "").strip() or None
        item = RawNewsItem(
            src_name=f"ddgs:{domain}",
            title=title,
            url=url,
            snippet=_trim(r.get("body") or r.get("snippet") or "", 260),
            published_at=published,
            raw_meta={"query": query, "domain": domain},
        )
        items.append(item)
    return items


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


