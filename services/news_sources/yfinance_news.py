"""yfinance news 适配器 —— yf.Ticker(sym).news 按 symbol 直接关联

为什么用：
- 跟 ddgs 关键词搜索不同，yfinance 给的 news 是 Yahoo 按 ticker 关联的
  （往往含财报 / 分红 / 上调评级等强相关事件）
- 免费、零额外 quota（yfinance 已在依赖里）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)


def fetch_yfinance_news(symbol: str, *, max_items: int = 20) -> List[RawNewsItem]:
    """yfinance.Ticker(symbol).news → RawNewsItem 列表

    yfinance .news 字段（v0.2+）：
      {uuid, title, publisher, link, providerPublishTime (epoch),
       type, thumbnail, relatedTickers}
    """
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance 未安装，跳过 yfinance_news")
        return []

    try:
        ticker = yf.Ticker(symbol)
        raw = ticker.news or []
    except Exception as e:
        log.warning(f"yfinance {symbol} news 抓取失败: {e}")
        return []

    items: List[RawNewsItem] = []
    for r in raw[:max_items]:
        # yfinance 0.2.66 把字段嵌到 r["content"] 下，老版直接平铺。两边兼容
        content = r.get("content") or r
        title = (content.get("title") or "").strip()
        link = (content.get("canonicalUrl", {}).get("url")
                or content.get("link")
                or content.get("clickThroughUrl", {}).get("url")
                or "")
        if not title or not link:
            continue
        publisher = (
            (content.get("provider") or {}).get("displayName")
            or content.get("publisher")
            or "unknown"
        )
        ts = content.get("pubDate") or content.get("providerPublishTime")
        published = _to_iso(ts)
        snippet = (content.get("summary") or content.get("description") or "").strip()
        items.append(RawNewsItem(
            src_name=f"yfinance:{publisher}",
            title=title,
            url=link,
            snippet=snippet[:260],
            published_at=published,
            raw_meta={
                "symbol": symbol,
                "related_tickers": content.get("relatedTickers") or [],
            },
        ))
    return items


def _to_iso(ts) -> str:
    """yfinance ts 可能是 epoch int 或 ISO 字符串，归一成 ISO"""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    return str(ts)
