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
        "alloc_currency": "CNY",
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


def test_explain_decision(monkeypatch, tmp_path):
    """explain_decision 单条回放：正常路径 + 路径逃逸/缺失的错误契约。
    MCP / CLI 共用本体（同 record_execution 模式），测本体即测两端。"""
    import openinvest.core.memory_store as ms
    from openinvest.core.decision_ledger import explain_decision

    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    committee_dir = tmp_path / ".committee" / "2026-07-03"
    committee_dir.mkdir(parents=True)
    (committee_dir / "GC_F.md").write_text(MD, encoding="utf-8")
    (committee_dir / "GC_F_path.json").write_text('{"p_up": 0.4}', encoding="utf-8")

    got = explain_decision("2026-07-03/GC=F")
    assert got["verdict"] == "HOLD"
    assert got["confidence"] == 0.65
    assert got["alloc_cny"] == -5000.0
    assert got["path_snapshot"] == {"p_up": 0.4}
    assert isinstance(got["confidence_lookup"], str) and got["confidence_lookup"]
    assert "伦敦金" in got["transcript_markdown"]

    assert "error" in explain_decision("no-slash")
    assert "error" in explain_decision("../../etc/GC=F")      # date 段防逃逸
    assert "error" in explain_decision("2026-07-03\n/GC=F")  # fullmatch，不吃尾随换行
    assert "error" in explain_decision("2026-07-03/UNKNOWN")


def test_history_as_trades_shapes_and_filters():
    """portfolio_history.jsonl → trades.db 同形 dict：buy/bought→BUY、sell/sold→SELL；
    deposit/withdraw 不是成交；补录已有持仓（':existing_position'）不是执行决议。"""
    from openinvest.core.decision_calc import history_as_trades

    got = history_as_trades([
        {"ts": "2026-07-03T09:00:00+08:00", "action": "buy", "symbol": "GC=F",
         "units": 2, "price": 500.0, "source": "mcp"},
        {"ts": "2026-07-03T10:00:00+08:00", "ts_origin": "2026-07-03T10:00:01+08:00",
         "action": "sold", "symbol": "BHP.AX", "units": 10, "source": "commsec_email"},
        {"ts": "2026-07-03T11:00:00+08:00", "action": "deposit", "symbol": "CNY", "units": 100},
        {"ts": "2026-07-03T12:00:00+08:00", "action": "buy", "symbol": "AAPL", "units": 1,
         "source": "mcp:existing_position", "funding_source": "external_funding"},
        {"ts": "2026-07-03T13:00:00+08:00", "action": "buy", "symbol": "NDQ.AX", "units": 1,
         "source": "dca_daily", "funding_source": "external_funding"},
        {"ts": "2026-07-03T14:00:00+08:00", "action": "bought", "symbol": "NDQ.AX", "units": 3},
    ])
    # dca_daily 自动定投被滤掉；CommSec 回灌（无 source）是真券商成交，保留
    assert [(t["symbol"], t["direction"], t["status"]) for t in got] == [
        ("GC=F", "BUY", "executed"), ("BHP.AX", "SELL", "executed"), ("NDQ.AX", "BUY", "executed")]
    assert got[2]["id"] == "history:5"
    assert got[0]["id"] == "history:0" and got[0]["verdict_id"] is None
    assert got[1]["ts"] == "2026-07-03T10:00:01+08:00"  # ts_origin（本地时区）优先


def test_list_decisions_matches_agent_buy_from_portfolio_history(monkeypatch, tmp_path):
    """MCP / CLI 的 buy 走 PortfolioManager.buy → 只写 memory/portfolio_history.jsonl、
    不写 trades.db。wiki 20 承诺的"7 天内同标的同向成交自动匹配"必须对这条路径成立——
    此前 list_decisions 只读 TradesDB，agent 记的成交永远 executed=None。"""
    import openinvest.core.memory_store as ms
    import openinvest.db.trades_db as trades_db_mod
    from openinvest.core.portfolio_manager import PortfolioManager

    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(trades_db_mod, "TradesDB", _FakeTradesDB)
    monkeypatch.setattr(_FakeTradesDB, "_TRADES", [], raising=False)  # trades.db 空：只有 history

    date = datetime.now().strftime("%Y-%m-%d")
    sym = "GC=F"
    committee_dir = tmp_path / ".committee" / date
    committee_dir.mkdir(parents=True)
    (committee_dir / "GC_F.md").write_text(
        f"# Committee: 伦敦金\n\n**Date**: {date}\n**Symbol**: {sym}\n"
        f"**Verdict**: ACCUMULATE (confidence 0.70)\n**Suggested allocation CNY**: 1,000\n",
        encoding="utf-8",
    )

    store = ms.MemoryStore()  # root = tmp_path（上面已 patch）
    store.write("user", "user", {"display_name": "t"}, "# user")
    store.write("strategy", "strategy", {"target_assets": []}, "# strategy")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 10000.0}, "holdings": []},
                "# portfolio")
    PortfolioManager(store).buy(sym, 2, 500.0, currency="CNY", kind="metal", unit_label="克", source="mcp")

    got = next(d for d in list_decisions(days=30) if d["decision_id"] == f"{date}/{sym}")
    assert got["executed"] is True
    assert [(t["direction"], t["units"], t["price"], t["status"]) for t in got["matched_trades"]] == [
        ("BUY", 2, 500.0, "executed")]
    assert str(got["matched_trades"][0]["id"]).startswith("history:")

    # 反向：补录已有持仓不算执行——同一天另一个 ACCUMULATE 标的只有 existing_position 记录 → 不匹配
    (committee_dir / "AAPL.md").write_text(
        f"# Committee: AAPL\n\n**Date**: {date}\n**Symbol**: AAPL\n"
        f"**Verdict**: ACCUMULATE (confidence 0.70)\n**Suggested allocation CNY**: 1,000\n",
        encoding="utf-8",
    )
    PortfolioManager(store).buy("AAPL", 1, 150.0, currency="USD", source="mcp:existing_position",
                                source_type="external_funding")
    aapl = next(d for d in list_decisions(days=30) if d["decision_id"] == f"{date}/AAPL")
    assert aapl["executed"] is None and aapl["matched_trades"] == []

    # 反向 2：自动定投（jobs/dca_daily.py 写 source="dca_daily"）不算执行决议——生产 90 天 66 笔
    # 买入 64 笔是它，算进去采纳率直接虚高
    (committee_dir / "NDQ_AX.md").write_text(
        f"# Committee: NDQ\n\n**Date**: {date}\n**Symbol**: NDQ.AX\n"
        f"**Verdict**: ACCUMULATE (confidence 0.70)\n**Suggested allocation CNY**: 1,000\n",
        encoding="utf-8",
    )
    PortfolioManager(store).buy("NDQ.AX", 1, 60.0, currency="AUD", kind="etf", source="dca_daily",
                                source_type="external_funding")
    ndq = next(d for d in list_decisions(days=30) if d["decision_id"] == f"{date}/NDQ.AX")
    assert ndq["executed"] is None and ndq["matched_trades"] == []
