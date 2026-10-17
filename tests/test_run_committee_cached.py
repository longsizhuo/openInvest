"""run_committee 命中当日缓存：CLI 不再要求 LLM key；MCP 缓存返回带 memo 与 Input Health。

e2e 实测（2026-10-17）：CLI 的 key 闸先于缓存检查——今天已有 transcript 也读不了；
MCP 缓存返回缺 memo / Input Health，agent 得再调一次 explain_decision。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

import pytest

from openinvest.core.decision_ledger import parse_committee_text

# 与 core/committee/persist._persist 同版式（Run at / Input Health / CIO Memo / 后续分节）
PERSIST_MD = """# Committee: AAPL

**Date**: 2026-10-17
**Symbol**: AAPL
**Run at**: 2026-10-17T10:00:00
**Verdict**: ACCUMULATE (confidence 0.70)
**Suggested allocation CNY**: 1000

---

## Input Health (deterministic)

```json
{"empty_inputs": ["event_brief"], "rewritten": ["single_invest_cap"]}
```

---

## CIO Memo (Round 3)

VERDICT: ACCUMULATE
首笔 ¥4,800，分三批。

---
这是 memo 里自带的分隔线，不该截断。

⚠️ 金额已按单次上限 ¥1,000 钳制


---

## Macro Strategist (shared)

macro text
"""

# Coordinator（save_committee_transcript）版式：### CIO 小节
COORDINATOR_MD = """# Committee: AAPL

**Symbol**: AAPL
**Verdict**: HOLD (confidence 0.50)

## Reports

### MACRO

macro text

### CIO

cio line 1
cio line 2

### QUANT

quant text
"""


def _seed(tmp_path, monkeypatch, text=PERSIST_MD):
    import openinvest.core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
    p = tmp_path / "memory" / ".committee" / datetime.now().strftime("%Y-%m-%d") / "AAPL.md"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_committee_text_extracts_cio_memo():
    got = parse_committee_text(PERSIST_MD)
    assert got["cio_memo"].startswith("VERDICT: ACCUMULATE")
    assert "这是 memo 里自带的分隔线" in got["cio_memo"] and "钳制" in got["cio_memo"]
    assert "macro text" not in got["cio_memo"] and "Input Health" not in got["cio_memo"]
    assert parse_committee_text(COORDINATOR_MD)["cio_memo"] == "cio line 1\ncio line 2"
    assert parse_committee_text("**Verdict**: HOLD (confidence 0.5)\n")["cio_memo"] is None


def test_cli_cached_run_committee_works_without_llm_key(monkeypatch, capfd, tmp_path):
    """无 key + 当日已有 transcript → 返回缓存结果，不报错、不退出。"""
    from openinvest.skill_cmds import committee_cmds as cc

    class FakePM:
        strategy = {"target_assets": [{"symbol": "AAPL", "target_pct": 1.0}]}

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.setattr("openinvest.core.portfolio_manager.PortfolioManager", lambda: FakePM())
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})
    md = tmp_path / "memory" / ".committee" / datetime.now().strftime("%Y-%m-%d") / "AAPL.md"
    md.parent.mkdir(parents=True)
    md.write_text(PERSIST_MD, encoding="utf-8")

    cc.cmd_run_committee(argparse.Namespace(symbol="AAPL", force=False, max_rounds=1))
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "cached" and "Input Health" in out["transcript_md"]

    # 反向：没缓存（或 --force）+ 无 key → 仍然报 key 错并退出 1
    for force in (True, False):
        if not force:
            md.unlink()
        with pytest.raises(SystemExit) as e:
            cc.cmd_run_committee(argparse.Namespace(symbol="AAPL", force=force, max_rounds=1))
        assert e.value.code == 1
        assert "LLM_API_KEY" in json.loads(capfd.readouterr().out)["error"]


def test_mcp_cached_run_committee_carries_memo_and_input_health(monkeypatch, tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openinvest.connectors.mcp_server import mcp

    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})

    async def go():
        async with create_connected_server_and_client_session(mcp) as client:
            return await client.call_tool("run_committee", {"symbol": "AAPL"})

    res = asyncio.run(go())
    assert not res.isError
    out = res.structuredContent
    out = out.get("result", out)
    assert out["cached"] is True and out["verdict"] == "ACCUMULATE"
    assert out["input_health"]["rewritten"] == ["single_invest_cap"]
    assert out["cio_memo"].startswith("VERDICT: ACCUMULATE") and "钳制" in out["cio_memo"]
