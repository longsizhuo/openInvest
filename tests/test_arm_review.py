"""B4 密封投票实验臂（评审路线 B4）——复盘侧契约测试。

守四件事：
- 纯核：同日同标的配对、同尺子基率/提升/CI、n<30 不出数字、配对符号检验、逐日方向翻转、票数稳定度
- review_arm：source 标 "arm:sealed"、Input Health 的 arm/votes/flip 读得回来
- run_arm：只写独立 jsonl + 报告；报告无 symbol
- 实验臂不污染 live：verdict_review.jsonl / confidence_lookup / 公开 JSON 有无臂都逐项相同；
  臂复盘炸了 live 复盘照常

不联网、不调 LLM。舰队侧（聚合/落盘/调度）见 tests/test_sealed_arm.py。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from openinvest.jobs import review_calc as rc
from openinvest.jobs import verdict_review as vr


# ---------- 复盘：纯核 ----------

def _rv(day: str, asset: str, verdict: str, *, ret: float, mkt: str, hit: bool,
        votes: List[str] | None = None) -> rc.VerdictReview:
    rv = rc.VerdictReview(date=day, asset=asset, verdict=verdict, confidence=0.6,
                          expected_direction=rc.EXPECTED_DIRECTION.get(verdict, "none"), macro_at_decision={})
    rv.actual_returns["30d"], rv.directions["30d"], rv.hits["30d"] = ret, mkt, hit
    if votes:
        rv.input_health = {"arm": "sealed_n3", "votes": [{"verdict": x} for x in votes]}
    return rv


def _paired_fixture():
    market = [("2026-08-03", "AAA", 0.05, "up"), ("2026-08-03", "BBB", -0.02, "flat"),
              ("2026-08-04", "AAA", 0.001, "flat"), ("2026-08-04", "BBB", 0.09, "up")]
    arm_verdicts = [("ACCUMULATE", True), ("ACCUMULATE", False), ("HOLD", True), ("HOLD", False)]
    ctl_verdicts = [("HOLD", False), ("HOLD", True), ("HOLD", True), ("ACCUMULATE", True)]
    arm = [_rv(d, a, v, ret=r, mkt=m, hit=h) for (d, a, r, m), (v, h) in zip(market, arm_verdicts)]
    ctl = [_rv(d, a, v, ret=r, mkt=m, hit=h) for (d, a, r, m), (v, h) in zip(market, ctl_verdicts)]
    return arm, ctl


def test_arm_vs_control_pairs_by_date_and_symbol_and_uses_same_yardstick_base_rate():
    arm, ctl = _paired_fixture()
    ctl.append(_rv("2026-08-05", "AAA", "HOLD", ret=0.0, mkt="flat", hit=True))   # 对照臂独有 → 不进配对
    cmp_ = rc.arm_vs_control(arm, ctl, min_n=4)
    assert (cmp_["matched"], cmp_["arm_total"], cmp_["control_total"]) == (4, 4, 5)
    A, C = cmp_["arm"], cmp_["control"]
    # 实验臂：命中 2/4；逐行基率 ACC→P(涨>0)=.75、HOLD→P(flat)=.5 → 均值 .625；提升 −12.5pp
    assert A["hit_rate_30d"] == 0.5 and A["base_rate_30d"] == pytest.approx(0.625)
    assert A["lift_30d"] == pytest.approx(-0.125)
    ci = A["ci95_30d"]
    assert A["lift_ci95_30d"] == pytest.approx([ci[0] - 0.625, ci[1] - 0.625], abs=1e-3)
    # 对照臂：命中 3/4，基率 (.5+.5+.5+.75)/4
    assert C["hit_rate_30d"] == 0.75 and C["base_rate_30d"] == pytest.approx(0.5625, abs=1e-3)
    assert A["hold_share"] == 0.5 and C["hold_share"] == 0.75
    # 配对 30d：A 仅臂中；B、D 仅对照中；C 都中
    p30 = cmp_["paired_30d"]
    assert (p30["n"], p30["arm_only"], p30["control_only"], p30["both"], p30["neither"]) == (4, 1, 2, 1, 0)
    assert p30["p"] == 1.0                                    # 1 vs 2：远谈不上显著
    # 逐日方向翻转：臂 AAA/BBB 都是 ACC→HOLD（翻），对照 AAA HOLD→HOLD（不翻）、BBB HOLD→ACC（翻）
    pc = cmp_["paired_churn"]
    assert (pc["n"], pc["arm_only"], pc["control_only"], pc["both"]) == (2, 1, 0, 1)
    # HOLD 配对：臂 HOLD = {3,4}，对照 HOLD = {1,2,3}；仅臂 HOLD 的是第 4 行，仅对照 HOLD 的是 1、2 行
    ph = cmp_["paired_hold"]
    assert (ph["arm_only"], ph["control_only"], ph["both"], ph["neither"]) == (1, 2, 1, 0)


def test_sign_test_and_direction_churn_definitions():
    assert rc.sign_test_p(0, 0) is None and rc.sign_test_p(1, 2) == 1.0 and rc.sign_test_p(5, 5) == 1.0
    assert rc.sign_test_p(0, 10) == pytest.approx(0.002, abs=5e-4)       # 2 / 2^10
    assert rc.sign_test_p(3, 17) == rc.sign_test_p(17, 3) == pytest.approx(0.0026, abs=5e-4)

    def row(day, asset, verdict):
        return _rv(day, asset, verdict, ret=0.0, mkt="flat", hit=True)

    ch = rc.direction_churn([
        row("2026-08-03", "AAA", "ACCUMULATE"), row("2026-08-04", "AAA", "HOLD"), row("2026-08-05", "AAA", "HOLD"),
        row("2026-08-12", "AAA", "BUY"),                                    # 与 08-05 相隔 7 天 → 断档，不比
        row("2026-08-03", "BBB", "BUY"), row("2026-08-04", "BBB", "ACCUMULATE"),   # 同为 up 类 → 不算翻
        row("2026-08-07", "CCC", "HOLD"), row("2026-08-10", "CCC", "UNCLEAR"),     # 周五→周一（3 天）算逐日；flat→none 算翻
    ])
    assert ch == {("AAA", "2026-08-04"): True, ("AAA", "2026-08-05"): False,
                  ("BBB", "2026-08-04"): False, ("CCC", "2026-08-10"): True}


def test_arm_vs_control_suppresses_numbers_below_30():
    arm, ctl = _paired_fixture()
    cmp_ = rc.arm_vs_control(arm, ctl)                          # 默认 min_n=30
    for side in (cmp_["arm"], cmp_["control"]):
        assert side["suppressed"] is True and side["n_scored_30d"] == 4
        assert side["hit_rate_30d"] is None and side["lift_30d"] is None and side["ci95_30d"] is None
        assert all("hit_rate_30d" not in c for c in side["by_verdict"].values())
    # 逐 verdict 的格子各按自己的分母：整侧 4 条 ≥ min_n=3，ACCUMULATE 只有 2 条 < 3 → 该格不出数字
    cmp3 = rc.arm_vs_control(arm, ctl, min_n=3)
    assert cmp3["arm"]["hit_rate_30d"] is not None
    assert "hit_rate_30d" not in cmp3["arm"]["by_verdict"]["ACCUMULATE"]
    assert "hit_rate_30d" in rc.arm_vs_control(arm, ctl, min_n=2)["arm"]["by_verdict"]["ACCUMULATE"]


def test_vote_stability_rates_hold_shares_and_tiers():
    mk = lambda i, v, votes: _rv(f"2026-08-{i:02d}", "AAA", v, ret=0.0, mkt="flat", hit=True, votes=votes)  # noqa: E731
    arm = [
        mk(3, "ACCUMULATE", ["ACCUMULATE"] * 3),                      # 全票一致
        mk(4, "HOLD", ["HOLD", "HOLD", "HOLD"]),                      # 全票一致
        mk(5, "HOLD", ["ACCUMULATE", "HOLD", "HOLD"]),                # 多数；方向翻转
        mk(6, "BUY", ["BUY", "ACCUMULATE", "BUY"]),                   # 多数；同方向类不翻转
        mk(7, "HOLD", ["ACCUMULATE", "BUY", "TRIM"]),                 # 无多数→HOLD；翻转
    ]
    arm.append(_rv("2026-08-08", "AAA", "HOLD", ret=0.0, mkt="flat", hit=True))   # 无逐票记录 → 不算
    ctl = [_rv("2026-08-03", "AAA", "HOLD", ret=0, mkt="flat", hit=True)] * 3 + \
          [_rv("2026-08-04", "AAA", "ACCUMULATE", ret=0, mkt="flat", hit=True)]
    st = rc.vote_stability(arm, ctl, min_n=2)
    assert st["n"] == 5 and st["arm_tags"] == {"sealed_n3": 5}
    assert st["unanimous"]["k"] == 2 and st["flip"]["k"] == 2 and st["split"]["k"] == 1
    assert st["unanimous"]["ci95"] and st["flip"]["rate"] == 0.4
    assert st["hold_share_votes"] == pytest.approx(5 / 15, abs=1e-3)        # 15 票里 5 票 HOLD
    assert st["hold_share_final"] == pytest.approx(3 / 5, abs=1e-3)
    assert st["hold_share_control"] == pytest.approx(3 / 4, abs=1e-3)
    assert {t: st["tiers"][t]["n"] for t in rc.ARM_TIERS} == {"unanimous": 2, "majority": 2, "split": 1}
    assert st["tiers"]["unanimous"]["hit_rate_30d"] == 1.0         # n_scored=2 ≥ min_n=2
    assert st["tiers"]["split"]["hit_rate_30d"] is None            # n_scored=1 < min_n


# ---------- 评审 #460：macro_shared 剔除 / 多数判定只有一份 ----------

def test_vote_tier_and_aggregate_votes_share_one_majority_definition():
    """复盘侧的一致度分档和写侧的聚合用同一份多数判定：穷举 N=3..5 的票组合，逐个对账。"""
    import itertools
    for n in (3, 4, 5):
        for votes in itertools.product(("HOLD", "ACCUMULATE", "TRIM"), repeat=n):
            agg = rc.aggregate_votes([{"verdict": v, "alloc": 0} for v in votes])
            tier = rc._vote_tier(list(votes))
            assert (tier == "split") == agg["split"], votes
            assert (tier == "unanimous") == (len(set(votes)) == 1), votes
            assert (tier == "majority") == (len(set(votes)) > 1 and not agg["split"]), votes


def test_rows_whose_macro_differs_from_control_are_excluded_and_counted():
    mk = lambda i, shared: _rv2(f"2026-08-0{i}", "A", "HOLD", health={"arm": "debate_r4", "final_round": 3,  # noqa: E731
                                                                      "converged": True, **({} if shared is None else {"macro_shared": shared})})
    arm = [mk(3, True), mk(4, False), mk(5, None)]                 # None = 早期存档没有这个键，按未知保留
    ctl = [_rv2(f"2026-08-0{i}", "A", "HOLD") for i in (3, 4, 5)]
    cmp_ = rc.arm_vs_control(arm, ctl, min_n=2)
    assert cmp_["matched"] == 2 and cmp_["arm_total"] == 2
    assert rc.macro_unshared_count(arm) == 1
    assert rc.side_by_side(ctl, {"debate": arm}, min_n=2)["keys"] == 2
    text = "\n".join(rc.arms_report_lines({"debate": arm}, ctl, min_n=2))
    assert "macro 与对照不是同一份而剔除的行" in text and "| debate |" in text
    row = next(l for l in text.splitlines() if l.startswith("| debate | debate"))
    assert row.rstrip().endswith("| 3 | 1 |"), row                  # 臂内 3 行，其中 1 行因 macro 不同被剔除


# ---------- 四臂并排 / debate 轮数 / macro-soft H3 ----------

def _rv2(day, asset, verdict, *, ret=0.0, excess=None, alloc=0.0, health=None):
    rv = _rv(day, asset, verdict, ret=ret, mkt="flat", hit=True)
    rv.alloc_cny = alloc
    if excess is not None:
        rv.excess_returns["30d"] = excess
    if alloc and verdict in rc.DIRECTIONAL_BUY:
        rv.followed_pnl["30d"] = rc.counterfactual_pnl(verdict, alloc, ret, 0.001)
    rv.input_health = health or {}
    return rv


def test_macro_soft_h3_counts_only_unlocked_rows_and_gates_each_number():
    arm = [_rv2("2026-08-03", "A", "ACCUMULATE", ret=0.05, alloc=1000, health={"macro_signal": "risk_off", "macro_strength": 6}),
           _rv2("2026-08-03", "B", "ACCUMULATE", ret=-0.02, alloc=1000, health={"macro_signal": "risk_off", "macro_strength": 6}),
           _rv2("2026-08-04", "A", "ACCUMULATE", ret=0.10, alloc=2000, health={"macro_signal": "risk_off", "macro_strength": 6}),
           _rv2("2026-08-04", "B", "HOLD", health={"macro_signal": "risk_off", "macro_strength": 8})]
    ctl = [_rv2("2026-08-03", "A", "HOLD", excess=0.03), _rv2("2026-08-03", "B", "HOLD", excess=-0.04),
           _rv2("2026-08-04", "A", "ACCUMULATE", excess=0.08), _rv2("2026-08-04", "B", "HOLD", excess=0.0)]
    h = rc.macro_soft_h3(arm, ctl, min_n=2)
    assert (h["directional"], h["unlocked"], h["agree_with_control"], h["matured"]) == (3, 2, 1, 2)
    assert h["excess_30d"]["mean"] == pytest.approx(-0.005)                 # 取的是对照臂同日同标的行的超额
    assert h["mean_return_30d"] == pytest.approx(0.015)
    # 照做：49 + (−21) = 28，名义 2000 → 1.4%（只算解锁的两行，agree 的那行不算）
    assert h["followed_net_return_30d"] == pytest.approx(0.014) and h["followed_n_30d"] == 2
    assert (h["n_macro_risk_off"], h["n_still_vetoed_strength_ge_8"]) == (4, 1)
    assert h["status"] == "fail"                                            # 超额 CI 下界 < 0
    # 成熟解锁行 < min_n：所有数字抹掉，状态 insufficient（红线 #2）
    h3 = rc.macro_soft_h3(arm, ctl, min_n=3)
    assert h3["status"] == "insufficient" and h3["excess_30d"] is None and h3["followed_net_return_30d"] is None
    assert h3["mean_return_30d"] is None and h3["unlocked"] == 2


def test_side_by_side_uses_only_keys_every_arm_has_and_arm_rounds_summary():
    ctl = [_rv2("2026-08-03", "A", "HOLD"), _rv2("2026-08-03", "B", "HOLD"), _rv2("2026-08-04", "A", "HOLD")]
    sealed = [_rv2("2026-08-03", "A", "HOLD"), _rv2("2026-08-04", "A", "HOLD")]
    debate = [_rv2("2026-08-03", "A", "HOLD", health={"final_round": 3, "converged": True}),
              _rv2("2026-08-03", "B", "HOLD", health={"final_round": 4, "converged": False}),
              _rv2("2026-08-04", "A", "TRIM", health={"final_round": 2, "converged": True})]
    sb = rc.side_by_side(ctl, {"sealed": sealed, "debate": debate}, min_n=2)
    assert sb["keys"] == 2 and set(sb["sides"]) == {"control", "sealed", "debate"}
    assert all(s["n_rows"] == 2 for s in sb["sides"].values())
    assert sb["sides"]["debate"]["directional_rows"] == 1 and sb["sides"]["control"]["directional_rows"] == 0
    d = rc.arm_rounds_summary(debate)
    assert d["n"] == 3 and d["mean_rounds"] == 3.0 and d["converged"]["k"] == 2


def test_arms_report_lines_has_every_section_and_no_symbols():
    ctl = [_rv2("2026-08-03", "SECRET1", "HOLD", excess=0.01), _rv2("2026-08-04", "SECRET1", "HOLD", excess=0.02)]
    sealed = [_rv2("2026-08-03", "SECRET1", "HOLD", health={"arm": "sealed_n3", "votes": [{"verdict": "HOLD"}] * 3}),
              _rv2("2026-08-04", "SECRET1", "HOLD", health={"arm": "sealed_n3", "votes": [{"verdict": "HOLD"}] * 3})]
    debate = [_rv2("2026-08-03", "SECRET1", "HOLD", health={"final_round": 2, "converged": True}),
              _rv2("2026-08-04", "SECRET1", "HOLD", health={"final_round": 4, "converged": False})]
    soft = [_rv2("2026-08-03", "SECRET1", "ACCUMULATE", ret=0.02, alloc=500,
                 health={"macro_signal": "risk_off", "macro_strength": 6}),
            _rv2("2026-08-04", "SECRET1", "HOLD", health={"macro_signal": "risk_off", "macro_strength": 6})]
    text = "\n".join(rc.arms_report_lines({"sealed": sealed, "debate": debate, "macrosoft": soft}, ctl, min_n=2))
    for must in ("## 臂一览", "## 并排（所有在场的臂同时具备的 2 个 标的×日", "全票一致", "debate 臂：辩论实际跑了几轮",
                 "## H3：macro-soft 臂", "配对对照（macrosoft vs 对照臂", "配对对照（debate vs 对照臂",
                 "H3 macro-soft 解锁行成熟 n ≥ 30"):
        assert must in text, must
    assert "SECRET1" not in text
    # 只有一个臂在场时不出别的臂的节
    only = "\n".join(rc.arms_report_lines({"debate": debate}, ctl, min_n=2))
    assert "H3：macro-soft" not in only and "全票一致" not in only and "debate 臂：辩论" in only


# ---------- 复盘：review_arm / run_arms 端到端 + 不污染 live ----------

HEALTH_FMT = "## Input Health (deterministic)\n```json\n{health}\n```\n"


def _write_transcript(mem: Path, subdir: str, day: str, sym: str, verdict: str, conf: float,
                      alloc: int, votes: List[str] | None = None, health_extra: Dict[str, Any] | None = None) -> None:
    d = mem / subdir / day
    d.mkdir(parents=True, exist_ok=True)
    health: Dict[str, Any] = {"schema": 1, "worker_unavailable": [], "rewritten": [], "forced_hold": False}
    if votes:
        health.update(arm=f"sealed_n{len(votes)}", flip=len(set(votes)) > 1, split=False,
                      votes=[{"verdict": v, "confidence": 0.62, "alloc": 0} for v in votes])
    health.update(health_extra or {})
    p = d / f"{sym}.md"
    p.write_text(
        f"# Committee: {sym}\n**Date**: {day}\n**Symbol**: {sym}\n**Verdict**: {verdict} (confidence {conf:.2f})\n"
        f"**Suggested allocation CNY**: {alloc}\n\n"
        '## Macro Context Snapshot\n```json\n{"vix": 15.0}\n```\n'
        + HEALTH_FMT.format(health=json.dumps(health)), encoding="utf-8")
    ts = datetime.fromisoformat(day + "T22:30:00").timestamp()
    import os
    os.utime(p, (ts, ts))


@pytest.fixture
def review_env(monkeypatch, tmp_path):
    from openinvest.core import memory_store as ms
    from openinvest.db import market_store as mstore

    mem = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    monkeypatch.setattr(mstore, "DB_PATH", str(tmp_path / "market_data.db"))
    monkeypatch.setattr(vr, "_STORE", None)
    monkeypatch.setattr(vr, "_build_symbol_resolver", lambda: {})
    monkeypatch.setattr(vr, "_benchmark_symbol", lambda: "")
    store = mstore.MarketStore()
    for i, day in enumerate(pd.bdate_range("2025-06-01", "2026-09-30")):
        for sym, base in [("AAPL", 100.0), ("MSFT", 200.0), ("^VIX", 15.0), ("^TNX", 4.0), ("USDCNY=X", 7.0)]:
            c = base * (1 + 0.001 * i)
            store.save_generic_price(sym, day.strftime("%Y-%m-%d"), c, high=c * 1.01, low=c * 0.99, volume=1e6)
    return mem


SOFT_HEALTH = {"arm": "macrosoft_r1", "macro_signal": "risk_off", "macro_strength": 6.0,
               "final_round": 1, "converged": False}


def _seed_arm_and_control(mem: Path) -> None:
    """对照臂 3 行 + sealed / debate / macro-soft 三臂各 3 行（同样 3 个 日期×标的）。"""
    rows = [("2026-08-03", "AAPL", ["ACCUMULATE", "ACCUMULATE", "HOLD"], "ACCUMULATE", 0.67, 4000, "HOLD", 0),
            ("2026-08-03", "MSFT", ["HOLD", "HOLD", "HOLD"], "HOLD", 1.0, 0, "ACCUMULATE", 3000),
            ("2026-08-04", "AAPL", ["ACCUMULATE", "BUY", "TRIM"], "HOLD", 0.33, 0, "HOLD", 0)]
    for day, sym, votes, v, c, a, cv, ca in rows:
        _write_transcript(mem, vr.SEALED_ARM_SUBDIR, day, sym, v, c, a, votes)
        _write_transcript(mem, ".backtest", day, sym, cv, 0.62, ca)
        _write_transcript(mem, vr.DEBATE_ARM_SUBDIR, day, sym, "HOLD", 0.55, 0,
                          health_extra={"arm": "debate_r4", "final_round": 3, "converged": True})
        soft_v, soft_a = ("ACCUMULATE", 2400) if (day, sym) != ("2026-08-03", "MSFT") else ("HOLD", 0)
        _write_transcript(mem, vr.MACROSOFT_ARM_SUBDIR, day, sym, soft_v, 0.55, soft_a, health_extra=SOFT_HEALTH)


def test_review_arm_tags_source_and_reads_votes_back(review_env):
    _seed_arm_and_control(review_env)
    arm = vr.review_arm()
    assert len(arm) == 3 and {r.source for r in arm} == {"arm:sealed"}
    by = {(r.date, r.asset): r for r in arm}
    r = by[("2026-08-03", "AAPL")]
    assert (r.verdict, r.confidence, r.alloc_cny) == ("ACCUMULATE", 0.67, 4000.0)
    assert r.input_health["arm"] == "sealed_n3" and r.input_health["flip"] is True
    assert [v["verdict"] for v in r.input_health["votes"]] == ["ACCUMULATE", "ACCUMULATE", "HOLD"]
    assert "30d" in r.hits                                   # 复用 review_one：标签只读行情库
    control = vr.review_arm_control(arm)
    assert len(control) == 3 and {c.source for c in control} == {"backtest"}
    soft = vr.review_arm(vr.MACROSOFT_ARM_SUBDIR, name="macrosoft")
    assert {x.source for x in soft} == {"arm:macrosoft"} and soft[0].input_health["macro_strength"] == 6.0
    # 多个臂共用同一批 (日期, 标的) 时对照行只读一次
    assert len(vr.review_arm_control(arm + soft)) == 3


def test_run_arms_writes_only_its_own_artifacts_and_report_is_anonymous(review_env, tmp_path):
    _seed_arm_and_control(review_env)
    res = vr.run_arms()
    assert res["status"] == "ok" and res["arms"] == {"sealed": 3, "debate": 3, "macrosoft": 3}
    for name in ("sealed", "debate", "macrosoft"):
        jl = review_env / ".dreams" / f"verdict_review_{name}.jsonl"
        assert Path(res["jsonl"][name]) == jl
        rows = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 3 and {r["source"] for r in rows} == {f"arm:{name}"}
    report = Path(res["report"])
    assert report == tmp_path / "docs" / "verdict_accuracy_arms.md"
    text = report.read_text(encoding="utf-8")
    for must in ("实验臂", "全票一致", "n<30 不展示", "## 并排", "debate 臂：辩论实际跑了几轮", "## H3：macro-soft 臂"):
        assert must in text, must
    assert "AAPL" not in text and "MSFT" not in text, "报告只含聚合，不能出现 symbol"
    # 没有任何臂目录 → skipped，不写任何文件
    import shutil
    for name in list((review_env / ".dreams").glob("verdict_review_*.jsonl")) + [report]:
        name.unlink()
    for sub in (vr.SEALED_ARM_SUBDIR, vr.DEBATE_ARM_SUBDIR, vr.MACROSOFT_ARM_SUBDIR):
        shutil.rmtree(review_env / sub)
    assert vr.run_arms()["status"] == "skipped"
    assert not report.exists() and not list((review_env / ".dreams").glob("verdict_review_*.jsonl"))


def test_run_arms_works_with_only_some_arms_present(review_env):
    _seed_arm_and_control(review_env)
    import shutil
    shutil.rmtree(review_env / vr.SEALED_ARM_SUBDIR)
    shutil.rmtree(review_env / vr.MACROSOFT_ARM_SUBDIR)
    res = vr.run_arms()
    assert res["arms"] == {"debate": 3}
    assert not (review_env / ".dreams" / "verdict_review_sealed.jsonl").exists()


def test_run_arms_never_pollutes_live_jsonl_lookup_or_public_json(review_env):
    """有/无实验臂目录各跑一次 run()：live jsonl / confidence_lookup / 公开 JSON 逐项相同（除时间戳）。"""
    import shutil

    from scripts import export_accuracy

    def snapshot():
        live_path = review_env / ".dreams" / "verdict_review.jsonl"
        lookup = json.loads((review_env / ".dreams" / "confidence_lookup.json").read_text(encoding="utf-8"))
        lookup.pop("generated_at", None)
        public = export_accuracy.build_summary(live_path)
        public.pop("generated_at", None)
        return live_path.read_text(encoding="utf-8"), lookup, public

    _write_transcript(review_env, ".committee", "2026-08-03", "AAPL", "HOLD", 0.62, 0)
    _seed_arm_and_control(review_env)        # 对照行进 .backtest（舰队口径本来就会收），臂行进各自的 .backtest_*
    arm_dirs = [review_env / s for s in (vr.SEALED_ARM_SUBDIR, vr.DEBATE_ARM_SUBDIR, vr.MACROSOFT_ARM_SUBDIR)]
    parked = review_env / "_parked"
    parked.mkdir()

    for d in arm_dirs:                        # 基线：没有任何实验臂
        shutil.move(str(d), str(parked / d.name))
    assert vr.run()["status"] == "ok"
    baseline = snapshot()
    assert not list((review_env / ".dreams").glob("verdict_review_*.jsonl"))

    for d in arm_dirs:                        # 实验臂回来；run() 尾巴会调 run_arms()
        shutil.move(str(parked / d.name), str(d))
    assert vr.run()["status"] == "ok"
    assert snapshot() == baseline, "实验臂不得改变 live jsonl / confidence_lookup / 公开 JSON"
    assert all(not str(json.loads(x)["source"]).startswith("arm:") for x in baseline[0].splitlines())
    for name in ("sealed", "debate", "macrosoft"):   # 臂行只在各自的独立 jsonl
        jl = review_env / ".dreams" / f"verdict_review_{name}.jsonl"
        assert len(jl.read_text(encoding="utf-8").splitlines()) == 3


def test_run_survives_a_broken_arm_review(review_env, monkeypatch):
    """实验臂复盘炸了，live 复盘照常返回 ok（不拖垮已写完的 live 产物）。"""
    _write_transcript(review_env, ".committee", "2026-08-03", "AAPL", "HOLD", 0.62, 0)
    monkeypatch.setattr(vr, "run_arms", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("arm boom")))
    assert vr.run()["status"] == "ok"
    assert (review_env / ".dreams" / "verdict_review.jsonl").exists()
