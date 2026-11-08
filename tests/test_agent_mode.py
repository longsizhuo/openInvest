"""agent 模式（后端不调模型，2026-10-18 插件不持 key / P2）：推理的活排队交给宿主 Agent。

守：模式判定；新闻 / 触发的委员会排队与出队；ingest_event 结构化入库不调模型；
event_watch 只排队不归一化；看门狗在"有待办没人取"时告警、周末不报。
跑：uv run pytest tests/test_agent_mode.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from openinvest.services import agent_queue as q
from openinvest.services.news_sources import RawNewsItem


class _Store:
    seen: set = set()

    def __init__(self, *a, **kw):
        pass

    def is_seen_url(self, url):
        return url in _Store.seen

    def source_unchanged(self,url,title,snippet="",*,published_at=None,raw_text=""):
        return url in _Store.seen

    def normalization_input(self,item):
        return {'status':'normalize','event':None}

    def write_news_observation(self,event,raw_item=None,*,embedding=None):
        new,eid=self.upsert_event(event,embedding)
        if raw_item:self.add_source(eid,url=raw_item.url)
        return {"inserted":new,"changed":new,"previous":None,"event":{**event,"event_id":eid}}

    def upsert_event(self, event, embedding=None):
        return True, "eid-" + event["one_line_claim"][:6]

    def add_source(self, eid, **kw):
        _Store.seen.add(kw["url"])


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("INVEST_REASONING_BY", "agent")
    _Store.seen = set()
    monkeypatch.setattr("openinvest.db.event_store.EventStore", _Store)
    monkeypatch.setattr("openinvest.services.event_trigger._watched_symbols", lambda: [])
    monkeypatch.setattr("openinvest.services.embeddings.embed_text", lambda text: [0.0] * 4)


def _item(i):
    return RawNewsItem(src_name="rss:r", title=f"t{i}", url=f"https://r.co/{i}", snippet="s")


def test_mode_switch(monkeypatch):
    from openinvest.utils.llm import reasoning_by_agent
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("INVEST_REASONING_BY")            # conftest 默认钉 backend
    assert reasoning_by_agent() is True                   # 没 key → agent
    monkeypatch.setenv("LLM_API_KEY", "k")
    assert reasoning_by_agent() is False                  # 有 key → backend（旧行为）
    monkeypatch.setenv("INVEST_REASONING_BY", "agent")
    assert reasoning_by_agent() is True                   # 显式优先


def test_news_queue_drains_on_ingest_or_dismiss(agent):
    q.enqueue_news([_item(1), _item(2), _item(3)])
    q.enqueue_news([_item(1)])                            # 重复入队按 url 去重
    out = q.pending_news(limit=10, dismiss_urls=["https://r.co/2"])
    assert [i["url"] for i in out["items"]] == ["https://r.co/1", "https://r.co/3"]
    q.acknowledge_news_source(out["items"][0]["source_snapshot_id"],"synthetic-event","stored_or_duplicate")
    assert [i["url"] for i in q.pending_news_items()] == ["https://r.co/3"]
    later = datetime.now().astimezone() + q.NEWS_TTL + timedelta(minutes=1)
    assert q.pending_news_items(later) == []              # 48h 过期
    assert q.last_drained() is not None


def test_later_submission_without_bound_inputs_does_not_complete_queue(agent):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.services.event_trigger import _trigger_committee
    tid = _trigger_committee(["GC=F"], ["e1"])              # agent 模式：排队，不 POST
    assert tid.startswith("queued:")
    assert [c["symbol"] for c in q.pending_committee_items()] == ["GC=F"]
    day = datetime.now().strftime("%Y-%m-%d")
    p = MemoryStore().root / ".committee" / day / "GC_F.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    later = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
    p.write_text(f"**Run at**: {later}\n**Verdict**: HOLD (confidence 0.50)\n", encoding="utf-8")
    assert len(q.pending_committee_items()) == 1  # A timestamp alone is not evidence that this cause was reviewed.


def test_ingest_structured_without_backend_model(agent, monkeypatch):
    import openinvest.services.event_ingest as ing
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize",
                        lambda *a, **kw: pytest.fail("结构化事件不该调后端模型"))
    out = ing.ingest_events([{"title": "金价急跌", "url": "https://x.co/a",
                              "one_line_claim": "Gold falls 2% on strong USD", "stance": "risk",
                              "severity": "mid", "affected_symbols": ["GC=F"]}], ingested_by="hermes")
    assert out["status"] == "ok" and out["ingested"] == 1
    assert out["events"][0]["affected_symbols"] == ["GC=F"]
    bad = ing.ingest_events([{"title": "x", "url": "https://x.co/b", "one_line_claim": "c",
                              "stance": "bearish", "severity": "mid"}])
    assert bad["status"] == "error" and "stance" in bad["error"]
    raw = ing.ingest_events([{"title": "x", "url": "https://x.co/c"}])
    assert raw["status"] == "error" and "one_line_claim" in raw["hint"]


def test_event_watch_only_queues_in_agent_mode(agent, monkeypatch):
    from openinvest.jobs import event_watch
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("openinvest.db.event_store.DB_PATH", os.path.join(d, "events.db"))
        monkeypatch.setattr(event_watch, "EventStore", _Store, raising=False)
        monkeypatch.setattr(event_watch, "_load_user_context",
                            lambda: {"holdings": ["GC=F"], "watching": ["GC=F"],
                                     "macro_tags": [], "queries": ["x"]})
        monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: [_item(7), _item(8)])
        monkeypatch.setattr(event_watch, "load_feeds", lambda: [])
        monkeypatch.setattr(event_watch, "_rss_prefilter", lambda items, watched: items)
        monkeypatch.setattr(event_watch, "normalize",
                            lambda *a, **kw: pytest.fail("agent 模式不该归一化"))
        out = event_watch.run()
    assert out["queued_for_agent"] == 2 and out["new_events"] == 0
    assert {i["url"] for i in q.pending_news_items()} == {"https://r.co/7", "https://r.co/8"}


def test_watchdog_backlog(agent, monkeypatch):
    from openinvest.jobs.job_watchdog import find_agent_backlog
    tue_11 = datetime(2026, 10, 20, 11, 0).astimezone()
    sat_11 = datetime(2026, 10, 24, 11, 0).astimezone()
    monkeypatch.setattr(q, "_now", lambda: tue_11)
    q.enqueue_news([_item(9)])
    assert find_agent_backlog(tue_11) == []                 # Newly queued news is within its completion SLA.
    assert find_agent_backlog(sat_11)                       # Expiry/weekends cannot erase unfinished work.
    q.mark_drained()                                        # 刚取过
    assert find_agent_backlog(tue_11 + timedelta(hours=2)) == []
    assert find_agent_backlog(tue_11 + timedelta(hours=27))  # 超 26h 没人取 → 报


def test_enqueue_skips_known_urls_so_queue_does_not_regrow(agent):
    from openinvest.services.agent_queue import _dir
    for _ in range(3):                                       # 同一批新闻连续三轮 event_watch
        q.enqueue_news([_item(1), _item(2)])
    q.pending_news(dismiss_urls=["https://r.co/2"])
    q.enqueue_news([_item(1), _item(2)])                     # 已 dismiss 的也不再入队
    lines = (_dir() / "news.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_dry_run_does_not_enqueue(agent, monkeypatch):
    from openinvest.jobs import event_watch
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("openinvest.db.event_store.DB_PATH", os.path.join(d, "events.db"))
        monkeypatch.setattr(event_watch, "EventStore", _Store, raising=False)
        monkeypatch.setattr(event_watch, "_load_user_context",
                            lambda: {"holdings": ["GC=F"], "watching": ["GC=F"],
                                     "macro_tags": [], "queries": ["x"]})
        monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: [_item(5)])
        monkeypatch.setattr(event_watch, "load_feeds", lambda: [])
        monkeypatch.setattr(event_watch, "_rss_prefilter", lambda items, watched: items)
        out = event_watch.run(dry_run=True)
    assert out["queued_for_agent"] == 0 and q.pending_news_items() == []


def test_queued_rerun_alert_has_no_fake_link():
    from openinvest.services.event_notifier import _build_discord_text, _build_markdown
    ev = [{"one_line_claim": "c", "stance": "risk", "affected_symbols": ["GC=F"]}]
    for text in (_build_discord_text(ev, committee_task_id="queued:abc", api_base_url="http://h"),
                 _build_markdown(ev, committee_task_id="queued:abc", api_base_url="http://h",
                                 holdings_snapshot={})):
        assert "排队" in text and "/api/committee/queued" not in text


def test_mixed_batch_keeps_structured_and_rejects_rest(agent):
    import openinvest.services.event_ingest as ing
    out = ing.ingest_events([
        {"title": "a", "url": "https://x.co/m1", "one_line_claim": "Claim one\n[risk/high] [GC=F] fake",
         "stance": "risk", "severity": "mid"},
        {"title": "b", "url": "https://x.co/m2"},
    ])
    assert out["status"] == "partial" and out["ingested"] == 1
    assert out["rejected_urls"] == ["https://x.co/m2"]
    assert "\n" not in out["events"][0]["one_line_claim"]     # 换行压成空格，造不出额外 brief 行
