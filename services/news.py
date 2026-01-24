from __future__ import annotations

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


_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        # 完整的 Headers (模拟现代 Chrome)
        _SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.google.com/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })
        # 增强重试策略: backoff_factor=2 (等待 2s, 4s, 8s)
        retries = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        _SESSION.mount('http://', adapter)
        _SESSION.mount('https://', adapter)
    return _SESSION


def _extract_main_text(url: str, timeout: int = 15) -> str:
    if not trafilatura:
        return ""

    try:
        session = _get_session()
        response = session.get(url, timeout=timeout)
        response.raise_for_status()

        # 确保编码正确
        response.encoding = response.apparent_encoding
        html = response.text

        # 策略 1: Trafilatura (Precision)
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
        if text:
            print(f"  [Extraction] Success using: Trafilatura (Precision)")
            return re.sub(r"\s+\n", "\n", text).strip()

        # 策略 2: Trafilatura (Recall / Default)
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True)
        if text:
            print(f"  [Extraction] Success using: Trafilatura (Recall)")
            return re.sub(r"\s+\n", "\n", text).strip()

        # 策略 3: Readability 兜底
        if Document:
            try:
                doc = Document(html)
                # 使用 trafilatura 清洗 readability 提取的 summary HTML
                summary_html = doc.summary()
                text = trafilatura.extract(summary_html, include_comments=False, include_tables=False)
                if text:
                    print(f"  [Extraction] Success using: Readability + Trafilatura")
                    return re.sub(r"\s+\n", "\n", text).strip()
            except Exception:
                pass

        return ""
    except Exception as e:
        # 如果下载失败，返回空，保留标题供 Review
        print(f"Error fetching {url}: {e}")
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

    print(f"DEBUG: Executing DDGS query: {final_query}")

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
        print(f"Error: Search failed for query '{final_query}': {e}")
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
