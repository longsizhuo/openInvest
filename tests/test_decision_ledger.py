"""decision_ledger 契约测试 —— parse / 幂等 / 成交匹配（issue #133 Decision 9）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from openinvest.core.decision_ledger import (
    MATCH_WINDOW_DAYS,
    _match_trades,
    list_decisions,
    parse_committee_file,
    record_execution,
)

MD = """# Committee: 伦敦金

**Date**: 2026-07-03
**Symbol**: GC=F
**Verdict**: HOLD (confidence 0.65)
**Suggested allocation CNY**: -5,000

## Macro Context Snapshot (for post-hoc attribution)

```json
{"vix": 15.81}
```
"""


def test_parse_committee_file(tmp_path):
    p = tmp_path / "GC_F.md"
    p.write_text(MD, encoding="utf-8")
    got = parse_committee_file(p)
    assert got == {
        "verdict": "HOLD",
        "confidence": 0.65,
        "macro_at_decision": {"vix": 15.81},
        "symbol": "GC=F",
        "alloc_cny": -5000.0,
    }
    assert parse_committee_file(tmp_path / "missing.md") is None


def test_record_execution_idempotent(tmp_path):
    p = tmp_path / "executions.jsonl"
    r1 = record_execution("2026-07-01/GC=F", False, reason="太贵", path=p)
    r2 = record_execution("2026-07-01/GC=F", False, reason="太贵", path=p)  # 重放
    assert r1 == r2
    assert len(p.read_text().splitlines()) == 1
    # 改口 → 新行 append，读方取最后一条
    record_execution("2026-07-01/GC=F", True, reason="想通了", path=p)
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert len(lines) == 2 and lines[-1]["executed"] is True


def test_record_execution_rejects_bad_id(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        record_execution("no-slash", True, path=tmp_path / "e.jsonl")


def test_match_trades():
    trades = [
        {"id": 1, "verdict_id": None, "symbol": "GC=F", "direction": "SELL",
         "units": 1, "ts": "2026-07-05T10:00:00", "status": "executed"},
        {"id": 2, "verdict_id": None, "symbol": "GC=F", "direction": "BUY",
         "units": 1, "ts": "2026-07-05T10:00:00", "status": "executed"},  # 方向不符
        {"id": 3, "verdict_id": None, "symbol": "GC=F", "direction": "SELL",
         "units": 1, "ts": f"2026-07-{3 + MATCH_WINDOW_DAYS + 1:02d}T10:00:00",
         "status": "executed"},  # 出窗
        {"id": 4, "verdict_id": "2026-07-03/GC=F", "symbol": "GC=F",
         "direction": "BUY", "units": 9, "ts": "2026-08-01T00:00:00",
         "status": "executed"},  # 显式关联，窗口/方向都不管
    ]
    # 显式 verdict_id 优先，只返回它
    got = _match_trades(trades, "2026-07-03/GC=F", "2026-07-03", "GC=F", "TRIM")
    assert [t["id"] for t in got] == [4]
    # 无显式关联 → 窗口 + 方向匹配
    got = _match_trades(trades[:3], "2026-07-03/GC=F", "2026-07-03", "GC=F", "TRIM")
    assert [t["id"] for t in got] == [1]
    # HOLD 无方向 → 不匹配
    assert _match_trades(trades[:3], "2026-07-03/GC=F", "2026-07-03", "GC=F", "HOLD") == []


def test_match_trades_utc_same_day_morning(monkeypatch):
    """UTC+8 用户决议日早晨的成交（UTC 还是前一天）必须落进窗口（review finding #4）。

    旧写法在本地时区为 UTC 时 skip——CI runner 恒 UTC，这条回归网从没在 CI 跑过
    （issue #179 P2）。改为强制 TZ=Asia/Shanghai + tzset()，任何环境都执行。
    """
    import os, time
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        _run_utc_morning_case()
    finally:
        # monkeypatch 会还原 TZ env，但 libc 时区缓存要再 tzset 一次才生效
        monkeypatch.undo()
        time.tzset()


def _run_utc_morning_case():
    # 本地 2026-07-03 06:00（东八区）= UTC 2026-07-02T22:00
    trades = [{"id": 1, "verdict_id": None, "symbol": "GC=F", "direction": "SELL",
               "units": 1, "ts": "2026-07-02T22:00:00+00:00", "status": "executed"}]
    got = _match_trades(trades, "2026-07-03/GC=F", "2026-07-03", "GC=F", "TRIM")
    assert [t["id"] for t in got] == [1]


def test_vid_matches_legacy_transcript_path():
    """旧文档口径（transcript 路径）也要能显式关联（review finding #5）。"""
    from openinvest.core.decision_ledger import _vid_matches
    assert _vid_matches("memory/.committee/2026-07-03/GC_F.md",
                        "2026-07-03/GC=F", "2026-07-03", "GC=F")
    assert _vid_matches("2026-07-03/GC=F", "2026-07-03/GC=F", "2026-07-03", "GC=F")
    assert not _vid_matches("memory/.committee/2026-07-02/GC_F.md",
                            "2026-07-03/GC=F", "2026-07-03", "GC=F")


def test_record_execution_concurrent_no_double_append(tmp_path):
    """并发相同重放只落一行（ADR-016 原子幂等闸，review finding #9）。"""
    from concurrent.futures import ThreadPoolExecutor
    p = tmp_path / "e.jsonl"

    def w(_):
        # 每次调用独立 open → 独立 open-file-description → flock 真实互斥
        record_execution("2026-07-01/GC=F", False, reason="x", path=p)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(w, range(16)))
    assert len(p.read_text().splitlines()) == 1


# ---------- list_decisions() 端到端（回归：本体之前零覆盖，只测过 parse/match/record 三个子函数）----------

class _FakeTradesDB:
    """免碰真实 db/trades.db —— list_decisions() 内部 `TradesDB()` 局部 import，
    patch 源模块属性即可让每次调用都拿到这个假类。trades 内容由测试用例注入。"""

    def __init__(self, *a, **kw):
        pass

    def list_trades(self, limit=10000):
        return list(self._TRADES)


def test_list_decisions_end_to_end_seeded_ledger(monkeypatch, tmp_path):
    """种委员会 md + interventions + verdict_review + executions + 假 trades，
    验证 list_decisions() 真的把四份账本 join 成一条完整记录，而不是静默退化成
    空列表——list_decisions() 之前没有任何测试直接调用过它本体（同 #197 那类风险：
    上游任一环节（MemoryStore 路径解析 / 目录遍历 / join 条件）写错都不会被现有
    测试发现，因为大家只测了 parse_committee_file / _match_trades / record_execution
    这几个子函数）。
    """
    import openinvest.core.memory_store as ms
    import openinvest.db.trades_db as trades_db_mod

    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(trades_db_mod, "TradesDB", _FakeTradesDB)

    date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    sym = "GC=F"
    decision_id = f"{date}/{sym}"
    monkeypatch.setattr(_FakeTradesDB, "_TRADES", [
        {"id": 1, "verdict_id": decision_id, "symbol": sym, "direction": "SELL",
         "units": 2, "ts": "2026-01-01T00:00:00", "status": "executed", "price": 500.0}
    ], raising=False)

    committee_dir = tmp_path / ".committee" / date
    committee_dir.mkdir(parents=True)
    (committee_dir / "GC_F.md").write_text(
        f"# Committee: 伦敦金\n\n"
        f"**Date**: {date}\n"
        f"**Symbol**: {sym}\n"
        f"**Verdict**: TRIM (confidence 0.72)\n"
        f"**Suggested allocation CNY**: -8,000\n\n"
        f"## Macro Context Snapshot (for post-hoc attribution)\n\n"
        f'```json\n{{"vix": 15.81}}\n```\n',
        encoding="utf-8",
    )

    dreams = tmp_path / ".dreams"
    dreams.mkdir(parents=True)
    (dreams / "interventions.jsonl").write_text(
        json.dumps({"date": date, "asset": sym, "rule": "sanity4_concentration",
                    "rule_family": "concentration", "original_verdict": "TRIM",
                    "original_alloc": -8000}) + "\n",
        encoding="utf-8",
    )
    (dreams / "verdict_review.jsonl").write_text(
        json.dumps({"date": date, "asset": sym, "source": "live",
                    "actual_returns": {"30d": 1.2}, "hits": {"30d": True},
                    "macro_shock": {"detected": False}}) + "\n",
        encoding="utf-8",
    )

    record_execution(decision_id, True, reason="想通了", trade_ids=[1])

    decisions = list_decisions(days=90)

    assert decisions != [], "list_decisions() 不应静默退化成空列表"
    got = next((d for d in decisions if d["decision_id"] == decision_id), None)
    assert got is not None, f"{decision_id} 未出现在 list_decisions() 结果里"
    assert got["symbol"] == sym
    assert got["verdict"] == "TRIM"
    assert got["confidence"] == 0.72
    assert got["alloc_cny"] == -8000.0
    assert got["intervention"] is not None
    assert got["intervention"]["rule"] == "sanity4_concentration"
    assert got["executed"] is True
    assert got["execution"]["reason"] == "想通了"
    assert [t["id"] for t in got["matched_trades"]] == [1]
    assert got["outcome"] is not None
    assert got["outcome"]["actual_returns"] == {"30d": 1.2}
