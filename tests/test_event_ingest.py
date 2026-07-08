"""#153：agent 投喂（event_ingest）+ RSS 预过滤契约。"""
from __future__ import annotations

import pytest


# ---------- ingest_events ----------

class _FakeStore:
    def __init__(self, *a, **kw):
        self.rows = {}
        self.sources = []
        self.seen = set()

    def is_seen_url(self, url):
        return url in self.seen

    def upsert_event(self, event, embedding=None):
        eid = "eid-" + event["one_line_claim"][:8]
        was_new = eid not in self.rows
        self.rows[eid] = event
        return was_new, eid

    def add_source(self, eid, **kw):
        self.sources.append((eid, kw))
        self.seen.add(kw["url"])


class _NE:
    def __init__(self, item, claim):
        self.event = {"one_line_claim": claim, "event_type": "policy",
                      "stance": "bearish", "severity": "mid",
                      "affected_symbols": ["510300.SS"], "entities": []}
        self.embedding = None
        self.raw_item = item
        self.raw_idx = 0


def test_ingest_normalizes_and_persists(monkeypatch):
    import openinvest.services.event_ingest as mod
    store = _FakeStore()
    monkeypatch.setattr("openinvest.db.event_store.EventStore", lambda *a, **kw: store)
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize",
                        lambda items, **kw: [_NE(items[0], "PBOC cuts RRR 50bp")])
    out = mod.ingest_events([{"title": "央行降准", "url": "https://caixin.com/x",
                              "source": "caixin"}])
    assert out["status"] == "ok" and out["ingested"] == 1
    assert out["events"][0]["affected_symbols"] == ["510300.SS"]
    # 来源打 agent: 前缀（审计归因）
    assert store.sources[0][1]["src_name"] == "agent:caixin"


def test_ingest_url_dedup_skips_llm(monkeypatch):
    import openinvest.services.event_ingest as mod
    store = _FakeStore()
    store.seen.add("https://caixin.com/x")
    monkeypatch.setattr("openinvest.db.event_store.EventStore", lambda *a, **kw: store)
    called = []
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize",
                        lambda items, **kw: called.append(1) or [])
    out = mod.ingest_events([{"title": "t", "url": "https://caixin.com/x"}])
    assert out == {"status": "ok", "ingested": 0, "duplicates": 1, "events": []}
    assert not called  # 已见 url 不烧 LLM


def test_ingest_missing_llm_key_is_explicit_error(monkeypatch):
    import openinvest.services.event_ingest as mod
    monkeypatch.setattr("openinvest.db.event_store.EventStore", lambda *a, **kw: _FakeStore())
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize", lambda items, **kw: [])
    monkeypatch.setattr("openinvest.utils.llm.get_llm_config_safe",
                        lambda **kw: (None, None, None, None))
    out = mod.ingest_events([{"title": "t", "url": "https://x.com/1"}])
    assert out["status"] == "error" and "LLM" in out["error"]


def test_ingest_rejects_missing_fields():
    from openinvest.services.event_ingest import ingest_events
    assert ingest_events([{"title": "", "url": "https://x"}])["status"] == "error"
    assert ingest_events([{"title": "t", "url": ""}])["status"] == "error"


# ---------- RSS 预过滤 ----------

def _raw(src, title, snippet=""):
    from openinvest.services.news_sources import RawNewsItem
    return RawNewsItem(src_name=src, title=title, url=f"https://x/{title[:8]}", snippet=snippet)


def test_rss_prefilter_keeps_relevant_and_macro():
    from openinvest.jobs.event_watch import _rss_prefilter
    watched = ["GC=F", "510300.SS"]
    items = [
        _raw("rss:yahoo_finance_headline", "Gold rallies to record high"),      # 别名命中
        _raw("rss:seeking_alpha_market", "沪深300 ETF 资金流入创新高"),            # 中文别名
        _raw("rss:yahoo_finance_headline", "Fed signals rate cut in September"),  # macro 保留
        _raw("rss:bbc_business", "Celebrity launches new perfume line"),          # 无关 → 拦
        _raw("ddgs:msn.com", "random noise but ddgs channel"),                    # 非 rss 不过滤
    ]
    kept = _rss_prefilter(items, watched)
    titles = [i.title for i in kept]
    assert "Celebrity launches new perfume line" not in titles
    assert len(kept) == 4


def test_rss_prefilter_unknown_symbol_falls_back_to_root():
    from openinvest.jobs.event_watch import _rss_prefilter
    items = [_raw("rss:ft_markets", "AAPL beats earnings expectations")]
    assert len(_rss_prefilter(items, ["AAPL"])) == 1
    assert len(_rss_prefilter([_raw("rss:ft_markets", "unrelated story")], ["AAPL"])) == 0


# ---------- akshare 中文快讯（#153 A股盲区） ----------

def test_cn_wire_adapter_maps_dataframes(monkeypatch):
    import pandas as pd
    import akshare as ak
    monkeypatch.setattr(ak, "stock_info_global_em", lambda: pd.DataFrame([
        {"标题": "央行开展 3000 亿逆回购", "摘要": "净投放...", "发布时间": "2026-07-07 09:00:00",
         "链接": "https://finance.eastmoney.com/a/1.html"}]))
    monkeypatch.setattr(ak, "stock_info_global_sina", lambda: pd.DataFrame([
        {"时间": "2026-07-07 09:01:00", "内容": "沪深300开盘涨0.5%"}]))
    from openinvest.services.news_sources.akshare_news import fetch_cn_wire
    items = fetch_cn_wire(max_items=5)
    assert len(items) == 2
    em, sina = items
    assert em.src_name == "akshare:em_global" and em.url.startswith("https://")
    assert em.published_at == "2026-07-07T09:00:00+08:00"
    assert sina.src_name == "akshare:sina_7x24" and sina.url.startswith("akshare://sina724/")


def test_cn_wire_source_failure_degrades(monkeypatch):
    import akshare as ak
    def boom(): raise RuntimeError("上游改版")
    monkeypatch.setattr(ak, "stock_info_global_em", boom)
    monkeypatch.setattr(ak, "stock_info_global_sina", boom)
    from openinvest.services.news_sources.akshare_news import fetch_cn_wire
    assert fetch_cn_wire() == []  # 静默降级不抛


def test_prefilter_covers_akshare_prefix():
    from openinvest.jobs.event_watch import _rss_prefilter
    items = [
        _raw("akshare:sina_7x24", "沪深300开盘涨0.5%"),      # 命中别名
        _raw("akshare:em_global", "某公司发布新款宠物食品"),   # 无关 → 拦
    ]
    kept = _rss_prefilter(items, ["510300.SS"])
    assert [i.title for i in kept] == ["沪深300开盘涨0.5%"]
