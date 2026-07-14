"""searxng_news 适配器测试：未配置静默关闭 / 解析 / 截断 / 失败隔离。"""
from __future__ import annotations

from openinvest.services.news_sources.searxng_news import (
    fetch_searxng_news,
    searxng_base_url,
)

_FAKE_RESULTS = {
    "results": [
        {
            "url": "https://news.qq.com/a/1",
            "title": "沪深300 增强ETF 领涨",
            "content": "今日沪深300……",
            "publishedDate": "2026-07-14T10:00:00",
            "engine": "bing news",
        },
        {"url": "", "title": "没 url 该跳过"},
        {"url": "https://x.com/2", "title": ""},  # 没 title 该跳过
        {
            "url": "https://finance.sina.com.cn/3",
            "title": "黄金震荡",
            "content": "",
            "publishedDate": None,
        },
    ]
}


def test_disabled_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert searxng_base_url() == ""
    # 未配置：不发任何请求直接空列表
    assert fetch_searxng_news("沪深300") == []


def test_parse_results(monkeypatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8890/")

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return _FAKE_RESULTS

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(
        "openinvest.services.news_sources.searxng_news.requests.get", fake_get
    )
    items = fetch_searxng_news("沪深300", max_results=10)
    # 尾斜杠被去掉、走 news 类目；默认不带 time_range（day 粒度引擎支持极差）
    assert captured["url"] == "http://127.0.0.1:8890/search"
    assert captured["params"]["categories"] == "news"
    assert "time_range" not in captured["params"]
    # 缺 url / 缺 title 的被跳过
    assert len(items) == 2
    assert items[0].src_name == "searxng:news.qq.com"
    assert items[0].published_at == "2026-07-14T10:00:00"
    assert items[1].published_at is None
    assert items[0].raw_meta["engine"] == "bing news"


def test_failure_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8890")

    def boom(url, params=None, timeout=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(
        "openinvest.services.news_sources.searxng_news.requests.get", boom
    )
    assert fetch_searxng_news("gold") == []
