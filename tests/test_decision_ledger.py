"""decision_ledger 契约测试 —— parse / 幂等 / 成交匹配（issue #133 Decision 9）。"""
from __future__ import annotations

import json

from openinvest.core.decision_ledger import (
    MATCH_WINDOW_DAYS,
    _match_trades,
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
