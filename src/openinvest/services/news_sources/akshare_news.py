"""中文快讯 wire 适配器（akshare，#153 A股信息盲区）。

激活条件：event_watch 检测到 watched 含 A 股 symbol（.SS/.SZ/.BJ）才拉——
海外用户零调用零成本，不违反"任意资产零配置"的产品原则。

默认两源（2026-07-07 本机实测均可达）：
- akshare:em_global   东方财富全球快讯（标题/摘要/真实链接，~200 条）
- akshare:sina_7x24   新浪财经 7×24（纯内容流，无 URL——用内容哈希做稳定去重键）

财联社电报（stock_info_global_cls）从境外 VPS 实测超时，刻意不进默认；
境内部署想加，照 _fetch_em 的样子添三行即可。

akshare 爬公开接口，上游改版会断——每源独立 try/except 静默降级，
与 fetch_all 的"任一源失败不影响其他源"约定一致。
"""
from __future__ import annotations

import hashlib
import logging
from typing import List

from openinvest.services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

_TZ_SUFFIX = "+08:00"  # akshare 快讯时间为北京时间


def _iso(ts: str) -> str:
    ts = (ts or "").strip().replace(" ", "T")
    return ts + _TZ_SUFFIX if ts and "+" not in ts else ts


def _fetch_em(max_items: int) -> List[RawNewsItem]:
    import akshare as ak
    df = ak.stock_info_global_em()
    items = []
    for _, row in df.head(max_items).iterrows():
        items.append(RawNewsItem(
            src_name="akshare:em_global",
            title=str(row.get("标题") or "").strip(),
            url=str(row.get("链接") or "").strip(),
            snippet=str(row.get("摘要") or "").strip(),
            published_at=_iso(str(row.get("发布时间") or "")),
        ))
    return [it for it in items if it.title and it.url]


def _fetch_sina(max_items: int) -> List[RawNewsItem]:
    import akshare as ak
    df = ak.stock_info_global_sina()
    items = []
    for _, row in df.head(max_items).iterrows():
        content = str(row.get("内容") or "").strip()
        if not content:
            continue
        # 无 URL 的纯快讯流：内容哈希做伪 URL——is_seen_url 去重键必须稳定
        pseudo = "akshare://sina724/" + hashlib.sha256(content.encode()).hexdigest()[:16]
        items.append(RawNewsItem(
            src_name="akshare:sina_7x24",
            title=content[:80],
            url=pseudo,
            snippet=content,
            published_at=_iso(str(row.get("时间") or "")),
        ))
    return items


def fetch_cn_wire(max_items: int = 20) -> List[RawNewsItem]:
    """拉全部中文快讯源。任一源失败 log + 跳过，绝不抛。"""
    out: List[RawNewsItem] = []
    for name, fn in (("em_global", _fetch_em), ("sina_7x24", _fetch_sina)):
        try:
            got = fn(max_items)
            out.extend(got)
            log.info(f"[akshare:{name}] {len(got)} 条")
        except Exception as e:  # noqa: BLE001
            log.warning(f"[akshare:{name}] 失败（上游接口变动/网络）: {type(e).__name__}: {str(e)[:80]}")
    return out
