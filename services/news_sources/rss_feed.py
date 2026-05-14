"""RSS feed 适配器 —— feedparser 解析免费财经 RSS

默认源在 rss_feeds.yml，env 可覆盖 INVEST_RSS_FEEDS_YML 指向自定义文件。

为什么 RSS 而不是 API：
- Reuters/BBC/FT/财新 都没免费 API key 那条路，但 RSS 是 public + 稳定
- 拿到的是结构化 entry（title/link/summary/published），不用抓 HTML
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

# 默认 yml 在同目录
_DEFAULT_YML = Path(__file__).parent / "rss_feeds.yml"


def fetch_rss(name: str, url: str, *, max_items: int = 20) -> List[RawNewsItem]:
    """单个 RSS feed → RawNewsItem 列表"""
    try:
        import feedparser
    except ImportError:
        log.warning("feedparser 未安装，跳过 rss_feed")
        return []

    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        log.warning(f"RSS {name} parse 失败: {e}")
        return []

    items: List[RawNewsItem] = []
    for entry in (parsed.entries or [])[:max_items]:
        link = entry.get("link") or entry.get("id") or ""
        title = entry.get("title") or ""
        if not link or not title:
            continue
        snippet = entry.get("summary") or entry.get("description") or ""
        # feedparser 给出 published_parsed (time.struct_time)，转 ISO
        published = None
        if entry.get("published_parsed"):
            try:
                published = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
            except Exception:
                published = None
        items.append(RawNewsItem(
            src_name=f"rss:{name}",
            title=title.strip(),
            url=link.strip(),
            snippet=_trim(_strip_html(snippet), 260),
            published_at=published,
            raw_meta={"feed_name": name, "feed_url": url},
        ))
    return items


def load_default_feeds(yml_path: Optional[Path] = None) -> List[Dict[str, str]]:
    """加载默认 RSS feed 列表 (name + url)"""
    p = Path(yml_path) if yml_path else _DEFAULT_YML
    if not p.exists():
        log.warning(f"RSS feed yml 不存在: {p}")
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", []) or []


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


def _strip_html(s: str) -> str:
    """粗暴去 HTML tag —— RSS summary 经常带 <p>/<a>"""
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()
