"""B4 密封投票实验臂（评审路线 B4）——舰队侧契约测试。

守三件事：
- 聚合规则（多数 / 平票 / 无多数 / 多数票金额中位数）是纯函数，边界写死
- run_one_day(votes=N)：同一份输入抽 N 次、落 Input Health(arm/votes/flip/split)、同日跑过就跳过、
  任一次抽样失败整标的不落盘；votes=1 的对照臂落盘格式不变
- main() 把臂接在对照臂之后、借对照臂的 macro_view；--sealed-arm 只能配 --prospective

复盘侧（review_arm / run_arm / 不污染 live）见 tests/test_arm_review.py。不联网、不调 LLM。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from openinvest.core.committee.debate import CommitteeReport
from openinvest.core.decision_ledger import parse_committee_text
from openinvest.jobs.verdict_review import SEALED_ARM_SUBDIR
from scripts import backtest_committee as bc


def V(verdict: str, alloc: float = 0.0, conf: float = 0.62) -> Dict[str, Any]:
    return {"verdict": verdict, "confidence": conf, "alloc": alloc}


# ---------- 聚合规则 ----------

def test_aggregate_majority_confidence_and_median_alloc():
    agg = bc.aggregate_votes([V("ACCUMULATE", 3000), V("HOLD"), V("ACCUMULATE", 5000)])
    assert agg["verdict"] == "ACCUMULATE" and agg["split"] is False
    assert agg["confidence"] == pytest.approx(0.67)          # 多数票占比 2/3
    assert agg["alloc"] == 4000                               # 多数票两次的中位数（偶数个取中间两数均值）
    assert agg["flip"] is True                                # up vs flat：方向类不全相同
    assert agg["rep"] == 0                                    # 代表票 = 多数票里最早的一次

    # 3 票里 2 票是多数；非多数那票的金额不进中位数
    agg = bc.aggregate_votes([V("HOLD"), V("TRIM", -2000), V("TRIM", -6000)])
    assert (agg["verdict"], agg["alloc"], agg["rep"]) == ("TRIM", -4000, 1)

    # 奇数个多数票：中位数是中间那个，不被极端值拖动
    agg = bc.aggregate_votes([V("ACCUMULATE", 1000), V("ACCUMULATE", 2000), V("ACCUMULATE", 90000)])
    assert (agg["alloc"], agg["confidence"], agg["flip"]) == (2000, 1.0, False)


def test_aggregate_unanimous_hold_has_zero_alloc_and_full_agreement():
    agg = bc.aggregate_votes([V("HOLD"), V("HOLD"), V("HOLD")])
    assert agg == {"verdict": "HOLD", "confidence": 1.0, "alloc": 0, "split": False, "flip": False, "rep": 0}
    # HOLD 多数里即使有票带了金额，聚合后 HOLD 恒 0
    assert bc.aggregate_votes([V("HOLD", 500), V("HOLD", 700), V("BUY", 100)])["alloc"] == 0


def test_aggregate_three_way_tie_is_hold_and_split():
    agg = bc.aggregate_votes([V("ACCUMULATE", 3000), V("BUY", 4000), V("TRIM", -1000)])
    assert agg["verdict"] == "HOLD" and agg["split"] is True and agg["alloc"] == 0
    assert agg["confidence"] == pytest.approx(0.33)           # 最大票占比 1/3
    assert agg["flip"] is True and agg["rep"] == 0
    # N=4 的 2:2 同样没有严格多数；N=5 的 2:2:1 也是
    assert bc.aggregate_votes([V("BUY", 1), V("BUY", 1), V("HOLD"), V("HOLD")])["split"] is True
    assert bc.aggregate_votes([V("BUY", 1), V("BUY", 1), V("HOLD"), V("HOLD"), V("TRIM", -1)])["split"] is True
    # N=5 的 3:1:1 有严格多数
    agg = bc.aggregate_votes([V("BUY", 1), V("BUY", 3), V("BUY", 2), V("HOLD"), V("TRIM", -1)])
    assert (agg["verdict"], agg["split"], agg["confidence"], agg["alloc"]) == ("BUY", False, 0.6, 2)


def test_aggregate_flip_uses_direction_classes_like_a4():
    # BUY 和 ACCUMULATE 都是 up：标签不同但方向类相同 → 不算翻转（与 A4 口径一致）
    assert bc.aggregate_votes([V("BUY", 1), V("ACCUMULATE", 1), V("BUY", 1)])["flip"] is False
    # UNCLEAR 是独立的 "none" 类，与 HOLD 的 flat 不同
    agg = bc.aggregate_votes([V("UNCLEAR"), V("UNCLEAR"), V("HOLD")])
    assert agg["verdict"] == "UNCLEAR" and agg["flip"] is True


# ---------- run_one_day(votes=N) ----------

DAY = "2020-02-14"


def _install_stubs(monkeypatch, tmp_path, verdict_script: List[Dict[str, Any]]):
    """种子行情 + 桩掉 LLM：返回记录 run_committee 入参的 calls 列表。"""
    import openinvest.core.committee as committee
    import openinvest.core.committee.persist as persist
    import openinvest.core.memory_store as mem_mod
    import openinvest.db.market_store as ms_mod
    import openinvest.utils.exchange_fee as ef

    monkeypatch.setattr(mem_mod, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market.db"))
    store = ms_mod.MarketStore()
    for i, d in enumerate(pd.bdate_range("2019-06-03", DAY)):
        store.save_generic_price("AAPL", d.strftime("%Y-%m-%d"), 100.0 + 0.5 * i,
                                 high=101.0 + 0.5 * i, low=99.0 + 0.5 * i, volume=1e6)
    monkeypatch.setattr(ef, "_STORE", store)
    monkeypatch.setattr(ef, "get_macro_data", lambda: "macro-data")
    monkeypatch.setattr(persist, "_capture_macro_context", lambda as_of_date=None: {"captured_at": as_of_date})

    macro_calls: List[Any] = []
    monkeypatch.setattr(committee, "run_macro_view",
                        lambda data, **kw: macro_calls.append(data) or "MACRO-VIEW-FROM-CONTROL")
    calls: List[Dict[str, Any]] = []
    script = list(verdict_script)

    def fake_run_committee(**kw):
        calls.append(kw)
        step = script[len(calls) - 1]
        if isinstance(step, Exception):
            raise step
        verdict = {"verdict": step["verdict"], "confidence": step.get("confidence", 0.62),
                   "dominant_view": "quant", "alloc_cny": step.get("alloc", 0)}
        verdict.update(step.get("extra", {}))
        report = CommitteeReport(asset=kw["asset"], macro_view=kw["macro_view"],
                                 quant_view=f"QUANT#{len(calls)}", risk_view=f"RISK#{len(calls)}",
                                 cio_memo=f"CIO MEMO#{len(calls)}")
        return {"asset": kw["asset"]["symbol"], "verdict": verdict, "report": report,
                "debate": {"max_rounds": 1, "final_round": 1, "converged": False,
                           "quant_history": [], "risk_history": [], "model": "m-test",
                           "cio_json_mode": True}}

    monkeypatch.setattr(committee, "run_committee", fake_run_committee)
    return calls, macro_calls


def _sealed_md(root: Path, sym: str = "AAPL") -> Path:
    return root / "memory" / SEALED_ARM_SUBDIR / DAY / f"{sym}.md"


def test_run_one_day_sealed_aggregates_and_writes_input_health(monkeypatch, tmp_path):
    calls, macro_calls = _install_stubs(monkeypatch, tmp_path, [
        {"verdict": "ACCUMULATE", "alloc": 3000}, {"verdict": "HOLD"}, {"verdict": "ACCUMULATE", "alloc": 5000}])
    out = bc.run_one_day(DAY, ["AAPL"], out_subdir=SEALED_ARM_SUBDIR, votes=3,
                         macro_view_override="MACRO-VIEW-FROM-CONTROL")

    # 同一份输入抽了 3 次，rounds=1，且 macro 借的是传入的（没重跑 Macro）
    assert len(calls) == 3 and macro_calls == []
    assert {c["max_debate_rounds"] for c in calls} == {1}
    for key in ("asset", "market_data", "macro_view", "portfolio_summary", "regime_brief", "prior_insights"):
        assert all(c[key] == calls[0][key] for c in calls), f"{key} 在 3 次抽样间必须逐字相同"
    assert calls[0]["macro_view"] == "MACRO-VIEW-FROM-CONTROL"
    assert out["macro_view"] == "MACRO-VIEW-FROM-CONTROL"

    v = out["verdicts"]["AAPL"]
    assert (v["verdict"], v["confidence"], v["alloc_cny"]) == ("ACCUMULATE", 0.67, 4000)
    assert v["flip"] is True and v["split"] is False

    # 落到 .backtest_sealed/<date>/，不碰对照臂目录
    md = _sealed_md(tmp_path)
    assert md.exists() and not (tmp_path / "memory" / ".backtest").exists()
    text = md.read_text(encoding="utf-8")
    parsed = parse_committee_text(text)
    assert (parsed["verdict"], parsed["confidence"], parsed["alloc_cny"]) == ("ACCUMULATE", 0.67, 4000.0)
    h = parsed["input_health"]
    assert h["arm"] == "sealed_n3" and h["flip"] is True and h["split"] is False
    assert [x["verdict"] for x in h["votes"]] == ["ACCUMULATE", "HOLD", "ACCUMULATE"]
    assert [x["alloc"] for x in h["votes"]] == [3000, 0, 5000]
    assert h["model"] == "m-test"                              # debate_meta 透传
    assert "CIO MEMO#1" in text                                # 正文是代表票（多数票里最早的一次）
    assert "**Contaminated**:" in text                             # 与对照臂同一个落盘尾巴（测试日期在 cutoff 前，故 true）


def test_run_one_day_sealed_resumes_by_skipping_finished_symbols(monkeypatch, tmp_path):
    calls, _ = _install_stubs(monkeypatch, tmp_path, [V("HOLD")] * 6)
    kw = dict(out_subdir=SEALED_ARM_SUBDIR, votes=3, macro_view_override="M")
    bc.run_one_day(DAY, ["AAPL"], **kw)
    assert len(calls) == 3
    again = bc.run_one_day(DAY, ["AAPL"], **kw)                # 同一天重跑 → 跳过，不花钱
    assert again["verdicts"]["AAPL"] == {"skipped": True} and len(calls) == 3


def test_run_one_day_sealed_split_becomes_hold_and_says_so(monkeypatch, tmp_path):
    _install_stubs(monkeypatch, tmp_path, [
        {"verdict": "ACCUMULATE", "alloc": 3000, "extra": {"_original_alloc": 9000}},
        {"verdict": "BUY", "alloc": 4000}, {"verdict": "HOLD"}])
    out = bc.run_one_day(DAY, ["AAPL"], out_subdir=SEALED_ARM_SUBDIR, votes=3, macro_view_override="M")
    v = out["verdicts"]["AAPL"]
    assert (v["verdict"], v["confidence"], v["alloc_cny"], v["split"]) == ("HOLD", 0.33, 0, True)
    text = _sealed_md(tmp_path).read_text(encoding="utf-8")
    parsed = parse_committee_text(text)
    assert parsed["verdict"] == "HOLD" and parsed["alloc_cny"] == 0.0
    h = parsed["input_health"]
    assert h["split"] is True and h["flip"] is True
    # split 的 HOLD 是投票结果，不是规则改写：代表票的 `_original_*` 溯源不能漏进来
    assert h["rewritten"] == [] and h["forced_hold"] is False
    assert "票数分裂" in text and "ACCUMULATE/BUY/HOLD" in text


def test_run_one_day_sealed_failed_sample_writes_nothing_so_resume_retries(monkeypatch, tmp_path):
    calls, _ = _install_stubs(monkeypatch, tmp_path, [V("HOLD"), RuntimeError("llm down"), V("HOLD")])
    out = bc.run_one_day(DAY, ["AAPL"], out_subdir=SEALED_ARM_SUBDIR, votes=3, macro_view_override="M")
    assert "error" in out["verdicts"]["AAPL"]
    assert not _sealed_md(tmp_path).exists(), "票数不足 N 的行不能落盘（会污染臂内稳定度）"


def test_control_arm_transcript_format_is_unchanged(monkeypatch, tmp_path):
    """votes=1（对照臂）：单次抽样、不带 arm/votes、不传 debate_meta——和加臂之前逐字段一致。"""
    calls, macro_calls = _install_stubs(monkeypatch, tmp_path, [{"verdict": "HOLD"}])
    out = bc.run_one_day(DAY, ["AAPL"])
    assert len(calls) == 1 and macro_calls == ["macro-data"]
    assert calls[0]["max_debate_rounds"] == 1                  # INVEST_MAX_DEBATE_ROUNDS 缺省
    assert out["macro_view"] == "MACRO-VIEW-FROM-CONTROL"      # 供下一臂借用
    text = (tmp_path / "memory" / ".backtest" / DAY / "AAPL.md").read_text(encoding="utf-8")
    h = parse_committee_text(text)["input_health"]
    assert "arm" not in h and "votes" not in h and "model" not in h
    assert "**Debate**" not in text


# ---------- main()：flag 约束 + 调度顺序 ----------

def test_sealed_flags_require_prospective_and_min_n(monkeypatch):
    monkeypatch.setattr("sys.argv", ["backtest_committee", "--sealed-arm", "--days", "1"])
    with pytest.raises(SystemExit, match="--prospective"):
        bc.main()
    monkeypatch.setattr("sys.argv", ["backtest_committee", "--prospective", "--sealed-arm", "--sealed-n", "2"])
    with pytest.raises(SystemExit, match="--sealed-n"):
        bc.main()


def _prospective_main(monkeypatch, argv, fake_run_one_day):
    import openinvest.db.market_store as ms_mod

    class _Store:
        def get_history_df(self, sym, days=5):
            return pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex([datetime.now().date()]))

    monkeypatch.setattr(ms_mod, "MarketStore", _Store)
    monkeypatch.setattr(bc, "run_one_day", fake_run_one_day)
    monkeypatch.setattr("sys.argv", ["backtest_committee", "--prospective", "--assets", "AAA,BBB", *argv])
    bc.main()


def test_main_runs_sealed_arm_after_control_and_shares_macro(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def fake(d, syms, **kw):
        seen.append({"syms": list(syms), **kw})
        return {"date": d, "macro_view": "MACRO-X", "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    _prospective_main(monkeypatch, ["--sealed-arm", "--sealed-n", "5"], fake)
    assert len(seen) == 2
    assert "out_subdir" not in seen[0] and "votes" not in seen[0]            # 对照臂：原调用不变
    assert seen[1]["out_subdir"] == SEALED_ARM_SUBDIR and seen[1]["votes"] == 5
    assert seen[1]["macro_view_override"] == "MACRO-X" and seen[1]["syms"] == ["AAA", "BBB"]


def test_main_without_sealed_flag_is_unchanged_and_failure_exits_nonzero(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def fake(d, syms, **kw):
        seen.append(kw)
        return {"date": d, "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    _prospective_main(monkeypatch, [], fake)
    assert seen == [{}]                                                        # 只有对照臂这一次调用

    def fake_sealed_fails(d, syms, **kw):
        if kw.get("votes"):
            return {"date": d, "verdicts": {"AAA": {"error": "boom"}, "BBB": {"verdict": "HOLD"}}}
        return {"date": d, "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    with pytest.raises(SystemExit) as ei:
        _prospective_main(monkeypatch, ["--sealed-arm"], fake_sealed_fails)
    assert ei.value.code == 1                                                  # 臂失败 → cron 退出码暴露
