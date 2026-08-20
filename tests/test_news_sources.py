"""services/news_sources 单测 —— mock 三个源的底层调用，验证统一入口 + 去重"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from openinvest.services.news_sources import RawNewsItem, fetch_all


def _stub_items(prefix: str, n: int):
    return [
        RawNewsItem(
            src_name=f"stub:{prefix}", title=f"{prefix} t{i}",
            url=f"https://{prefix}.example/{i}", snippet="x",
        )
        for i in range(n)
    ]


def test_fetch_all_runs_each_source_and_dedups_urls(monkeypatch):
    # 测试隔离：本机 .env 可能配了 SEARXNG_URL（load_dotenv 会带进进程），
    # 不清掉会注册 searxng 任务真发 HTTP，条数断言全崩
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    with patch("openinvest.services.news_sources.ddgs_news.fetch_ddgs_news") as m_ddgs, \
         patch("openinvest.services.news_sources.yfinance_news.fetch_yfinance_news") as m_yf, \
         patch("openinvest.services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_ddgs.return_value = _stub_items("ddgs", 3)
        m_yf.return_value = _stub_items("yf", 2)
        m_rss.return_value = [
            RawNewsItem(src_name="rss:r", title="t", url="https://ddgs.example/0", snippet="dup"),
            RawNewsItem(src_name="rss:r", title="t", url="https://rss.example/0", snippet="x"),
        ]
        out = fetch_all(
            queries=["foo"],
            symbols=["NDQ.AX"],
            rss_feeds=[{"name": "r", "url": "https://feed"}],
        )
    # 3 ddgs + 2 yf + 2 rss = 7 raw，1 条 url 跟 ddgs/0 重复 → 6 条
    assert len(out) == 6
    assert m_ddgs.called and m_yf.called and m_rss.called


def test_fetch_all_continues_on_per_source_failure(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    with patch("openinvest.services.news_sources.ddgs_news.fetch_ddgs_news",
               side_effect=RuntimeError("boom")), \
         patch("openinvest.services.news_sources.yfinance_news.fetch_yfinance_news") as m_yf, \
         patch("openinvest.services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_yf.return_value = _stub_items("yf", 2)
        m_rss.return_value = _stub_items("rss", 1)
        out = fetch_all(
            queries=["x"], symbols=["A"],
            rss_feeds=[{"name": "r", "url": "https://feed"}],
        )
    # ddgs 挂了不影响其他
    assert len(out) == 3


def test_fetch_all_empty_inputs():
    assert fetch_all() == []


def test_fetch_all_registers_searxng_when_configured(monkeypatch):
    """配了 SEARXNG_URL 时，queries 会同时喂给 ddgs 和 searxng 两个源。"""
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8890")
    with patch("openinvest.services.news_sources.ddgs_news.fetch_ddgs_news") as m_ddgs, \
         patch(
             "openinvest.services.news_sources.searxng_news.fetch_searxng_news"
         ) as m_sx:
        m_ddgs.return_value = _stub_items("ddgs", 1)
        m_sx.return_value = _stub_items("searxng", 2)
        out = fetch_all(queries=["gold"])
    assert m_sx.called
    assert len(out) == 3


def test_rss_feed_parses_minimal_feed():
    """单测 rss_feed.fetch_rss 自身 —— mock feedparser.parse"""
    from openinvest.services.news_sources.rss_feed import fetch_rss
    fake = MagicMock()
    fake.entries = [
        {"link": "https://r.com/a", "title": "<b>A</b>",
         "summary": "<p>hello world</p>", "published_parsed": (2026, 5, 13, 10, 0, 0, 0, 0, 0)},
        {"link": "", "title": "skip me"},  # 无 link 跳过
    ]
    with patch("feedparser.parse", return_value=fake):
        out = fetch_rss("test", "https://feed", max_items=10)
    assert len(out) == 1
    assert out[0].title == "<b>A</b>"  # title 不剥 HTML（让 LLM 看到原貌）
    assert "<p>" not in out[0].snippet   # snippet 剥了
    assert out[0].published_at and out[0].published_at.startswith("2026-05-13")


def test_fetch_all_returns_when_a_source_hangs(monkeypatch):
    """卡死的源不能拖住 fetch_all（2026-07-30 事故回归）。

    旧实现用 `with ThreadPoolExecutor(...)`，退出时 shutdown(wait=True) 会 join
    卡死的 worker —— as_completed 的 timeout 形同虚设，event_watch 那个实例永不
    结束，APScheduler max_instances=1 于是把之后每一次触发都 skip 掉。
    """
    import threading
    import time

    monkeypatch.delenv("SEARXNG_URL", raising=False)
    release = threading.Event()

    def _hang(*_a, **_kw):
        release.wait(60)  # 测试结束前一直卡着
        return []

    try:
        with patch("openinvest.services.news_sources.ddgs_news.fetch_ddgs_news") as m_ddgs, \
             patch("openinvest.services.news_sources.yfinance_news.fetch_yfinance_news") as m_yf, \
             patch("openinvest.services.news_sources.rss_feed.fetch_rss", _hang):
            m_ddgs.return_value = _stub_items("ddgs", 2)
            m_yf.return_value = []
            t0 = time.monotonic()
            out = fetch_all(
                queries=["foo"],
                symbols=["NDQ.AX"],
                rss_feeds=[{"name": "r", "url": "https://feed"}],
                timeout_sec=1.0,
            )
            elapsed = time.monotonic() - t0
        assert elapsed < 10, f"fetch_all 被卡死的源拖了 {elapsed:.1f}s"
        assert len(out) == 2, "已经到手的源结果应照常返回"
    finally:
        release.set()
