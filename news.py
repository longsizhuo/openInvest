from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import re
import time
from ddgs import DDGS
import trafilatura



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
    org_words = len(re.findall(r"\b(Fed|Federal Reserve|SEC|Treasury|ECB|RBA|earnings|CPI|GDP|filing|guidance)\b", text, re.I))
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


def _extract_main_text(url: str, timeout: int = 15) -> str:
    """
    尽量抽取正文。没有 trafilatura 时会退化为返回空字符串。
    """
    if not trafilatura:
        return ""

    try:
        downloaded = trafilatura.fetch_url(url, timeout=timeout)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
        text = re.sub(r"\s+\n", "\n", text).strip()
        return text
    except Exception:
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


# -----------------------------
# 对外主函数：你只需要 import 调用它
# -----------------------------
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
    返回结构：
    {
      "query": str,
      "trusted": [ {title,url,domain,date,score,scores,flags,summary} ],
      "review":  [...],
      "filtered": [...]
    }
    """
    if DDGS is None:
        raise RuntimeError("duckduckgo_search 未安装或不可用：请先 pip install duckduckgo_search")

    # 召回策略：中性主题 + 事实词，避免只搜 risk 造成偏差
    recall_query = f'{topic_query} (earnings OR CPI OR GDP OR Fed OR RBA OR guidance OR filings OR statement)'

    raw_items: List[NewsItem] = []
    try:
        with DDGS() as ddgs:
            results = ddgs.news(recall_query, region=region, safesearch=safesearch, max_results=max_results)
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
        # 如果搜索失败（例如 No results found），我们捕获异常并返回空结果，而不是让程序崩溃
        print(f"Warning: News search failed or returned no results: {e}")

    items = _dedup(raw_items)

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
            # summary 用 snippet 优先；没有正文抽取时也能看
            "summary": it.snippet or _safe_trim(it.text, 300),
        }

        # 分桶阈值：你可以后续按效果改
        if score >= 0.78 and not any(f in flags for f in ["source_low", "clickbait_high", "fear_high_evidence_low"]):
            trusted.append(record)
        elif any(f in flags for f in ["source_low", "clickbait_high", "fear_high_evidence_low"]):
            filtered.append(record)
        else:
            review.append(record)

    return {
        "query": recall_query,
        "trusted": trusted,
        "review": review,
        "filtered": filtered,
    }
