"""run_committee 命中当日缓存：CLI 不再要求 LLM key；MCP 缓存返回带 memo 与 Input Health。

e2e 实测（2026-10-17）：CLI 的 key 闸先于缓存检查——今天已有 transcript 也读不了；
MCP 缓存返回缺 memo / Input Health，agent 得再调一次 explain_decision。
"""
from __future__ import annotations
from tests.integrity_helpers import context_id, bind_cached_transcript

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
    p.write_text(bind_cached_transcript(tmp_path/"memory", "AAPL", text), encoding="utf-8")
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
    verdict = {"verdict": "ACCUMULATE", "confidence": 0.7, "dominant_view": "quant", "alloc_cny": 800,"reasoning_review":{"status":"passed","origin":"synthetic_unit_fixture"}}
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
    saved = save_committee_transcript("TESTSYM", raw, provider="hermes", context_id=context_id("TESTSYM"))["saved"]
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
    md.write_text(bind_cached_transcript(tmp_path/"memory", "AAPL", PERSIST_MD), encoding="utf-8")

    from pathlib import Path
    original_read=Path.read_text
    reads=[]
    def tracked_read(path,*a,**kw):
        if path == md: reads.append(path)
        return original_read(path,*a,**kw)
    monkeypatch.setattr(Path,"read_text",tracked_read)
    cc.cmd_run_committee(argparse.Namespace(symbol="AAPL", force=False, max_rounds=1))
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "cached" and "Input Health" in out["transcript_md"]
    assert len(reads)==1, "Return the same validated text snapshot; do not reread mutable latest"


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


UNCLEAR_MD = PERSIST_MD.replace("**Verdict**: ACCUMULATE (confidence 0.70)", "**Verdict**: UNCLEAR (confidence 0.00)").replace(
    '{"empty_inputs": ["event_brief"], "rewritten": ["single_invest_cap"]}',
    '{"worker_unavailable": ["macro", "quant", "risk", "cio"], "cio_unclear": true}')


def _call_mcp(args):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openinvest.connectors.mcp_server import mcp

    async def go():
        async with create_connected_server_and_client_session(mcp) as client:
            return await client.call_tool("run_committee", args)
    return asyncio.run(go())


def test_mcp_failed_transcript_is_not_served_as_cache(monkeypatch, tmp_path):
    """当天已有 UNCLEAR 记录（LLM 没跑成）→ 不当缓存，重跑。"""
    _seed(tmp_path, monkeypatch, text=UNCLEAR_MD)
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})
    calls = []

    def fake_session(**kw):
        calls.append(kw)
        p = tmp_path / "memory" / ".committee" / datetime.now().strftime("%Y-%m-%d") / "AAPL.md"
        p.write_text(PERSIST_MD, encoding="utf-8")   # 重跑成功落盘
        return {"asset_committees": {"AAPL": {"verdict": {"verdict": "ACCUMULATE", "confidence": 0.7}}}}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session", fake_session)
    res = _call_mcp({"symbol": "AAPL"})
    assert not res.isError and len(calls) == 1
    out = res.structuredContent
    out = out.get("result", out)
    assert out["cached"] is False and out["verdict"]["verdict"] == "ACCUMULATE" and "degraded" not in out


def test_mcp_fresh_unclear_is_an_error(monkeypatch, tmp_path):
    """没跑成（UNCLEAR）→ isError，不再当成功结果返回（2026-10-09 Codex 实测 402 时 isError=false）。"""
    _seed(tmp_path, monkeypatch, text=UNCLEAR_MD)
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})
    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session",
                        lambda **kw: {"asset_committees": {"AAPL": {"verdict": {"verdict": "UNCLEAR", "confidence": 0}}}})
    res = _call_mcp({"symbol": "AAPL", "force": True})
    assert res.isError
    assert "没跑成" in res.content[0].text and "doctor" in res.content[0].text


DEGRADED_MD = PERSIST_MD.replace(
    '{"empty_inputs": ["event_brief"], "rewritten": ["single_invest_cap"]}',
    '{"worker_unavailable": ["risk"], "forced_hold": false}')


def test_mcp_degraded_transcript_is_retried_after_source_recovery(monkeypatch, tmp_path):
    path=_seed(tmp_path,monkeypatch,text=DEGRADED_MD)
    calls=[]
    def recovered(**kw):
        calls.append(kw);path.write_text(PERSIST_MD)
        return {"asset_committees":{"AAPL":{"verdict":{"verdict":"ACCUMULATE","confidence":.7}}}}
    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session",recovered)
    res=_call_mcp({"symbol":"AAPL"})
    out=res.structuredContent;out=out.get("result",out)
    assert not res.isError and not out["cached"] and len(calls)==1


def test_mcp_fresh_degraded_returns_verdict_with_flag(monkeypatch, tmp_path):
    p = _seed(tmp_path, monkeypatch, text=UNCLEAR_MD)
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})

    def fake_session(**kw):
        p.write_text(DEGRADED_MD, encoding="utf-8")
        return {"asset_committees": {"AAPL": {"verdict": {"verdict": "ACCUMULATE", "confidence": 0.7}}}}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session", fake_session)
    res = _call_mcp({"symbol": "AAPL", "force": True})
    out = res.structuredContent
    out = out.get("result", out)
    assert not res.isError and out["verdict"]["verdict"] == "ACCUMULATE" and "risk" in out["degraded"]


def _cli_env(monkeypatch, tmp_path, text):
    from openinvest.skill_cmds import committee_cmds as cc

    class FakePM:
        strategy = {"target_assets": [{"symbol": "AAPL", "target_pct": 1.0}]}

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.setattr("openinvest.core.portfolio_manager.PortfolioManager", lambda: FakePM())
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})
    md = tmp_path / "memory" / ".committee" / datetime.now().strftime("%Y-%m-%d") / "AAPL.md"
    md.parent.mkdir(parents=True)
    md.write_text(bind_cached_transcript(tmp_path/"memory", "AAPL", text), encoding="utf-8")
    return cc, md


def test_cli_unclear_cache_reruns_and_fresh_unclear_exits_1(monkeypatch, capfd, tmp_path):
    cc, md = _cli_env(monkeypatch, tmp_path, UNCLEAR_MD)
    calls = []

    def fake_session(**kw):
        calls.append(kw)
        return {"asset_committees": {"AAPL": {"verdict": {"verdict": "UNCLEAR", "confidence": 0}}}}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session", fake_session)
    with pytest.raises(SystemExit) as e:
        cc.cmd_run_committee(argparse.Namespace(symbol="AAPL", force=False, max_rounds=1))
    assert e.value.code == 1 and len(calls) == 1          # UNCLEAR 缓存没被命中，重跑了
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "error" and "没跑成" in out["error"]


def test_cli_degraded_cache_is_retried_after_source_recovery(monkeypatch, capfd, tmp_path):
    cc,md=_cli_env(monkeypatch,tmp_path,DEGRADED_MD)
    calls=[]
    def recovered(**kw):
        calls.append(kw);md.write_text(PERSIST_MD)
        return {"asset_committees":{"AAPL":{"verdict":{"verdict":"ACCUMULATE","confidence":.7}}}}
    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session",recovered)
    cc.cmd_run_committee(argparse.Namespace(symbol="AAPL",force=False,max_rounds=1))
    out=json.loads(capfd.readouterr().out)
    assert out["status"] == "ok" and len(calls)==1


@pytest.fixture(autouse=True)
def _synthetic_review_for_non_review_contract(monkeypatch):
    # These tests isolate storage/cache/format contracts; dedicated review-flow
    # tests exercise actual verification, unavailable capability and failures.
    monkeypatch.setattr("openinvest.services.reasoning_review.review_once",
        lambda *args,**kwargs:{"status":"passed","origin":"synthetic_unit_fixture"})
