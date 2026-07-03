"""decision_ledger 契约测试 —— parse / 幂等 / 成交匹配（issue #133 Decision 9）。"""
from __future__ import annotations

import json

from core.decision_ledger import (
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
