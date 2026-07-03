"""DDG 财经新闻搜索 + trafilatura/readability 抓正文 + 反标题党规则评分

现状（2026-06-11 待办5评审更正——此前的"孤儿代码警告"已过时）
=================================================================
- **ddgs 宏观新闻源是活的，但不走本模块**：production 路径是
  `services/news_sources/ddgs_news.py` + `jobs/event_watch.py`（每 30min cron
  把持仓/macro_tags 构造的 queries 传给 fetch_all → fetch_ddgs_news →
  event_normalizer 归一化 → event_store → event_brief 注入 Macro/Risk/Gemini）。
  "系统抓不到宏观新闻"的担忧不成立。
- 本模块只有 `get_real_finance_news` + truth-score 管线是死代码（0 caller）。
- ⚠️ **`_extract_main_text` 被 `news_sources/ddgs_news.py` lazy import 复用**
  （extract_fulltext=True 时）——cleanup 删本文件会打断那条路径，别删。

历史：原 caller 是 capabilities/sdk_agent.py 的 `finance_news` langchain tool，被
commit 934ff7a (2026-05-08, "chore(deps): 删 langchain") 连带移除。

恢复 finance_news tool 的方案经 2026-06 待办5 评审**搁置**，原因：
- 宏观新闻覆盖已由事件层完成（ddgs queries + RSS + yfinance news 三源）
- 给 Macro 一个生肉 DDG 搜索 tool = 旁路事件层的归一化/去重/severity 过滤
  （anti-noise 管线），且在委员会跑动中引入非确定性 IO
- Macro 已通过 run_macro_view(event_brief=...) 看到策划后的事件
如未来确有按需搜索需求，参照 capabilities/tools.py 的 TOOL_DEFINITIONS/_impl_* 模式，
且只给 Macro 角色。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import trafilatura
from ddgs import DDGS
from readability import Document
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


# -----------------------------
# 数据结构
# -----------------------------
@dataclass
class NewsItem:
    title: str
    url: str
    domain: str
    date: str
    snippet: str = ""
    text: str = ""
    score: float = 0.0
    scores: Optional[Dict[str, float]] = None
    flags: Optional[List[str]] = None


# -----------------------------
# 规则：标题党 & 恐惧营销
# -----------------------------
CLICKBAIT_PATTERNS = [
    r"\bwon't believe\b",
    r"\bshocking\b",
    r"\bwhat happens next\b",
    r"\bthis is why\b",
    r"\bhere's how\b",
    r"\bthe truth\b",
    r"\bmassive\b",
    r"\bjust\b",
    r"\bsecret\b",
    r"\brevealed\b",
    r"\bnever\b",
    r"\beverything you need to know\b",
    r"\bexplodes?\b",
    r"\bplunges?\b",
    r"\bskyrocket(s|ed)?\b",
    r"\bmeltdown\b",
    r"\bcrash\b",
    r"\bpanic\b",
]

FEAR_WORDS = [
    "panic",
    "crash",
    "collapse",
    "meltdown",
    "recession",
    "bloodbath",
    "doom",
    "catastrophe",
    "fear",
    "plunge",
    "wipeout",
]


def _normalize_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _safe_trim(s: str, n: int = 400) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "..."


def _clickbait_score(title: str) -> float:
    t = (title or "").lower()
    score = 0.0

    # 夸张标点 / 结构
    if "!" in title:
        score += 0.20
    if "?" in title:
        score += 0.10
    if re.search(r"\b\d+\b", title):  # “7 reasons …”
        score += 0.10
    if re.search(r"\b(you|your)\b", t):
        score += 0.05

    # 关键词命中
    for p in CLICKBAIT_PATTERNS:
        if re.search(p, t):
            score += 0.15

    return min(1.0, score)


def _fear_proxy_score(title: str, text: str) -> float:
    # 不上模型时的 proxy：用于把最极端的先降权
    t = (title or "").lower()
    body = (text or "").lower()
    s = 0.0
    for w in FEAR_WORDS:
        if w in t:
            s += 0.10
        if w in body:
            s += 0.03

    if "!" in (title or ""):
        s += 0.05

    return min(1.0, s)


def _evidence_density(text: str) -> float:
    """
    粗略“事实密度”：数据/机构/时间线痕迹越多越像事实型报道。
    """
    text = (text or "").strip()
    if len(text) < 400:
        return 0.10

    numbers = len(re.findall(r"\b\d+(\.\d+)?%?\b", text))
    org_words = len(
        re.findall(r"\b(Fed|Federal Reserve|SEC|Treasury|ECB|RBA|earnings|CPI|GDP|filing|guidance)\b", text, re.I))
    date_words = len(re.findall(r"\b(202\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", text, re.I))

    score = 0.0
    score += min(0.50, numbers / 80.0)
    score += min(0.30, org_words / 20.0)
    score += min(0.20, date_words / 20.0)
    return min(1.0, score)


def _source_quality(domain: str, whitelist: Optional[List[str]], blacklist: Optional[List[str]]) -> float:
    if not domain:
        return 0.3
    if whitelist and domain in whitelist:
        return 1.0
    if blacklist and domain in blacklist:
        return 0.0
    # 默认中性
    return 0.60


from curl_cffi import requests as cffi_requests

_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        # Use curl_cffi for browser impersonation (impersonate="chrome")
        # This bypasses TLS fingerprinting and reduces 429 errors
        _SESSION = cffi_requests.Session(impersonate="chrome")
        _SESSION.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        })
    return _SESSION


def _extract_main_text(url: str, timeout: int = 15) -> str:
    if not trafilatura:
        return ""

    try:
        session = _get_session()
        # curl_cffi handles headers and TLS fingerprint automatically
        response = session.get(url, timeout=timeout)
        response.raise_for_status()

        html = response.text

        # 策略 1: Trafilatura (Precision)
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
        if text:
            log.info("[Extraction] Success using: Trafilatura (Precision)")
            return re.sub(r"\s+\n", "\n", text).strip()

        # 策略 2: Trafilatura (Recall / Default)
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True)
        if text:
            log.info("[Extraction] Success using: Trafilatura (Recall)")
            return re.sub(r"\s+\n", "\n", text).strip()

        # 策略 3: Readability 兜底
        if Document:
            try:
                doc = Document(html)
                summary_html = doc.summary()
                text = trafilatura.extract(summary_html, include_comments=False, include_tables=False)
                if text:
                    log.info("[Extraction] Success using: Readability + Trafilatura")
                    return re.sub(r"\s+\n", "\n", text).strip()
            except Exception:
                pass

        return ""
    except Exception as e:
        log.warning("Error fetching %s: %s", url, e)
        return ""


def _truth_score(
        title: str,
        domain: str,
        text: str,
        whitelist: Optional[List[str]],
        blacklist: Optional[List[str]],
) -> Tuple[float, Dict[str, float], List[str]]:
    flags: List[str] = []

    cb = _clickbait_score(title)
    ev = _evidence_density(text)
    fear = _fear_proxy_score(title, text)
    src = _source_quality(domain, whitelist, blacklist)

    # 解释性 flags，便于你迭代阈值
    if cb >= 0.60:
        flags.append("clickbait_high")
    if fear >= 0.60 and ev <= 0.30:
        flags.append("fear_high_evidence_low")
    if src <= 0.30:
        flags.append("source_low")
    if not text:
        flags.append("no_fulltext")

    # 可解释加权：你后续可以换成学习到的权重
    score = (
            0.40 * src
            + 0.35 * ev
            + 0.15 * (1.0 - cb)
            + 0.10 * (1.0 - fear)
    )
    score = max(0.0, min(1.0, score))

    scores = {"source": src, "evidence": ev, "clickbait": cb, "fear": fear}
    return score, scores, flags


def _dedup(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    out: List[NewsItem] = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        out.append(it)
    return out


def get_real_finance_news(
        topic_query: str,
        *,
        max_results: int = 25,
        whitelist_domains: Optional[List[str]] = None,
        blacklist_domains: Optional[List[str]] = None,
        region: str = "wt-wt",
        safesearch: str = "off",
        extract_fulltext: bool = True,
        sleep_sec: float = 0.0,
) -> Dict[str, Any]:
    """
    优化策略：
    1. 放弃 DDGS 不支持的复杂布尔查询（(A OR B)），避免 "No results found"。
    2. 使用 topic_query 进行宽泛召回。
    3. 依赖本地的 _evidence_density 进行关键词权重排序。
    """
    if DDGS is None:
        raise RuntimeError("duckduckgo_search 未安装或不可用：请先 pip install duckduckgo_search")

    # -------------------------------------------------------
    # 1. 构造查询：不再尝试复杂的括号语法
    # -------------------------------------------------------
    # 如果你想稍微增加一点金融相关性，可以在后面拼一个通用的词，比如 "news" 或 "finance"
    # 但实际上直接搜 topic_query 效果往往最好，因为 DDG 的 news tab 本身就是新闻。
    final_query = topic_query

    log.debug("Executing DDGS query: %s", final_query)

    raw_items: List[NewsItem] = []

    # -------------------------------------------------------
    # 2. 执行搜索
    # -------------------------------------------------------
    try:
        with DDGS() as ddgs:
            results = ddgs.news(
                final_query,
                region=region,
                safesearch=safesearch,
                max_results=max_results
            )

            # 处理结果
            if results:
                for r in results:
                    url = (r.get("url") or "").strip()
                    title = (r.get("title") or "").strip()
                    if not url or not title:
                        continue

                    raw_items.append(
                        NewsItem(
                            title=title,
                            url=url,
                            domain=_normalize_domain(url),
                            date=(r.get("date") or "").strip(),
                            snippet=_safe_trim(r.get("body") or r.get("snippet") or "", 260),
                        )
                    )
    except Exception as e:
        log.warning("Search failed for query '%s': %s", final_query, e)
        # 如果连基础查询都挂了，那就返回空结构
        return {"query": final_query, "trusted": [], "review": [], "filtered": []}

    items = _dedup(raw_items)

    # -------------------------------------------------------
    # 3. 本地评分与分桶 (这是你的强项，依靠这里来区分质量)
    # -------------------------------------------------------
    trusted: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []

    for it in items:
        if sleep_sec > 0:
            time.sleep(sleep_sec)

        if extract_fulltext and trafilatura:
            it.text = _extract_main_text(it.url)
        else:
            it.text = ""

        score, scores, flags = _truth_score(it.title, it.domain, it.text, whitelist_domains, blacklist_domains)
        it.score, it.scores, it.flags = score, scores, flags

        record = {
            "title": it.title,
            "url": it.url,
            "domain": it.domain,
            "date": it.date,
            "score": round(score, 3),
            "scores": {k: round(v, 3) for k, v in (scores or {}).items()},
            "flags": flags,
            "summary": it.snippet or _safe_trim(it.text, 300),
        }

        # 这里的逻辑不变
        if score >= 0.78 and not any(f in flags for f in ["source_low", "clickbait_high", "fear_high_evidence_low"]):
            trusted.append(record)
        elif any(f in flags for f in ["source_low", "clickbait_high", "fear_high_evidence_low"]):
            filtered.append(record)
        else:
            review.append(record)

    return {
        "query": final_query,
        "trusted": trusted,
        "review": review,
        "filtered": filtered,
    }
