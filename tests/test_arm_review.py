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


# ---------- 复盘：review_arm / run_arm 端到端 + 不污染 live ----------

HEALTH_FMT = "## Input Health (deterministic)\n```json\n{health}\n```\n"


def _write_transcript(mem: Path, subdir: str, day: str, sym: str, verdict: str, conf: float,
                      alloc: int, votes: List[str] | None = None) -> None:
    d = mem / subdir / day
    d.mkdir(parents=True, exist_ok=True)
    health: Dict[str, Any] = {"schema": 1, "worker_unavailable": [], "rewritten": [], "forced_hold": False}
    if votes:
        health.update(arm=f"sealed_n{len(votes)}", flip=len(set(votes)) > 1, split=False,
                      votes=[{"verdict": v, "confidence": 0.62, "alloc": 0} for v in votes])
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


def _seed_arm_and_control(mem: Path) -> None:
    rows = [("2026-08-03", "AAPL", ["ACCUMULATE", "ACCUMULATE", "HOLD"], "ACCUMULATE", 0.67, 4000, "HOLD", 0),
            ("2026-08-03", "MSFT", ["HOLD", "HOLD", "HOLD"], "HOLD", 1.0, 0, "ACCUMULATE", 3000),
            ("2026-08-04", "AAPL", ["ACCUMULATE", "BUY", "TRIM"], "HOLD", 0.33, 0, "HOLD", 0)]
    for day, sym, votes, v, c, a, cv, ca in rows:
        _write_transcript(mem, vr.SEALED_ARM_SUBDIR, day, sym, v, c, a, votes)
        _write_transcript(mem, ".backtest", day, sym, cv, 0.62, ca)


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


def test_run_arm_writes_only_its_own_artifacts_and_report_is_anonymous(review_env, tmp_path):
    _seed_arm_and_control(review_env)
    res = vr.run_arm()
    assert res["status"] == "ok" and res["n_arm"] == 3 and res["n_matched"] == 3
    jl = review_env / ".dreams" / "verdict_review_sealed.jsonl"
    assert Path(res["jsonl"]) == jl
    rows = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3 and {r["source"] for r in rows} == {"arm:sealed"}
    report = Path(res["report"])
    assert report == tmp_path / "docs" / "verdict_accuracy_sealed.md"
    text = report.read_text(encoding="utf-8")
    assert "实验臂" in text and "全票一致" in text and "n<30 不展示" in text
    assert "AAPL" not in text and "MSFT" not in text, "报告只含聚合，不能出现 symbol"
    # 没有臂目录 → skipped，不写任何文件
    (tmp_path / "docs" / "verdict_accuracy_sealed.md").unlink()
    jl.unlink()
    import shutil
    shutil.rmtree(review_env / vr.SEALED_ARM_SUBDIR)
    assert vr.run_arm()["status"] == "skipped"
    assert not jl.exists() and not report.exists()


def test_run_arm_never_pollutes_live_jsonl_lookup_or_public_json(review_env):
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
    _seed_arm_and_control(review_env)        # 对照行进 .backtest（舰队口径本来就会收），臂行进 .backtest_sealed
    arm_dir, parked = review_env / vr.SEALED_ARM_SUBDIR, review_env / "_parked"

    shutil.move(str(arm_dir), str(parked))   # 基线：没有实验臂
    assert vr.run()["status"] == "ok"
    baseline = snapshot()
    assert not (review_env / ".dreams" / "verdict_review_sealed.jsonl").exists()

    shutil.move(str(parked), str(arm_dir))   # 实验臂回来；run() 尾巴会调 run_arm()
    assert vr.run()["status"] == "ok"
    assert snapshot() == baseline, "实验臂不得改变 live jsonl / confidence_lookup / 公开 JSON"
    assert all(not str(json.loads(x)["source"]).startswith("arm:") for x in baseline[0].splitlines())
    sealed = (review_env / ".dreams" / "verdict_review_sealed.jsonl").read_text(encoding="utf-8")
    assert len(sealed.splitlines()) == 3     # 臂行只在独立 jsonl


def test_run_survives_a_broken_arm_review(review_env, monkeypatch):
    """实验臂复盘炸了，live 复盘照常返回 ok（不拖垮已写完的 live 产物）。"""
    _write_transcript(review_env, ".committee", "2026-08-03", "AAPL", "HOLD", 0.62, 0)
    monkeypatch.setattr(vr, "run_arm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("arm boom")))
    assert vr.run()["status"] == "ok"
    assert (review_env / ".dreams" / "verdict_review.jsonl").exists()
