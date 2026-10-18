"""B4 密封投票实验臂（评审路线 B4）——舰队侧契约测试。

守四件事：
- 聚合规则（多数 / 平票 / 无多数 / 多数票金额中位数）是纯函数，边界写死
- run_one_day(votes=N)：同一份输入抽 N 次、落 Input Health(arm/votes/flip/split)、同日跑过就跳过、
  任一次抽样失败整标的不落盘；votes=1 的对照臂落盘格式不变
- debate 臂（rounds=4 单次抽样）/ macro-soft 臂（CIO 规则 2 变体）：落盘健康位、变体只动规则 2 且只在臂内生效
- main() 把四臂接在对照臂之后、借对照臂的 macro_view；各 flag 只能配 --prospective

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
                "debate": {"max_rounds": kw["max_debate_rounds"], "final_round": step.get("final_round", 1),
                           "converged": step.get("converged", False),
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


# ---------- debate 臂 / macro-soft 臂（单次抽样的实验臂） ----------

MACRO_RISK_OFF = "SIGNAL: risk_off\nSTRENGTH: 6\nSCORE: -3\nONE_LINER: x"


def test_debate_arm_is_single_sample_rounds4_and_records_rounds(monkeypatch, tmp_path):
    calls, macro_calls = _install_stubs(monkeypatch, tmp_path, [
        {"verdict": "HOLD", "final_round": 3, "converged": True}])
    out = bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.DEBATE_ARM_SUBDIR, macro_view_override=MACRO_RISK_OFF,
                         max_rounds_override=bc.DEBATE_ROUNDS, arm_health={"arm": "debate_r4"})
    assert len(calls) == 1 and macro_calls == []
    assert calls[0]["max_debate_rounds"] == 4 and calls[0]["macro_view"] == MACRO_RISK_OFF
    text = (tmp_path / "memory" / bc.DEBATE_ARM_SUBDIR / DAY / "AAPL.md").read_text(encoding="utf-8")
    h = parse_committee_text(text)["input_health"]
    assert h["arm"] == "debate_r4" and h["final_round"] == 3 and h["converged"] is True
    assert (h["macro_signal"], h["macro_strength"]) == ("risk_off", 6.0)
    assert "votes" not in h and "**Debate**: 3 round(s) (max 4, converged)" in text
    assert out["verdicts"]["AAPL"]["verdict"] == "HOLD" and "votes" not in out["verdicts"]["AAPL"]


def test_macro_soft_arm_health_and_env_rounds_unchanged(monkeypatch, tmp_path):
    calls, _ = _install_stubs(monkeypatch, tmp_path, [{"verdict": "ACCUMULATE", "alloc": 2400}])
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.MACROSOFT_ARM_SUBDIR, macro_view_override=MACRO_RISK_OFF,
                   arm_health={"arm": "macrosoft_r1"})
    assert calls[0]["max_debate_rounds"] == 1                  # 没传 override → 仍是 INVEST_MAX_DEBATE_ROUNDS 缺省
    text = (tmp_path / "memory" / bc.MACROSOFT_ARM_SUBDIR / DAY / "AAPL.md").read_text(encoding="utf-8")
    parsed = parse_committee_text(text)
    assert (parsed["verdict"], parsed["alloc_cny"]) == ("ACCUMULATE", 2400.0)
    assert parsed["input_health"]["arm"] == "macrosoft_r1" and parsed["input_health"]["macro_strength"] == 6.0


def test_macro_signal_strength_parse():
    assert bc.macro_signal_strength(MACRO_RISK_OFF) == ("risk_off", 6.0)
    assert bc.macro_signal_strength("[backtest macro failed: boom]") == (None, None)


def _cio_prompts(monkeypatch):
    """跑一次 stub 委员会（rounds=1），返回 CIO agent 收到的 system prompt（同 test_fleet_confidence_t2 的做法）。"""
    import openinvest.core.committee.debate as debate
    seen = []

    class _Stub:
        last_tool_calls = []

        def __init__(self, role):
            self.role = role

        def run(self, ctx):
            if self.role == "cio":
                return "VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: risk\nSUGGESTED_ALLOC_CNY: 0\n"
            return "SIGNAL: neutral\nONE_LINER: stub\nSTRENGTH: weak"

        def tool_call_summary(self):
            return ""

    def fake_create_agent(system_prompt, *, role="unknown", **kw):
        if role == "cio":
            seen.append(system_prompt)
        return _Stub(role)

    monkeypatch.setattr(debate, "_create_agent", fake_create_agent)
    monkeypatch.setattr("openinvest.utils.llm.supports_json_output", lambda: False)
    monkeypatch.delenv("INVEST_CIO_THINKING", raising=False)
    debate.run_committee(asset={"symbol": "QQQ", "display_name": "QQQ"}, market_data="(stub)", macro_view="(stub)",
                         portfolio_summary="(stub)", persist_to_memory=False, max_debate_rounds=1)
    return seen


def test_macro_soft_variant_only_touches_rule2_and_live_prompt_is_unchanged(monkeypatch):
    import hashlib

    import openinvest.core.committee.debate as debate
    from openinvest.capabilities.committee.cio import build_cio_prompt

    asset = {"symbol": "QQQ", "display_name": "QQQ"}
    for json_mode in (False, True):
        live = build_cio_prompt(asset, json_mode=json_mode)
        soft = bc.build_cio_prompt_macro_soft(asset, json_mode=json_mode)
        assert "macro-soft 试跑变体" in soft and "× (10 − Macro STRENGTH) / 10" in soft
        assert "STRENGTH ≥ 8 的 risk_off 仍然否决" in soft
        start = soft.index("。\n   **例外——Macro 偏空但不否决")
        end = soft.index("仍然否决（HOLD）。", start) + len("仍然否决（HOLD）。")
        assert soft[:start] + soft[end:] == live            # 变体 = live + 规则 2 后面插入一段，别无二致

    sha = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()  # noqa: E731
    before = sha(build_cio_prompt(asset))
    live_seen = _cio_prompts(monkeypatch)
    with bc._macro_soft_arm():
        soft_seen = _cio_prompts(monkeypatch)
    after_seen = _cio_prompts(monkeypatch)
    assert [sha(p) for p in live_seen] == [before] and [sha(p) for p in after_seen] == [before]
    assert len(soft_seen) == 1 and "macro-soft 试跑变体" in soft_seen[0]
    assert debate.build_cio_prompt is build_cio_prompt       # 开关退出后命名空间还原


def test_macro_soft_anchor_drift_fails_loudly(monkeypatch):
    monkeypatch.setattr(bc, "_CIO_RULE2", "2. 这句话 cio.md 里没有")
    with pytest.raises(RuntimeError, match="锚点失效"):
        bc.build_cio_prompt_macro_soft({"symbol": "QQQ"})


# ---------- 评审 #460：macro_shared / 对照轮数闸 / 共用多数判定 ----------

def _health(tmp_path, subdir):
    return parse_committee_text((tmp_path / "memory" / subdir / DAY / "AAPL.md").read_text(encoding="utf-8"))["input_health"]


def test_macro_shared_true_only_when_arm_macro_is_the_one_in_the_control_transcript(monkeypatch, tmp_path):
    _install_stubs(monkeypatch, tmp_path, [V("HOLD")] * 12)
    res = bc.run_one_day(DAY, ["AAPL"])                                   # 对照臂真跑 Macro
    shared = res["macro_view"]
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.SEALED_ARM_SUBDIR, votes=3, macro_view_override=shared)
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.DEBATE_ARM_SUBDIR, max_rounds_override=4,
                   macro_view_override=shared, arm_health={"arm": "debate_r4"})
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.MACROSOFT_ARM_SUBDIR, macro_view_override=shared,
                   arm_health={"arm": "macrosoft_r1"})
    for sub in (bc.SEALED_ARM_SUBDIR, bc.DEBATE_ARM_SUBDIR, bc.MACROSOFT_ARM_SUBDIR):
        assert _health(tmp_path, sub)["macro_shared"] is True, sub
    assert "macro_shared" not in _health(tmp_path, ".backtest")            # 对照臂落盘格式不变


def test_macro_shared_false_when_arm_reran_macro_or_control_transcript_is_missing(monkeypatch, tmp_path):
    _install_stubs(monkeypatch, tmp_path, [V("HOLD")] * 8)
    # 情形 1：对照臂中途失败重跑——先落盘的标的是上一次的 Macro，臂拿到的是这次的另一份
    bc.run_one_day(DAY, ["AAPL"])
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.MACROSOFT_ARM_SUBDIR, macro_view_override="ANOTHER MACRO DRAW",
                   arm_health={"arm": "macrosoft_r1"})
    assert _health(tmp_path, bc.MACROSOFT_ARM_SUBDIR)["macro_shared"] is False
    # 情形 2：对照臂整天被续跑跳过（没有 macro_view 可借）→ 臂自己重跑 Macro；这里模拟成对照 transcript 不含这份 macro
    import shutil
    shutil.rmtree(tmp_path / "memory" / ".backtest")
    bc.run_one_day(DAY, ["AAPL"], out_subdir=bc.SEALED_ARM_SUBDIR, votes=3)
    assert _health(tmp_path, bc.SEALED_ARM_SUBDIR)["macro_shared"] is False    # 核对不了也记 False
    assert _health(tmp_path, bc.SEALED_ARM_SUBDIR)["final_round"] == 1          # sealed 臂记下实际轮数


def test_arms_refuse_to_start_when_control_rounds_env_is_not_1(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def fake(d, syms, **kw):
        seen.append(kw)
        return {"date": d, "macro_view": "M", "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    for flag in ("--sealed-arm", "--debate-arm", "--macro-soft-arm"):
        monkeypatch.setenv("INVEST_MAX_DEBATE_ROUNDS", "2")
        with pytest.raises(SystemExit, match="INVEST_MAX_DEBATE_ROUNDS"):
            _prospective_main(monkeypatch, [flag], fake)
    assert seen == [], "拒绝发生在花钱之前：连对照臂都没跑"
    # 没开任何试跑臂：对照臂行为不变，env 照旧可用（Optuna 之类）
    _prospective_main(monkeypatch, [], fake)
    assert len(seen) == 1
    # env = 1 或未设：放行
    seen.clear()
    monkeypatch.setenv("INVEST_MAX_DEBATE_ROUNDS", "1")
    _prospective_main(monkeypatch, ["--sealed-arm"], fake)
    monkeypatch.delenv("INVEST_MAX_DEBATE_ROUNDS")
    _prospective_main(monkeypatch, ["--sealed-arm"], fake)
    assert len(seen) == 4


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


def test_main_runs_all_four_arms_in_order_sharing_macro(monkeypatch):
    import openinvest.core.committee.debate as debate
    from openinvest.capabilities.committee.cio import build_cio_prompt

    seen: List[Dict[str, Any]] = []

    def fake(d, syms, **kw):
        seen.append({"variant_active": debate.build_cio_prompt is bc.build_cio_prompt_macro_soft, **kw})
        return {"date": d, "macro_view": "MACRO-X", "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    _prospective_main(monkeypatch, ["--sealed-arm", "--debate-arm", "--macro-soft-arm"], fake)
    assert len(seen) == 4
    control, sealed, debate_kw, soft = seen
    assert "out_subdir" not in control and not control["variant_active"]
    assert sealed["out_subdir"] == bc.SEALED_ARM_SUBDIR and sealed["votes"] == 3
    assert debate_kw["out_subdir"] == bc.DEBATE_ARM_SUBDIR and debate_kw["max_rounds_override"] == 4
    assert debate_kw["arm_health"] == {"arm": "debate_r4"} and not debate_kw["variant_active"]
    assert soft["out_subdir"] == bc.MACROSOFT_ARM_SUBDIR and soft["arm_health"] == {"arm": "macrosoft_r1"}
    assert soft["variant_active"], "macro-soft 臂必须在变体开关里跑"
    assert "max_rounds_override" not in soft and "votes" not in soft
    assert {k["macro_view_override"] for k in (sealed, debate_kw, soft)} == {"MACRO-X"}
    assert debate.build_cio_prompt is build_cio_prompt          # 跑完还原


def test_macro_soft_anchor_failure_skips_that_arm_only_and_exits_nonzero(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def fake(d, syms, **kw):
        seen.append(kw)
        return {"date": d, "macro_view": "M", "verdicts": {s: {"verdict": "HOLD"} for s in syms}}

    monkeypatch.setattr(bc, "_CIO_RULE2", "2. 没有这句")
    with pytest.raises(SystemExit) as ei:
        _prospective_main(monkeypatch, ["--debate-arm", "--macro-soft-arm"], fake)
    assert ei.value.code == 1
    assert [k.get("out_subdir") for k in seen] == [None, bc.DEBATE_ARM_SUBDIR]   # debate 臂照跑，macro-soft 没花钱


def test_new_arm_flags_require_prospective(monkeypatch):
    for flag in ("--debate-arm", "--macro-soft-arm"):
        monkeypatch.setattr("sys.argv", ["backtest_committee", flag, "--days", "1"])
        with pytest.raises(SystemExit, match="--prospective"):
            bc.main()


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
