"""#234-5 契约：Coordinator save 与 Direct 路径的确定性后处理逐位一致

同一份 CIO memo + 同一组确定性输入（regime_brief / INDEP_DEFENSE_FLAG / 行情）分别走
Direct（session.run_committee_for_symbol → debate.run_committee，LLM 全 stub）与
Coordinator（coordinator.save_committee_transcript），最终 verdict 与 interventions.jsonl
落账行必须完全相同。旧 Coordinator 不传 current_price / defense_dca、不记账：
黄金 ACCUMULATE 遇防御 Direct 放行 ⅓ 批并记账、Coordinator 静默全拦且账本无行；
TRIM 买回价 ≥ 现价 Direct 被 Sanity5 打回 HOLD、Coordinator 原样放行。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

SYM = "GC=F"

_STRATEGY = f"""---
name: strategy
type: strategy
schema_version: 1
target_assets:
  - symbol: {SYM}
    display_name: Gold
    type: metal
    channel: direct
    max_single_invest_cny: 10000
---
"""
_PORTFOLIO = """---
schema_version: 2
cash:
  CNY: 100000.0
holdings: []
name: portfolio
type: state
---
"""

_ACCUMULATE = (
    "VERDICT: ACCUMULATE\nCONFIDENCE: 0.6\nDOMINANT_VIEW: quant\n"
    "SUGGESTED_ALLOC_CNY: 9000"
)
_TRIM_REENTRY_ABOVE = (
    "VERDICT: TRIM\nCONFIDENCE: 0.7\nDOMINANT_VIEW: risk\n"
    "SUGGESTED_ALLOC_CNY: -5000\nTRIM_REASON: bearish\nREENTRY_PRICE: 999999"
)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "user.md").write_text("---\nname: user\ntype: profile\nschema_version: 1\n---\n")
    (mem / "strategy.md").write_text(_STRATEGY)
    (mem / "portfolio.md").write_text(_PORTFOLIO)
    from openinvest.core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)

    idx = pd.bdate_range("2025-01-01", periods=300)
    df = pd.DataFrame({"Close": 100.0 + np.arange(300) * 0.1}, index=idx)
    import openinvest.utils.exchange_fee as ef
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **k: df)
    monkeypatch.setattr("openinvest.core.runner.session.get_history_data", lambda *a, **k: df)
    monkeypatch.setattr("openinvest.core.runner.session.analyze_multi_timeframe",
                        lambda *a, **k: "MOCK_MARKET")
    import openinvest.core.regime_probability as rp
    monkeypatch.setattr(rp, "get_regime_forward_summary", lambda *a, **k: None)
    monkeypatch.setattr(rp, "build_reentry_reference", lambda *a, **k: ("", None))
    monkeypatch.setattr("openinvest.core.committee.debate._persist", lambda *a, **k: None)

    from openinvest.core.regime import format_regime_brief
    from openinvest.utils.market_metrics import compute_metrics
    regime_brief = format_regime_brief(compute_metrics(df), symbol=SYM, prob_hint=None)
    return mem, regime_brief


def _ledger(mem):
    p = mem / ".dreams" / "interventions.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    if p.exists():
        p.unlink()   # 两路径须看到同一账本状态（DCA 闸读历史分批）
    return rows


def _run_direct(monkeypatch, memo, sentiment):
    class _Agent:
        def __init__(self, role):
            self.role = role
            self.last_tool_calls = []

        def run(self, ctx):
            if self.role == "cio":
                return memo
            return "SIGNAL: neutral\nSTRENGTH: 3\nONE_LINER: stub\n"

        def tool_call_summary(self):
            return ""

    monkeypatch.setattr("openinvest.core.committee.debate._create_agent",
                        lambda _p, **kw: _Agent(kw.get("role")))
    from openinvest.core.runner.session import run_committee_for_symbol
    res = run_committee_for_symbol(
        SYM, max_debate_rounds=1, shared_macro_view="MOCK_MACRO", event_brief="",
        portfolio_summary_override="MOCK_PORTFOLIO", prior_insights_override="",
        sentiment_brief=sentiment, valuation_brief_override="",
    )
    return res["verdict"]


def _run_coordinator(memo, sentiment, regime_brief):
    from openinvest.core.runner.coordinator import save_committee_transcript
    raw = (
        "=== MACRO ===\nMOCK_MACRO\n\n"
        f"=== QUANT_R1 ===\n{regime_brief}\n\n{sentiment}\nSIGNAL: neutral\n\n"
        "=== RISK_R1 ===\nSIGNAL: neutral\n\n"
        f"=== CIO ===\n{memo}\n"
    )
    return save_committee_transcript(SYM, raw)["verdict"]


@pytest.mark.parametrize("memo,sentiment,expect_rule", [
    (_ACCUMULATE, "INDEP_DEFENSE_FLAG: on", "defense_gold_dca_tranche"),
    (_TRIM_REENTRY_ABOVE, "INDEP_DEFENSE_FLAG: off", "sanity5_reentry_not_below_current"),
], ids=["gold_defense_dca", "trim_sanity5"])
def test_coordinator_matches_direct_postprocess(world, monkeypatch, memo, sentiment, expect_rule):
    mem, regime_brief = world
    v_direct = _run_direct(monkeypatch, memo, sentiment)
    rows_direct = _ledger(mem)
    v_coord = _run_coordinator(memo, sentiment, regime_brief)
    rows_coord = _ledger(mem)

    assert [r["rule"] for r in rows_direct] == [expect_rule]   # 场景确实触发了干预
    assert v_coord == v_direct
    assert rows_coord == rows_direct


def test_coordinator_catches_worker_failure_outside_cio_section(world):
    """worker 失败哨兵在 QUANT 段、不在 CIO 段：coordinator 也得强制 HOLD（cio_text 只剩 CIO 段，
    靠 worker_brief=raw 才看得到）；CIO 段的否定句提及不算失败。"""
    _, regime_brief = world
    from openinvest.core.committee.agent_io import AGENT_UNAVAILABLE_MARKER
    from openinvest.core.runner.coordinator import save_committee_transcript
    memo = "VERDICT: BUY\nCONFIDENCE: 0.9\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 5000"
    raw = (
        "=== MACRO ===\nMOCK_MACRO\n\n"
        f"=== QUANT_R1 ===\n{AGENT_UNAVAILABLE_MARKER} reason=retry_exhausted\n\n"
        "=== RISK_R1 ===\nSIGNAL: neutral\n\n"
        f"=== CIO ===\n{memo}\n"
    )
    v = save_committee_transcript(SYM, raw)["verdict"]
    assert v["verdict"] == "HOLD" and v["confidence"] <= 0.4 and v["alloc_cny"] == 0

    ok = save_committee_transcript(SYM, raw.replace(
        f"{AGENT_UNAVAILABLE_MARKER} reason=retry_exhausted",
        f"SIGNAL: bullish\n无 {AGENT_UNAVAILABLE_MARKER} 标记"))["verdict"]
    assert "_original_confidence_unavailable" not in ok
