"""services/event_trigger：两条门（爬虫 event_watch + agent 投喂 ingest_event）共用的
触发闸 + 冷却/日上限/越级 + 顾问模式隔离。

回归背景（2026-10）：ingest_events 只入库不触发——Hermes 哨兵喂进来的持仓风险事件
committee_task_id 全 NULL。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from openinvest.jobs import event_watch
from openinvest.services import event_trigger
from openinvest.services.event_normalizer import NormalizedEvent
from openinvest.services.event_trigger import admit
from openinvest.services.news_sources import RawNewsItem

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
WATCHED = ["AAPL", "MSFT", "TSLA", "NVDA", "SPY"]


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _adm(state, cands, now=NOW, **kw):
    kw = {"cooldown_hours": 12, "daily_cap": 4, **kw}
    return admit(state, cands, now, **kw)


# ---------- 纯函数：冷却 + 滚动 24h 上限 + 越级 ----------

def test_admit_cap_limits_symbols_per_rolling_day():
    got, state = _adm({}, [(s, 2) for s in "ABCDE"])
    assert got == ["A", "B", "C", "D"]
    assert len(state["runs"]) == 4 and state["last"]["A"] == {"ts": _iso(NOW), "sev": 2}
    # 23h 后额度仍满（滚动窗口，不是自然日）；25h 后清空
    assert _adm(state, [("E", 2)], NOW + timedelta(hours=23))[0] == []
    assert _adm(state, [("E", 2)], NOW + timedelta(hours=25))[0] == ["E"]


def test_admit_cap_prefers_higher_severity():
    """额度紧时高 severity 先占（旧版按字母序，sev-3 会被 sev-2 挤掉）"""
    got, _ = _adm({}, [("A", 2), ("B", 2), ("C", 3)], daily_cap=2)
    assert got == ["C", "A"]


def test_admit_per_symbol_cooldown():
    state = {"last": {"A": {"ts": _iso(NOW - timedelta(hours=11)), "sev": 2}}, "runs": []}
    assert _adm(state, [("A", 2), ("B", 2)])[0] == ["B"]
    state = {"last": {"A": {"ts": _iso(NOW - timedelta(hours=13)), "sev": 2}}, "runs": []}
    assert _adm(state, [("A", 2)])[0] == ["A"]


def test_admit_escalation_bypasses_cooldown_only_when_strictly_higher():
    opened_mid = {"last": {"A": {"ts": _iso(NOW - timedelta(hours=1)), "sev": 2}}, "runs": []}
    got, state = _adm(opened_mid, [("A", 3)])
    assert got == ["A"] and state["last"]["A"] == {"ts": _iso(NOW), "sev": 3}
    assert _adm(opened_mid, [("A", 2)])[0] == []                          # 同级不越
    assert _adm(opened_mid, [("A", 3)], escalation_bypass=False)[0] == []  # 开关关
    full = {**opened_mid, "runs": [_iso(NOW - timedelta(hours=1))] * 4}
    assert _adm(full, [("A", 3)])[0] == []                                # 越级仍受上限
    legacy = {"last": {"A": _iso(NOW - timedelta(hours=1))}, "runs": []}   # 旧纯字符串格式
    assert _adm(legacy, [("A", 3)])[0] == []                              # rank 未知按 high，不越级


def test_admit_tolerates_bad_state():
    assert _adm({}, [("A", 2)], daily_cap=0)[0] == []
    junk = {"last": {"A": "not-a-date", "B": {"ts": 5, "sev": "x"}}, "runs": ["garbage", None]}
    got, state = _adm(junk, [("A", 2), ("B", 2)])
    assert got == ["A", "B"] and state["runs"] == [_iso(NOW)] * 2
    assert _adm(["not", "a", "dict"], [("A", 2)])[0] == ["A"]
    assert _adm({"last": "x", "runs": "y"}, [("A", 2)])[0] == ["A"]
    # 缺时区 / 纯日期 按 UTC 解析，不抛 TypeError
    naive = {"last": {"A": "2026-10-07T06:00:00"}, "runs": ["2026-10-07"] * 4}
    assert _adm(naive, [("B", 2)])[0] == []          # 纯日期 runs 仍在 24h 窗内 → 额度满
    assert _adm({"last": {"A": "2026-10-07T06:00:00"}}, [("A", 2)])[0] == []  # 6h 前 → 冷却中
    # 晚于 now 的时刻（手改/时钟回拨）当没记录，不能把 symbol 永久锁死
    future = {"last": {"A": _iso(NOW + timedelta(days=30))}, "runs": [_iso(NOW + timedelta(days=30))] * 4}
    assert _adm(future, [("A", 2)])[0] == ["A"]


# ---------- 两条门端到端（真 EventStore tmp 库 + mock 委员会/报警） ----------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(event_trigger, "_watched_symbols", lambda: list(WATCHED))
    snapshot = MagicMock(return_value={})
    monkeypatch.setattr(event_trigger, "_holdings_snapshot", snapshot)
    trigger = MagicMock(side_effect=lambda symbols, event_ids: f"task-{len(trigger.mock_calls)}")
    alert = MagicMock(return_value="")
    monkeypatch.setattr(event_trigger, "_trigger_committee", trigger)
    monkeypatch.setattr(event_trigger, "send_event_alert", alert)
    return trigger, alert, snapshot


def _state(tmp_path):
    return json.loads((tmp_path / "memory" / ".state" / "event_committee_triggers.json").read_text())


def _ne(claim, affected, *, stance="risk", severity="high", item=None, idx=0):
    item = item or RawNewsItem(src_name="r", title=claim, url=f"https://r.co/{claim}", snippet="s")
    return NormalizedEvent(
        raw_idx=idx,
        event={"one_line_claim": claim, "event_type": "earnings", "stance": stance,
               "severity": severity, "ts": "2026-10-07T10:00:00Z",
               "entities": [], "affected_symbols": affected},
        embedding=None, raw_item=item)


def _feed(monkeypatch, claim, affected, *, stance="risk", severity="high"):
    """走 ingest_events 真入口（MCP/CLI 同源），normalize mock 成一条事件。"""
    from openinvest.services.event_ingest import ingest_events
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize",
                        lambda items, **kw: [_ne(claim, affected, stance=stance,
                                                 severity=severity, item=items[0])])
    return ingest_events([{"title": claim, "url": f"https://x.co/{claim}"}],
                         ingested_by="hermes-sentinel")


def _run_watch(monkeypatch, events, holdings):
    monkeypatch.setattr(event_watch, "_load_user_context", lambda: {
        "holdings": holdings, "watching": [], "queries": ["x"]})
    monkeypatch.setattr(event_watch, "load_feeds", lambda: [])
    items = [ne.raw_item for ne in events]
    monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: items)
    monkeypatch.setattr(event_watch, "normalize", lambda its: events)
    return event_watch.run()


def test_ingest_door_triggers_committee_and_marks_event(env, monkeypatch):
    """核心回归：agent 投喂的持仓风险事件必须触发委员会（旧码只入库，0 次触发）。"""
    trigger, alert, snapshot = env
    out = _feed(monkeypatch, "earnings-miss-guidance-cut", ["NVDA"])
    assert out["status"] == "ok" and out["ingested"] == 1
    trigger.assert_called_once()
    assert trigger.call_args.kwargs["symbols"] == ["NVDA"]
    assert out["committee_task_id"] == "task-1"
    from openinvest.db.event_store import EventStore
    assert EventStore().get_event(out["events"][0]["event_id"])["committee_task_id"] == "task-1"
    alert.assert_called_once()        # 报警照走 send_event_alert（默认静默策略在它内部）
    snapshot.assert_not_called()      # 报警静默时不为它拉行情


def test_holdings_snapshot_only_when_alerts_enabled(env, monkeypatch):
    _, _, snapshot = env
    monkeypatch.setenv("INVEST_EVENT_ALERT", "1")
    _feed(monkeypatch, "alerts-on", ["AAPL"])
    snapshot.assert_called_once_with(["AAPL"])


def test_ingest_door_gate_rejects_neutral_low_unwatched(env, monkeypatch):
    trigger, alert, _ = env
    _feed(monkeypatch, "neutral-item", ["AAPL"], stance="neutral")
    _feed(monkeypatch, "low-item", ["AAPL"], severity="low")
    _feed(monkeypatch, "unwatched-item", ["NFLX"])
    trigger.assert_not_called()
    alert.assert_not_called()


def test_cooldown_shared_between_event_watch_and_ingest(env, monkeypatch):
    """爬虫门先触发 MSFT → 12h 内投喂门同 symbol 同级不再跑；别的 symbol 照跑。"""
    trigger, _, _ = env
    assert _run_watch(monkeypatch, [_ne("msft-selloff", ["MSFT"])], ["MSFT"])["committee_symbols"] == ["MSFT"]
    assert _feed(monkeypatch, "msft-futures-slump", ["MSFT"])["committee_task_id"] is None
    assert _feed(monkeypatch, "tsla-breaks-support", ["TSLA"])["committee_task_id"] == "task-2"
    assert [c.kwargs["symbols"] for c in trigger.call_args_list] == [["MSFT"], ["TSLA"]]


def test_escalation_bypass_across_doors(env, monkeypatch, tmp_path):
    """sev-2 前瞻开的冷却不能吞掉随后的 sev-3 实锤；同级第三条仍被冷却拦。"""
    trigger, _, _ = env
    assert _feed(monkeypatch, "preview", ["NVDA"], severity="mid")["committee_task_id"] == "task-1"
    assert _feed(monkeypatch, "results-miss", ["NVDA"], severity="high")["committee_task_id"] == "task-2"
    assert _feed(monkeypatch, "results-followup", ["NVDA"], severity="high")["committee_task_id"] is None
    assert _state(tmp_path)["last"]["NVDA"]["sev"] == 3


def test_escalation_bypass_can_be_disabled(env, monkeypatch):
    trigger, _, _ = env
    monkeypatch.setenv("INVEST_EVENT_COMMITTEE_ESCALATION_BYPASS", "false")
    _feed(monkeypatch, "preview", ["NVDA"], severity="mid")
    assert _feed(monkeypatch, "results-miss", ["NVDA"], severity="high")["committee_task_id"] is None
    assert trigger.call_count == 1


def test_daily_cap_across_ingest_calls(env, monkeypatch):
    trigger, _, _ = env
    for sym in WATCHED:
        _feed(monkeypatch, f"claim-{sym}", [sym])
    assert trigger.call_count == 4  # 第 5 个 symbol 被滚动 24h 上限拦下


def test_batch_only_marks_events_feeding_the_committee(env, monkeypatch):
    """同批 A（放行）+ B（symbol 冷却中）：B 不进 event_ids、committee_task_id 保持 NULL。"""
    trigger, _, _ = env
    _feed(monkeypatch, "spy-first", ["SPY"])                       # SPY 进冷却
    a, b = _ne("aapl-guidance", ["AAPL"], idx=0), _ne("spy-again", ["SPY"], idx=1)
    out = _run_watch(monkeypatch, [a, b], ["AAPL", "SPY"])
    assert out["committee_symbols"] == ["AAPL"]
    from openinvest.db.event_store import EventStore, claim_to_event_id
    st = EventStore()
    assert st.get_event(claim_to_event_id("aapl-guidance"))["committee_task_id"] == out["committee_task_id"]
    assert st.get_event(claim_to_event_id("spy-again"))["committee_task_id"] is None
    assert trigger.call_args.kwargs["event_ids"] == [claim_to_event_id("aapl-guidance")]


def test_failed_trigger_rolls_back_reservation(env, monkeypatch, tmp_path):
    trigger, _, _ = env
    trigger.side_effect = lambda symbols, event_ids: None  # 本机 web 挂了
    assert _feed(monkeypatch, "first", ["TSLA"])["committee_task_id"] is None
    assert "TSLA" not in _state(tmp_path)["last"] and _state(tmp_path)["runs"] == []
    trigger.side_effect = lambda symbols, event_ids: "task-ok"
    assert _feed(monkeypatch, "second", ["TSLA"])["committee_task_id"] == "task-ok"


def test_reservation_is_locked_before_http_and_not_overwritten(env, monkeypatch, tmp_path):
    """额度在 HTTP 前已落盘；HTTP 期间另一门写入的记录不会被本门的旧快照覆盖。"""
    from openinvest.core.memory_store import MemoryStore
    trigger, _, _ = env

    def other_door_writes_during_http(symbols, event_ids):
        assert "AAPL" in _state(tmp_path)["last"]          # 已预占
        def add_spy(cur):
            cur["last"]["SPY"] = {"ts": _iso(datetime.now(timezone.utc)), "sev": 3}
            cur["runs"].append(cur["last"]["SPY"]["ts"])
            return cur, None
        MemoryStore().state_update("event_committee_triggers", add_spy)
        return "task-x"

    trigger.side_effect = other_door_writes_during_http
    assert _feed(monkeypatch, "aapl-news", ["AAPL"])["committee_task_id"] == "task-x"
    st = _state(tmp_path)
    assert set(st["last"]) == {"AAPL", "SPY"} and len(st["runs"]) == 2


def test_corrupt_state_file_does_not_block_trigger(env, monkeypatch, tmp_path):
    trigger, _, _ = env
    p = tmp_path / "memory" / ".state" / "event_committee_triggers.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert _feed(monkeypatch, "after-corruption", ["AAPL"])["committee_task_id"] == "task-1"


def test_event_watch_survives_trigger_failure(env, monkeypatch):
    """触发环节抛异常：事件已入库，job 照样 ok 返回（不把入库成果算成失败）。"""
    monkeypatch.setattr(event_watch, "trigger_for_new_events",
                        MagicMock(side_effect=RuntimeError("boom")))
    out = _run_watch(monkeypatch, [_ne("msft-x", ["MSFT"])], ["MSFT"])
    assert out["status"] == "ok" and out["new_events"] == 1 and out["committee_task_id"] is None
    from openinvest.db.event_store import EventStore, claim_to_event_id
    assert EventStore().get_event(claim_to_event_id("msft-x")) is not None


def test_advisory_mcp_ingest_never_triggers_or_alerts(env, monkeypatch):
    """顾问实例的 ingest_event 对群聊陌生人放行：入库照做，绝不触发委员会/报警、不读持仓。"""
    trigger, alert, _ = env
    monkeypatch.setenv("INVEST_ADVISORY_MODE", "1")
    watched = MagicMock(return_value=list(WATCHED))  # 不 raise：异常会被 ingest 的兜底吞掉，测不出闸
    monkeypatch.setattr(event_trigger, "_watched_symbols", watched)
    from openinvest.connectors import mcp_server as m
    monkeypatch.setattr("openinvest.services.event_normalizer.normalize",
                        lambda items, **kw: [_ne("stranger-fed", ["AAPL"], item=items[0])])
    out = m.ingest_event(title="t", url="https://x.co/adv")
    assert out["status"] == "ok" and out["ingested"] == 1
    assert out["committee_task_id"] is None
    trigger.assert_not_called()
    alert.assert_not_called()
    watched.assert_not_called()  # 顾问模式不读持仓


def test_adr_tagged_event_triggers_hk_listing_committee(env, monkeypatch):
    """事件打标美股 ADR 代码、关注的是港股上市：照样过闸，委员会按关注写法跑，事件打上链接。
    ETF→指数跟踪不进闸（指数事件不触发 ETF 委员会，现行为）。"""
    trigger, _, _ = env
    monkeypatch.setattr(event_trigger, "_watched_symbols", lambda: ["0700.HK", "QQQ"])
    out = _feed(monkeypatch, "adr-guidance-cut", ["TCEHY"])
    assert trigger.call_args.kwargs["symbols"] == ["0700.HK"]
    from openinvest.db.event_store import EventStore
    assert EventStore().get_event(out["events"][0]["event_id"])["committee_task_id"] == out["committee_task_id"]
    assert _feed(monkeypatch, "index-slide", ["^NDX"])["committee_task_id"] is None
    assert trigger.call_count == 1
