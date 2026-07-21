"""RSS feed 适配器 —— feedparser 解析免费财经 RSS

默认源在 rss_feeds.yml，env 可覆盖 INVEST_RSS_FEEDS_YML 指向自定义文件。

为什么 RSS 而不是 API：
- Reuters/BBC/FT/财新 都没免费 API key 那条路，但 RSS 是 public + 稳定
- 拿到的是结构化 entry（title/link/summary/published），不用抓 HTML
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from openinvest.services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

# 默认 yml 在同目录
_DEFAULT_YML = Path(__file__).parent / "rss_feeds.yml"

# 用户级额外源上限——add_news_source 开放给顾问模式群聊，无上限等于把
# normalize 的 LLM 账单交给陌生人
MAX_EXTRA_FEEDS = 30


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
    """加载默认 RSS feed 列表 (name + url)。

    优先级：显式 yml_path > env `INVEST_RSS_FEEDS_YML`（整体替换默认清单）>
    包内 rss_feeds.yml。env 此前只写在注释里没实现，现在是真的。
    """
    env_p = os.getenv("INVEST_RSS_FEEDS_YML", "").strip()
    p = Path(yml_path) if yml_path else (Path(env_p) if env_p else _DEFAULT_YML)
    if not p.exists():
        log.warning(f"RSS feed yml 不存在: {p}")
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", []) or []


# ------------------------------------------------------------------
# 用户级额外源（INVEST_HOME/rss_feeds.yml）—— MCP/CLI news_sources 管理
# ------------------------------------------------------------------

def _extra_yml() -> Path:
    from openinvest import paths
    return paths.INVEST_ROOT / "rss_feeds.yml"


def load_extra_feeds() -> List[Dict[str, str]]:
    """用户/群聊自助添加的额外源。文件不存在 = 没加过，返回空。"""
    p = _extra_yml()
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("feeds", []) or []
    except Exception as e:  # noqa: BLE001  手编坏 yml 不该炸掉整条抓取链
        log.warning(f"额外源 yml 解析失败，忽略: {p}: {e}")
        return []


def load_feeds() -> List[Dict[str, str]]:
    """默认源 + 用户级额外源，按 url 去重（默认源优先）。抓取方统一用这个。"""
    feeds = list(load_default_feeds())
    seen = {f.get("url") for f in feeds}
    for f in load_extra_feeds():
        if f.get("url") not in seen:
            feeds.append(f)
            seen.add(f.get("url"))
    return feeds


def _write_extra_feeds(feeds: List[Dict[str, str]]) -> None:
    p = _extra_yml()
    tmp = p.with_suffix(".yml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"feeds": feeds}, f, allow_unicode=True, sort_keys=False)
    tmp.replace(p)


def add_extra_feed(name: str, url: str) -> Dict[str, object]:
    """加一个用户级 RSS/Atom 源。返回 {feed, probe_items}；不合法抛 ValueError。

    守护（顾问模式下这是暴露给群聊的写入口）：
    - name 规整为 [a-z0-9_]（与默认清单同一约定）
    - url 必须 http(s)，且 live probe 能解析出至少 1 条 entry（挡"随手贴个网页"）
    - 上限 MAX_EXTRA_FEEDS；url 与默认/已有源重复 = 幂等返回已有条目
    """
    name = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower()).strip("_")
    url = (url or "").strip()
    if not name:
        raise ValueError("name 不能为空（规整后仅剩 [a-z0-9_]）")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"url 必须是 http(s) RSS/Atom 地址: {url!r}")

    extras = load_extra_feeds()
    for f in load_default_feeds() + extras:
        if f.get("url") == url:
            return {"feed": f, "probe_items": None, "already_exists": True}
    if any(f.get("name") == name for f in load_default_feeds() + extras):
        raise ValueError(f"源名 {name!r} 已被占用，换一个 name")
    if len(extras) >= MAX_EXTRA_FEEDS:
        raise ValueError(f"额外源已达上限 {MAX_EXTRA_FEEDS} 个，先 remove 再 add")

    probe = fetch_rss(name, url, max_items=3)
    if not probe:
        raise ValueError(f"probe 失败：{url} 解析不出任何 RSS/Atom entry（不是 feed 或暂时抓不到）")

    feed = {"name": name, "url": url}
    _write_extra_feeds(extras + [feed])
    return {"feed": feed, "probe_items": len(probe), "already_exists": False}


def remove_extra_feed(key: str) -> bool:
    """按 name 或 url 删一个额外源（只动用户级清单，默认源不可删）。"""
    key = (key or "").strip()
    extras = load_extra_feeds()
    kept = [f for f in extras if f.get("name") != key and f.get("url") != key]
    if len(kept) == len(extras):
        return False
    _write_extra_feeds(kept)
    return True


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


def _strip_html(s: str) -> str:
    """粗暴去 HTML tag —— RSS summary 经常带 <p>/<a>"""
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()
