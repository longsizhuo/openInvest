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
    # Coordinator 在 CIO 后面加了别的（含数字的）小节 → 照样在它前面截止，不吞
    for heading in ("EVENT_BRIEF", "QUANT_R3"):
        assert parse_committee_text(COORDINATOR_MD.replace("### QUANT\n", f"### {heading}\n"))[
            "cio_memo"] == "cio line 1\ncio line 2"
    assert parse_committee_text("**Verdict**: HOLD (confidence 0.5)\n")["cio_memo"] is None


@pytest.fixture
def mem_root(tmp_path, monkeypatch):
    """真写盘用：memory 根指向 tmp，行情空（macro 快照 / 现价都不触网）。"""
    import pandas as pd

    import openinvest.core.memory_store as ms
    import openinvest.utils.exchange_fee as ef
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **kw: pd.DataFrame())
    return tmp_path / "memory"


# 含内部分隔线 / 空行 / 小标题 / 钳制句——贴近真实 CIO memo，且不能被分节正则截断
_MEMO = ("VERDICT: ACCUMULATE\nCONFIDENCE: 0.7\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 800\n\n"
         "首笔 ¥800，分三批。\n\n---\n\n### 分批计划\n- 第一批\n- 第二批\n\n"
         "⚠️ 金额已按单次上限 ¥1,000 钳制")


def test_persist_roundtrip_cio_memo_verbatim(mem_root):
    """真跑 _persist 再用 parse_committee_text 读回：cio_memo 逐字等于写入的 memo。
    persist.py 改了分节标题 / 顺序，这里红，而不是 cached 路径悄悄返回 None 或截错。"""
    from datetime import datetime

    from openinvest.core.committee import persist
    from openinvest.core.committee.debate import CommitteeReport
    report = CommitteeReport(asset={"symbol": "TESTSYM", "display_name": "Test"},
                             macro_view="MACRO risk_on", quant_view="QUANT bullish",
                             risk_view="RISK ok", cio_memo=_MEMO)
    verdict = {"verdict": "ACCUMULATE", "confidence": 0.7, "dominant_view": "quant", "alloc_cny": 800}
    persist._persist(report, verdict, debate_meta={"max_rounds": 2, "final_round": 2, "converged": True})
    md = mem_root / ".committee" / datetime.now().strftime("%Y-%m-%d") / "TESTSYM.md"
    got = parse_committee_text(md.read_text(encoding="utf-8"))
    assert got["cio_memo"] == _MEMO
    assert got["verdict"] == "ACCUMULATE" and got["input_health"]["schema"] == 1   # 别的块没被吞


def test_coordinator_roundtrip_cio_memo_verbatim(mem_root):
    """同上，走 Coordinator 落盘（save_committee_transcript 的 `### CIO` 版式，后面还有 QUANT / RISK 小节）。"""
    from pathlib import Path

    from openinvest.core.runner.coordinator import save_committee_transcript
    raw = ("=== MACRO ===\n宏观中性\n\n=== QUANT_R1 ===\nSIGNAL: bullish\nSTRENGTH: 6\n\n"
           "=== RISK_R1 ===\nSIGNAL: concerned\nSTRENGTH: 5\n\n"
           f"=== CIO ===\n{_MEMO}\n\n"
           "=== QUANT ===\nSIGNAL: bullish\n\n=== RISK ===\nSIGNAL: concerned\n")
    saved = save_committee_transcript("TESTSYM", raw, provider="hermes")["saved"]
    text = Path(saved).read_text(encoding="utf-8")
    assert "### QUANT\n" in text and "### RISK\n" in text      # CIO 后面确有别的小节
    got = parse_committee_text(text)
    assert got["cio_memo"] == _MEMO
    assert "SIGNAL: bullish" not in got["cio_memo"]              # 没吞后面的小节


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
