"""Input Health 块 + 同日重跑归档 + 计分板诚实化（2026-10 评审 Sprint 1，Track A1/A2）

守四件事：
- persist 落盘带 Input Health JSON 块与 **Run at** 行；live 同日重跑把旧 transcript 归档到
  <date>/runs/ 而不是覆盖；parse_committee_text 能把块读回来
- rewrite_tags / forced_hold 从 parse_cio_memo 的 `_` 溯源键推回规则名（词汇与干预账本对齐）
- UNCLEAR 不再折进 HOLD 桶：review_one 不打 hits；summarize / export 只计数
- 每桶给同样本基率 / 提升 / Wilson 95% CI；n<30 时 lift / ci95 与 rate 一起抹掉
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import pytest

from openinvest.core.committee import cio_parse, persist
from openinvest.core.committee.debate import CommitteeReport
from openinvest.core.decision_ledger import parse_committee_text
from openinvest.core.runner.session import build_input_health_facts
from openinvest.jobs import review_calc as rc
from openinvest.jobs import verdict_review as vr


# ---------- rewrite_tags / forced_hold ----------

def test_rewrite_tags_vocabulary_matches_intervention_rules():
    assert cio_parse.rewrite_tags({"verdict": "HOLD"}) == []
    assert cio_parse.rewrite_tags(
        {"verdict": "HOLD", "_original_verdict": "ACCUMULATE", "_defense_downgrade": "accumulate_to_hold"}
    ) == ["defense_accumulate_to_hold"]
    assert cio_parse.rewrite_tags(
        {"verdict": "HOLD", "_original_verdict": "TRIM", "_sanity5_reason": "reentry_missing"}
    ) == ["sanity5_reentry_missing"]
    assert cio_parse.rewrite_tags(
        {"verdict": "ACCUMULATE", "_original_verdict": "BUY", "_original_confidence": 0.97}
    ) == ["sanity1_buy_overdrive"]
    assert cio_parse.rewrite_tags({"verdict": "ACCUMULATE", "_original_alloc": 500000}) == ["sanity2_alloc_clamp"]
    assert cio_parse.rewrite_tags(
        {"verdict": "HOLD", "_original_verdict": "ACCUMULATE", "_original_confidence_unavailable": 0.8}
    ) == ["sanity3_worker_unavailable"]
    assert cio_parse.rewrite_tags(
        {"verdict": "ACCUMULATE", "_defense_dca": "tranche", "_original_verdict": "ACCUMULATE",
         "_original_alloc": 3000}
    ) == ["defense_gold_dca_tranche"]


def test_rewrite_tags_recognises_single_invest_cap_from_pr_430():
    """PR #430 的 Sanity 6 只钳金额不改方向：_original_verdict == verdict、_original_alloc 变、
    带 _single_invest_cap 标记——必须标成 single_invest_cap，不能误标 sanity2_alloc_clamp。"""
    out = {"verdict": "ACCUMULATE", "alloc_cny": 10000, "_original_verdict": "ACCUMULATE",
           "_original_alloc": 25000, "_single_invest_cap": 10000}
    assert cio_parse.rewrite_tags(out) == ["single_invest_cap"]
    assert cio_parse.forced_hold(out) is False


def test_forced_hold_definition():
    assert cio_parse.forced_hold({"verdict": "HOLD", "_original_verdict": "TRIM"}) is True
    assert cio_parse.forced_hold({"verdict": "HOLD"}) is False                      # CIO 自己说 HOLD
    assert cio_parse.forced_hold({"verdict": "HOLD", "_original_verdict": "HOLD"}) is False
    assert cio_parse.forced_hold({"verdict": "ACCUMULATE", "_original_verdict": "BUY"}) is False


# ---------- persist：Input Health 块 + Run at + 同日归档 ----------

def _report(sym: str = "TEST.AX", risk_view: str = "RISK ok") -> CommitteeReport:
    return CommitteeReport(
        asset={"symbol": sym, "display_name": "Test"},
        macro_view="MACRO risk_on", quant_view="QUANT bullish", risk_view=risk_view,
        cio_memo="VERDICT: HOLD\nCONFIDENCE: 0.6",
    )


@pytest.fixture
def mem_root(tmp_path, monkeypatch):
    from openinvest.core import memory_store as ms
    root = tmp_path / "memory"
    root.mkdir()
    monkeypatch.setattr(ms, "MEMORY_ROOT", root)
    # 不触网：macro 快照拉 ^VIX 等走 get_history_data
    import openinvest.utils.exchange_fee as ef
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **kw: pd.DataFrame())
    return root


def test_build_input_health_merges_caller_facts_with_persist_facts():
    report = _report(risk_view="[WORKER_UNAVAILABLE] reason=timeout")
    verdict = {"verdict": "HOLD", "confidence": 0.4, "_original_verdict": "ACCUMULATE",
               "_original_confidence_unavailable": 0.8}
    h = persist.build_input_health(
        report, verdict, {"model": "m-test", "cio_json_mode": True},
        {"price_last_bar": "2026-10-16", "empty_inputs": ["valuation_brief"]},
    )
    assert h["schema"] == 1
    assert h["price_last_bar"] == "2026-10-16" and h["empty_inputs"] == ["valuation_brief"]
    assert h["worker_unavailable"] == ["risk"]
    assert h["model"] == "m-test" and h["cio_json_mode"] is True
    assert h["rewritten"] == ["sanity3_worker_unavailable"]
    assert h["forced_hold"] is True and h["cio_unclear"] is False


def test_persist_writes_health_block_and_archives_same_day_rerun(mem_root):
    verdict = {"verdict": "HOLD", "confidence": 0.62, "dominant_view": "quant", "alloc_cny": 0}
    persist._persist(_report(), verdict, debate_meta={"max_rounds": 2, "final_round": 2, "converged": False,
                                                      "model": "m1", "cio_json_mode": True},
                     input_health={"price_last_bar": "2026-10-16", "empty_inputs": []})
    date_dirs = [p for p in (mem_root / ".committee").iterdir() if p.is_dir()]
    assert len(date_dirs) == 1
    md = date_dirs[0] / "TEST_AX.md"
    text = md.read_text(encoding="utf-8")
    assert "**Run at**:" in text and "## Input Health (deterministic)" in text

    parsed = parse_committee_text(text)
    assert parsed["input_health"]["price_last_bar"] == "2026-10-16"
    assert parsed["input_health"]["rewritten"] == [] and parsed["input_health"]["forced_hold"] is False
    assert parsed["input_health"]["model"] == "m1"
    assert parsed["run_at"]
    # 老格式（无块）→ {} / None，不猜
    old = parse_committee_text("**Symbol**: X\n**Verdict**: HOLD (confidence 0.5)\n")
    assert old["input_health"] == {} and old["run_at"] is None

    # 同日重跑：旧 transcript 归档到 runs/，<sym>.md 换成新的，date 目录一层仍只有一个 *.md
    time.sleep(0.01)
    verdict2 = {"verdict": "ACCUMULATE", "confidence": 0.7, "dominant_view": "quant", "alloc_cny": 2000}
    persist._persist(_report(), verdict2)
    assert "**Verdict**: ACCUMULATE" in md.read_text(encoding="utf-8")
    runs = sorted((date_dirs[0] / persist.RUNS_SUBDIR).glob("TEST_AX_*.md"))
    assert len(runs) == 1 and "**Verdict**: HOLD" in runs[0].read_text(encoding="utf-8")
    assert [p.name for p in date_dirs[0].glob("*.md")] == ["TEST_AX.md"]

    # 第三次：归档目录里两份，互不覆盖
    time.sleep(0.01)
    persist._persist(_report(), verdict)
    assert len(list((date_dirs[0] / persist.RUNS_SUBDIR).glob("TEST_AX_*.md"))) == 2


def test_persist_backtest_output_dir_does_not_archive(mem_root, tmp_path):
    """研究回测（output_dir 指定）反复重写同日文件是有意的，不归档。"""
    out = tmp_path / "bt" / "2024-01-02"
    verdict = {"verdict": "HOLD", "confidence": 0.5, "dominant_view": "tie", "alloc_cny": 0}
    persist._persist(_report(), verdict, output_dir=out, date_override="2024-01-02")
    persist._persist(_report(), verdict, output_dir=out, date_override="2024-01-02")
    assert not (out / persist.RUNS_SUBDIR).exists()
    assert "**Run at**: 2024-01-02" in (out / "TEST_AX.md").read_text(encoding="utf-8")


# ---------- session：资产级事实 ----------

def test_build_input_health_facts_lists_empty_inputs_and_price_bar():
    df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.date_range("2026-10-15", periods=2))
    df.attrs["yf_fetch_failed"] = True
    facts = build_input_health_facts(
        df, event_brief="", sentiment_brief="VIX 20", valuation_brief="", prior_insights="",
        reentry_reference="ref", prob_hint=None,
        portfolio_summary="⚠️ portfolio_summary 完整版构建失败，请勿据此做集中度判断",
    )
    assert facts["price_last_bar"] == "2026-10-16" and facts["price_fetch_failed"] is True
    assert facts["empty_inputs"] == ["event_brief", "valuation_brief", "prior_insights",
                                     "prob_hint", "portfolio_summary"]
    ok = build_input_health_facts(
        df, event_brief="e", sentiment_brief="s", valuation_brief="v", prior_insights="p",
        reentry_reference="r", prob_hint={"n": 1}, portfolio_summary="总资产 ¥1",
    )
    assert ok["empty_inputs"] == []


# ---------- review_calc：Wilson / 降级判定 / UNCLEAR / 提升 ----------

def test_wilson_ci_known_values():
    assert rc.wilson_ci(0, 0) is None
    lo, hi = rc.wilson_ci(5, 10)
    assert lo == pytest.approx(0.237, abs=0.002) and hi == pytest.approx(0.763, abs=0.002)
    assert rc.wilson_ci(10, 10) == [0.722, 1.0]
    assert rc.wilson_ci(0, 10)[0] == 0.0


def test_is_degraded_input_only_on_critical_facts():
    assert rc.is_degraded_input({}) is False
    assert rc.is_degraded_input({"empty_inputs": ["valuation_brief", "prior_insights"]}) is False
    assert rc.is_degraded_input({"empty_inputs": ["sentiment_brief"]}) is True
    assert rc.is_degraded_input({"worker_unavailable": ["quant"]}) is True
    assert rc.is_degraded_input({"price_fetch_failed": True}) is True
    assert rc.is_degraded_input({"macro_data_empty": True}) is True


_RET = {"up": 0.10, "down": -0.10, "flat": 0.01}   # 与 directions 一致的事后收益（band 假定 8%）


def _rv(verdict: str, hit: bool | None, mkt: str, conf: float = 0.7, health=None) -> rc.VerdictReview:
    r = rc.VerdictReview(
        date="2025-06-02", asset="NDQ.AX", verdict=verdict, confidence=conf,
        expected_direction=rc.EXPECTED_DIRECTION.get(verdict, rc.UNCLEAR_DIRECTION),
        macro_at_decision={}, directions={"30d": mkt}, actual_returns={"30d": _RET[mkt]},
        input_health=health or {},
    )
    if hit is not None:
        r.hits = {"30d": hit}
    return r


def test_summarize_unclear_counted_not_scored_and_lift_vs_base_rate():
    # 基率样本池 = 进命中率的 37 条（3 条 UNCLEAR 不进）：flat 30/37；
    # ACCUMULATE 命中尺子是"涨 >0" → 基率 = 30 flat(+1%) + 5 up = 35/37（不是 directions 的 up）
    rows = [_rv("HOLD", True, "flat") for _ in range(30)]
    rows += [_rv("ACCUMULATE", True, "up") for _ in range(5)]
    rows += [_rv("UNCLEAR", None, "up", conf=0.0) for _ in range(3)]
    rows += [_rv("HOLD", False, "down", conf=0.3) for _ in range(2)]  # 低置信 HOLD（老存档启发式）
    s = rc.summarize_verdict_reviews(rows)
    b = s["holdout"]
    assert b["unclear_count"] == 3 and b["by_verdict"]["UNCLEAR"]["n"] == 3
    assert "hit_rate_30d" not in b["by_verdict"]["UNCLEAR"]
    hold = b["by_verdict"]["HOLD"]
    assert hold["n"] == 32 and hold["hit_rate_30d"] == pytest.approx(30 / 32, abs=1e-3)
    assert hold["base_rate_30d"] == pytest.approx(30 / 37, abs=1e-3)
    assert hold["lift_30d"] == pytest.approx(30 / 32 - 30 / 37, abs=2e-3)
    assert hold["ci95_30d"][0] < hold["hit_rate_30d"] < hold["ci95_30d"][1]
    acc = b["by_verdict"]["ACCUMULATE"]
    assert acc["base_rate_30d"] == pytest.approx(35 / 37, abs=1e-3)   # 同尺子：P(涨>0)，不是 P(up>band)
    assert acc["lift_30d"] == pytest.approx(1.0 - 35 / 37, abs=2e-3)
    assert b["by_window"]["30d"]["ci95"] is not None
    assert b["forced_hold_count"] == 2            # conf ≤ 0.4 的 HOLD（无 Input Health 块时的启发式）
    assert b["degraded_input_count"] == 0 and b["health_tagged_count"] == 0
    # 带 Input Health 块的样本：按 flag 算，不再猜
    tagged = [_rv("HOLD", True, "flat", conf=0.6, health={"forced_hold": True, "worker_unavailable": ["cio"]})]
    b2 = rc.summarize_verdict_reviews(rows + tagged)["holdout"]
    assert b2["forced_hold_count"] == 3 and b2["degraded_input_count"] == 1 and b2["health_tagged_count"] == 1
    # 报告渲染不炸且带标注行
    text = "\n".join(rc._bucket_lines("x", b2))
    assert "UNCLEAR | 3" in text and "强制/低置信 HOLD 3 条" in text and "30d 提升" in text


def test_review_one_unclear_has_no_hits(tmp_path, monkeypatch):
    date_dir = tmp_path / "2026-06-01"
    date_dir.mkdir()
    (date_dir / "X.md").write_text(
        "**Symbol**: X\n**Verdict**: UNCLEAR (confidence 0.00)\n"
        "## Input Health (deterministic)\n```json\n{\"cio_unclear\": true}\n```\n", encoding="utf-8")
    monkeypatch.setattr(vr, "_window_return", lambda *a, **k: 0.10)   # > flat band 8% → up
    monkeypatch.setattr(vr, "_detect_macro_shock", lambda *a, **k: {"detected": False, "drivers": []})
    monkeypatch.setattr(vr, "_decision_regime", lambda *a, **k: None)
    monkeypatch.setattr(vr, "_atr_pct_cached", lambda *a, **k: 2.0)
    rv = vr.review_one(date_dir, "X", {})
    assert rv.expected_direction == "none"
    assert rv.hits == {} and rv.actual_returns["30d"] == 0.10 and rv.directions["30d"] == "up"
    assert rv.input_health == {"cio_unclear": True}


# ---------- export：公开 JSON ----------

def test_export_unclear_bucket_and_lift_ci_suppression():
    from scripts.export_accuracy import _aggregate, _suppress_small_samples, HIT_HORIZON
    rows = [{"expected_direction": "flat", "hits": {HIT_HORIZON: True}, "directions": {HIT_HORIZON: "flat"},
             "actual_returns": {HIT_HORIZON: 0.01}} for _ in range(31)]
    rows += [{"expected_direction": "up", "hits": {HIT_HORIZON: False}, "directions": {HIT_HORIZON: "flat"},
              "actual_returns": {HIT_HORIZON: -0.01}} for _ in range(4)]
    rows += [{"expected_direction": "none", "hits": {}, "directions": {HIT_HORIZON: "up"},
              "actual_returns": {HIT_HORIZON: 0.2}}]
    rows += [{"expected_direction": "none", "hits": {}, "directions": {}}]  # 未成熟 UNCLEAR 不计
    agg = _aggregate(rows)
    assert agg["sample_size"] == 35                       # UNCLEAR 不进命中样本
    assert agg["by_direction"]["unclear"] == {"hit": None, "total": 1, "rate": None}
    hold = agg["by_direction"]["hold"]
    assert hold["base_rate"] == pytest.approx(35 / 35) and hold["lift"] == pytest.approx(0.0)
    assert hold["ci95"] == rc.wilson_ci(31, 31)
    # bullish 基率 = 进命中率的 35 条里 P(涨>0) = 31 (+1%) / 35，与 directions 的 up(> band)=0 不同
    assert agg["by_direction"]["bullish"]["base_rate"] == pytest.approx(31 / 35, abs=1e-3)
    assert agg["base_rate"]["up"] == pytest.approx(0.0)      # 市场方向基率口径不变（UNCLEAR 不进）

    supp = _suppress_small_samples(agg)
    assert supp["by_direction"]["hold"]["lift"] is not None          # n=31 ≥ 30 保留
    bull = supp["by_direction"]["bullish"]
    assert bull["rate"] is None and bull["hit"] is None and bull["lift"] is None and bull["ci95"] is None
    assert supp["by_direction"]["unclear"]["total"] == 1


def test_export_public_fixture_has_no_sensitive_keys(tmp_path):
    """新键（base_rate / lift / ci95 / unclear）不得撞 CI 红线 grep 的敏感字段。"""
    from scripts.export_accuracy import build_summary
    jsonl = tmp_path / "v.jsonl"
    jsonl.write_text(json.dumps({"date": "2026-05-01", "asset": "NDQ.AX", "expected_direction": "none",
                                 "hits": {}, "directions": {"30d": "up"}, "source": "live"}) + "\n")
    out = json.dumps(build_summary(jsonl))
    for kw in ('"symbol"', '"threshold"', '"verdict_id"', '"asset"', "NDQ", "GC=F"):
        assert kw not in out


# ---- CR #431 建议 1/2/4：归档时机、并发撞车、同秒后缀 ----

def test_persist_failure_before_write_keeps_previous_transcript(mem_root, monkeypatch):
    """拼块途中（macro 快照拉行情）抛异常 → 当天 <sym>.md 必须原样还在，runs/ 不该出现。
    旧行为是直接覆盖，出错时旧件还在；归档不能比旧行为更糟（缓存命中 / 复盘都靠这个文件）。"""
    verdict = {"verdict": "HOLD", "confidence": 0.5, "dominant_view": "tie", "alloc_cny": 0}
    persist._persist(_report(), verdict)
    date_dir = next(p for p in (mem_root / ".committee").iterdir() if p.is_dir())
    md = date_dir / "TEST_AX.md"
    before = md.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(persist, "_capture_macro_context", boom)
    with pytest.raises(RuntimeError):
        persist._persist(_report(), {**verdict, "verdict": "ACCUMULATE"})
    assert md.read_text(encoding="utf-8") == before
    assert not (date_dir / persist.RUNS_SUBDIR).exists()


def test_archive_tolerates_concurrent_rename(tmp_path, monkeypatch):
    """两个进程同时跑同一标的：后到的 replace 撞 FileNotFoundError → 当没有旧件（返回 None），
    不抛、不让四个角色的 LLM 调用白跑。"""
    md = tmp_path / "X.md"
    md.write_text("old", encoding="utf-8")

    def gone(self, target):
        raise FileNotFoundError(self)
    monkeypatch.setattr(Path, "replace", gone)
    assert persist._archive_previous_run(md) is None


def test_archive_same_second_gets_numeric_suffix(tmp_path):
    """同一秒连跑两次：第二份归档带 _2 后缀，绝不覆盖第一份。"""
    import os
    md = tmp_path / "X.md"
    fixed = 1_800_000_000  # 固定 mtime → 同一个 HHMMSS
    md.write_text("run1", encoding="utf-8"); os.utime(md, (fixed, fixed))
    t1 = persist._archive_previous_run(md)
    md.write_text("run2", encoding="utf-8"); os.utime(md, (fixed, fixed))
    t2 = persist._archive_previous_run(md)
    assert t1.name != t2.name and t2.name.endswith("_2.md")
    assert t1.read_text(encoding="utf-8") == "run1" and t2.read_text(encoding="utf-8") == "run2"


# ---- CR #431 建议 3：Coordinator 路径也带 Input Health + 归档 ----

_COORD_RAW = """
=== MACRO ===
宏观中性

=== QUANT_R1 ===
SIGNAL: bullish
STRENGTH: 6

=== RISK_R1 ===
[WORKER_UNAVAILABLE] reason=timeout

=== CIO ===
VERDICT: HOLD
CONFIDENCE: 0.6
DOMINANT_VIEW: risk
SUGGESTED_ALLOC_CNY: 0
"""


def test_coordinator_save_writes_health_block_and_archives(mem_root, monkeypatch):
    from openinvest.core.runner.coordinator import save_committee_transcript
    r1 = save_committee_transcript("TESTSYM", _COORD_RAW, provider="hermes")
    text = Path(r1["saved"]).read_text(encoding="utf-8")
    assert "**Run at**:" in text and "## Input Health (deterministic)" in text
    parsed = parse_committee_text(text)
    h = parsed["input_health"]
    assert h["entry"] == "coordinator" and h["provider"] == "hermes"
    assert h["worker_unavailable"] == ["risk"]
    # CIO 自己就说 HOLD，Sanity 3 只压了置信度/金额：有改写标记，但不算"被强制成 HOLD"
    assert "sanity3_worker_unavailable" in h["rewritten"] and h["forced_hold"] is False
    assert parsed["verdict"] == "HOLD"
    # 同日重跑 → 归档
    time.sleep(0.01)
    save_committee_transcript("TESTSYM", _COORD_RAW)
    runs = list((Path(r1["saved"]).parent / persist.RUNS_SUBDIR).glob("TESTSYM_*.md"))
    assert len(runs) == 1
