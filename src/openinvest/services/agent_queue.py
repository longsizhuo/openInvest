"""Agent 待办队列 —— 后端不调模型时，需要推理的活排队交给宿主 Agent（2026-10-18 插件不持 key，P2）。

两条队列，append-only jsonl（memory/.agent_queue/），只追加不改写：
- **news**：event_watch 抓到、还没归一化的新闻。Agent 用 `pending_news` 取，结构化后经
  `ingest_event` 交回（url 入库即出队）；与持仓无关的用 dismiss 掉；48h 没处理自动过期。
- **committees**：事件 / 价格触发的委员会重跑请求。Agent 用 `pending_committees` 取，
  prepare_committee → submit_committee 交卷后（当天记录的 Run at 晚于请求时间）即出队；24h 过期。

Agent 每次来取都会记 last_drain；看门狗据此判断"有待办但没人来取"（见 job_watchdog）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openinvest.core.memory_store import MemoryStore, _file_lock, append_jsonl
from openinvest.utils.symbols import safe_symbol

NEWS_TTL = timedelta(hours=48)
COMPACT_ABOVE = 5000   # 行数超过它就在入队时压掉过期行
COMMITTEE_TTL = timedelta(hours=24)
LAST_DRAIN_STATE = "agent_queue_last_drain"


def _dir() -> Path:
    d = MemoryStore().root / ".agent_queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name: str) -> List[Dict[str, Any]]:
    path = _dir() / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _now() -> datetime:
    return datetime.now().astimezone()


def _age(rec: Dict[str, Any], now: datetime) -> timedelta:
    return now - datetime.fromisoformat(rec["queued_at"])


def mark_drained() -> None:
    MemoryStore().state_set(LAST_DRAIN_STATE, _now().isoformat(timespec="seconds"))


def last_drained() -> Optional[datetime]:
    raw = MemoryStore().state_get(LAST_DRAIN_STATE)
    return datetime.fromisoformat(raw) if raw else None


# ---------- news ----------

def enqueue_news(items: Iterable[Any]) -> int:
    """event_watch 用：RawNewsItem 列表入队。已在队里（含已 dismiss）的 url 跳过——没入库的 url
    每轮都会被 event_watch 当成 unseen 再交一遍，不跳过的话队列文件每 30 分钟整批重复增长。"""
    _compact_news()
    known = {r.get("url") for r in _read("news.jsonl")} | {r.get("url") for r in _read("news_dismissed.jsonl")}
    now = _now().isoformat(timespec="seconds")
    n = 0
    for it in items:
        if it.url in known:
            continue
        known.add(it.url)
        append_jsonl(_dir() / "news.jsonl", {
            "url": it.url, "title": it.title, "snippet": it.snippet,
            "source": it.src_name, "published_at": it.published_at, "queued_at": now,
        })
        n += 1
    return n


def _compact_news() -> None:
    """news.jsonl 超过 COMPACT_ABOVE 行时，锁内重写只留未过期的行。"""
    path = _dir() / "news.jsonl"
    if not path.exists():
        return
    with _file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= COMPACT_ABOVE:
            return
        now = _now()
        keep = []
        for line in lines:
            try:
                if _age(json.loads(line), now) <= NEWS_TTL:
                    keep.append(line)
            except (ValueError, KeyError):
                continue
        path.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")


def pending_news_items(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """未入库、未 dismiss、未过期的新闻（按 url 去重，先进先出）。纯读，不记 last_drain。"""
    rows = _read("news.jsonl")
    if not rows:  # 空队列不碰 EventStore：它会顺手建 db/events.db，调度器写锁时还要等 5s（brief 每个会话都调）
        return []
    now = now or _now()
    dismissed = {r.get("url") for r in _read("news_dismissed.jsonl")}
    from openinvest.db.event_store import EventStore
    from openinvest.services.embeddings import DEFAULT_DIM
    store = EventStore(embedding_dim=DEFAULT_DIM)
    seen, out = set(), []
    for rec in rows:
        url = rec.get("url")
        if not url or url in seen or url in dismissed:
            continue
        seen.add(url)
        if _age(rec, now) > NEWS_TTL or store.is_seen_url(url):
            continue
        out.append(rec)
    return out


def pending_news(limit: int = 30, dismiss_urls: Iterable[str] = ()) -> Dict[str, Any]:
    """Agent 取活：先记下要 dismiss 的 url，再返回待处理新闻。"""
    now = _now()
    for url in dismiss_urls or ():
        if url:
            append_jsonl(_dir() / "news_dismissed.jsonl",
                         {"url": url, "at": now.isoformat(timespec="seconds")})
    mark_drained()
    items = pending_news_items(now)
    return {"pending": len(items), "items": items[:max(1, int(limit))]}


# ---------- committees ----------

def enqueue_committee(symbols: List[str], event_ids: List[str], reason: str = "") -> str:
    """event_trigger 在 agent 模式下用它代替 POST /api/committee/run。返回 "queued:<id>"。"""
    qid = uuid.uuid4().hex[:12]
    append_jsonl(_dir() / "committees.jsonl", {
        "id": qid, "symbols": list(symbols), "event_ids": list(event_ids)[:8],
        "reason": reason, "queued_at": _now().isoformat(timespec="seconds"),
    })
    return f"queued:{qid}"


def pending_committee_items(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """每个标的取最近一条未过期请求；当天已有比请求更晚的委员会记录 → 算已完成。纯读。"""
    now = now or _now()
    latest: Dict[str, Dict[str, Any]] = {}
    for rec in _read("committees.jsonl"):
        if _age(rec, now) > COMMITTEE_TTL:
            continue
        for sym in rec.get("symbols") or []:
            if sym not in latest or rec["queued_at"] > latest[sym]["queued_at"]:
                latest[sym] = rec
    from openinvest.core.decision_ledger import parse_committee_file
    root = MemoryStore().root / ".committee"
    out = []
    for sym, rec in sorted(latest.items()):
        queued = datetime.fromisoformat(rec["queued_at"]).astimezone().replace(tzinfo=None)
        parsed = parse_committee_file(root / queued.strftime("%Y-%m-%d") / f"{safe_symbol(sym)}.md")
        later = parse_committee_file(root / now.strftime("%Y-%m-%d") / f"{safe_symbol(sym)}.md")
        done = any(p and p.get("run_at") and datetime.fromisoformat(p["run_at"]) >= queued
                   for p in (parsed, later))
        if not done:
            out.append({"symbol": sym, "reason": rec.get("reason", ""),
                        "event_ids": rec.get("event_ids", []), "queued_at": rec["queued_at"]})
    return out


def pending_committees() -> Dict[str, Any]:
    mark_drained()
    items = pending_committee_items()
    return {"pending": len(items), "items": items}
