"""Coordinator 收卷用 prepare 时后端存下的确定性事实（2026-10-18 插件不持 key / P1）。

守：防御 / regime 判定不再依赖 agent 把 INDEP_DEFENSE_FLAG / INPUTS 行抄进 transcript；
解析不出裁决的 CIO 不存档；Coordinator 记录补齐宏观快照与路径快照；MCP 出题/收卷可用。
跑：uv run pytest tests/test_coordinator_facts.py -q
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from openinvest.core.decision_ledger import parse_committee_text
from openinvest.core.runner import coordinator as co
from tests.integrity_helpers import context_id

CIO = "VERDICT: ACCUMULATE\nCONFIDENCE: 0.7\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 800\n首笔 ¥800。"
RAW = (f"=== MACRO ===\n宏观中性\n\n=== QUANT_R1 ===\nSIGNAL: bullish\n\n"
       f"=== RISK_R1 ===\nSIGNAL: ok\n\n=== CIO ===\n{CIO}\n")   # 故意不抄任何确定性行
REGIME_BRIEF = "REGIME: downtrend\nINPUTS: atr_spike_ratio=1.0"
SENTIMENT_BRIEF = "INDEP_DEFENSE_FLAG: on"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import openinvest.core.committee.persist as persist
    import openinvest.core.memory_store as ms
    import openinvest.utils.exchange_fee as ef
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(persist, "_capture_macro_context", lambda **kw: {"vix": 21.5})
    seen = {}
    real = co.parse_cio_memo

    def spy(text, **kw):
        seen.update(kw)
        return real(text, **kw)

    monkeypatch.setattr(co, "parse_cio_memo", spy)
    return tmp_path / "memory", seen


def _health(saved):
    return parse_committee_text(open(saved, encoding="utf-8").read())["input_health"]


def test_saved_facts_drive_defense_and_regime(env):
    mem, seen = env
    cid = context_id("TESTSYM", regime_brief=REGIME_BRIEF, sentiment_brief=SENTIMENT_BRIEF,
                     path_profile={"p_down_30": 0.4}, atr_pct=1.2)
    res = co.save_committee_transcript("TESTSYM", RAW, provider="hermes", context_id=cid)
    assert seen["defense_flag_on"] is True and seen["regime"] == "downtrend"
    assert _health(res["saved"])["facts_source"] == "prepare"
    got = parse_committee_text(open(res["saved"], encoding="utf-8").read())
    assert got["macro_at_decision"] == {"vix": 21.5}           # 宏观快照补齐
    day = datetime.now().strftime("%Y-%m-%d")
    snap = json.loads((mem / ".committee" / day / "TESTSYM_path.json").read_text())
    assert snap["profile"] == {"p_down_30": 0.4} and snap["regime"] == "downtrend"


def test_missing_context_never_falls_back_to_transcript(env):
    mem, seen = env
    with pytest.raises(ValueError, match="DATA_UNTRUSTED"):
        co.save_committee_transcript("TESTSYM", RAW)
    assert not seen


def test_unparseable_cio_is_rejected_and_not_saved(env):
    mem, _ = env
    with pytest.raises(ValueError, match="UNCLEAR"):
        co.save_committee_transcript("TESTSYM", "=== CIO ===\n我觉得还行\n", context_id=context_id("TESTSYM"))
    assert not list((mem / ".committee").rglob("TESTSYM.md"))


def _call_mcp(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openinvest.connectors.mcp_server import mcp

    async def go():
        async with create_connected_server_and_client_session(mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def test_mcp_submit_committee(env):
    cid = context_id("TESTSYM", regime_brief=REGIME_BRIEF, sentiment_brief=SENTIMENT_BRIEF)
    res = _call_mcp("submit_committee", {"symbol": "TESTSYM", "transcript": RAW, "provider": "hermes",
                                         "model": "claude-haiku-5-5", "context_id": cid})
    assert not res.isError
    out = res.structuredContent
    out = out.get("result", out)
    assert out["facts_source"] == "prepare" and out["decision_id"].endswith("/TESTSYM")
    # 传入的模型名落进存档 Input Health（复盘 model_era = provider:model）；没传记 unknown，不猜
    from pathlib import Path

    from openinvest.jobs.review_calc import model_era_of
    h = parse_committee_text(Path(out["saved"]).read_text(encoding="utf-8"))["input_health"]
    assert h["model"] == "claude-haiku-5-5" and model_era_of(h, "2026-10-20") == "hermes:claude-haiku-5-5"
    res2 = _call_mcp("submit_committee", {"symbol": "TESTSYM2", "transcript": RAW, "provider": "hermes", "context_id": context_id("TESTSYM2")})
    out2 = res2.structuredContent
    out2 = out2.get("result", out2)
    h2 = parse_committee_text(Path(out2["saved"]).read_text(encoding="utf-8"))["input_health"]
    assert h2["model"] == "unknown" and model_era_of(h2, "2026-10-20") == "hermes:unknown"
    bad = _call_mcp("submit_committee", {"symbol": "TESTSYM", "transcript": "=== CIO ===\n还行\n", "context_id": cid})
    assert bad.isError and "different content" in bad.content[0].text


def test_invalid_resubmit_preserves_existing_evidence(env):
    mem, _ = env
    cid = context_id("TESTSYM", regime_brief=REGIME_BRIEF, sentiment_brief=SENTIMENT_BRIEF,
                     path_profile={"p_down_30": 0.4})
    co.save_committee_transcript("TESTSYM", RAW, context_id=cid)
    snap = mem / ".committee" / datetime.now().strftime("%Y-%m-%d") / "TESTSYM_path.json"
    assert snap.exists()
    before = snap.read_bytes()
    (mem / ".evidence/contexts" / f"{cid}.json").unlink()
    replay = co.save_committee_transcript("TESTSYM", RAW, context_id=cid)
    assert replay["replayed"] and not replay["current_advice_valid"]
    assert snap.read_bytes() == before


def test_submit_writes_daily_sidecar_and_loader_reads_it(env):
    mem, _ = env
    cid = context_id("TESTSYM", regime_brief=REGIME_BRIEF, sentiment_brief=SENTIMENT_BRIEF,
                     path_reference="- 30d 回踩中位 -4%", path_profile={"x": 1})
    co.save_committee_transcript("TESTSYM", RAW, provider="hermes", context_id=cid)
    day = datetime.now().strftime("%Y-%m-%d")
    rec = co.load_submitted_committee("TESTSYM", day)
    assert rec["verdict"]["verdict"] in ("ACCUMULATE", "HOLD")      # 规则后裁决（防御开着可能降级）
    assert rec["report"].macro_view == "宏观中性" and "VERDICT" in rec["report"].cio_memo
    assert rec["path_reference"] == "- 30d 回踩中位 -4%" and rec["path_profile"] == {"x": 1}
    assert co.load_submitted_committee("OTHER", day) is None


def test_sidecar_keeps_numpy_numbers_numeric(env, monkeypatch):
    import numpy as np
    real = co.parse_cio_memo
    monkeypatch.setattr(co, "parse_cio_memo", lambda t, **kw: {**real(t, **kw), "confidence": np.float64(0.66),
                                                              "alloc_cny": np.int64(800)})
    co.save_committee_transcript("TESTSYM", RAW, context_id=context_id("TESTSYM"))
    rec = co.load_submitted_committee("TESTSYM", datetime.now().strftime("%Y-%m-%d"))
    assert isinstance(rec["verdict"]["confidence"], float) and f"{rec['verdict']['confidence']:.2f}" == "0.66"
    assert rec["verdict"]["alloc_cny"] == 800


@pytest.fixture(autouse=True)
def _synthetic_review_for_non_review_contract(monkeypatch):
    # These tests isolate storage/cache/format contracts; dedicated review-flow
    # tests exercise actual verification, unavailable capability and failures.
    monkeypatch.setattr("openinvest.services.reasoning_review.review_once",
        lambda *args,**kwargs:{"status":"passed","origin":"synthetic_unit_fixture"})
