"""A5：自报置信度校准 + 裁决中性度（review_calc 纯函数 + verdict_review 接线 + 日报一行）

只测量/展示，不碰裁决逻辑。数值用手算例子钉死。
"""
from __future__ import annotations

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.jobs import review_calc as rc
from openinvest.jobs import verdict_review as vr
from openinvest.jobs.daily_report_builder import assemble_full_report


def _rv(verdict="HOLD", conf=0.6, hit=True, mkt="flat", *, date="2026-09-01", source="live",
        contaminated=False, hits=None, directions=None) -> rc.VerdictReview:
    return rc.VerdictReview(
        date=date, asset="NDQ.AX", verdict=verdict, confidence=conf,
        expected_direction=rc.EXPECTED_DIRECTION.get(verdict, rc.UNCLEAR_DIRECTION),
        macro_at_decision={},
        hits={"30d": hit} if hits is None else hits,
        directions={"30d": mkt} if directions is None else directions,
        source=source, contaminated=contaminated,
    )


# ---------- 校准：手算例子 ----------

def _hand_example():
    """10 条：
    ≤0.4 档  2 条 conf 0.4 hits [0,0]            → 命中率 0，   Brier (0.16+0.16)/2 = 0.16
    0.6–0.7  4 条 conf 0.6 hits [1,1,1,0]        → 命中率 0.75，Brier (3×0.16+0.36)/4 = 0.21
    0.7–0.8  4 条 conf 0.8 hits [1,0,0,0]        → 命中率 0.25，Brier (0.04+3×0.64)/4 = 0.49
    整体：Brier = (0.32+0.84+1.96)/10 = 0.312；p̄ = 4/10 = 0.4 → 基率 Brier = 0.4×0.6 = 0.24
    ECE = (2×|0.4−0| + 4×|0.6−0.75| + 4×|0.8−0.25|)/10 = (0.8+0.6+2.2)/10 = 0.36
    """
    rows = [_rv(conf=0.4, hit=False)] * 2
    rows += [_rv(conf=0.6, hit=h) for h in (True, True, True, False)]
    rows += [_rv(conf=0.8, hit=h) for h in (True, False, False, False)]
    return rows


def test_calibration_hand_computed():
    cal = rc.calibration_summary(_hand_example(), min_n=2)
    assert cal["n"] == 10 and cal["window"] == "30d"
    assert cal["hit_rate"] == pytest.approx(0.4)
    assert cal["brier"] == pytest.approx(0.312)
    assert cal["base_rate_brier"] == pytest.approx(0.24)
    assert cal["ece"] == pytest.approx(0.36)
    b = {x["label"]: x for x in cal["bins"]}
    assert [x["label"] for x in cal["bins"]] == list(rc.CALIBRATION_BINS)
    assert (b["≤0.4"]["n"], b["≤0.4"]["hit_rate"], b["≤0.4"]["brier"]) == (2, 0.0, pytest.approx(0.16))
    assert (b["0.6–0.7"]["n"], b["0.6–0.7"]["hit_rate"], b["0.6–0.7"]["brier"]) == (4, 0.75, pytest.approx(0.21))
    assert (b["0.7–0.8"]["n"], b["0.7–0.8"]["hit_rate"], b["0.7–0.8"]["brier"]) == (4, 0.25, pytest.approx(0.49))
    # 各档用同一个 p̄=0.4 当常数：0.6–0.7 档 (3×0.36+0.16)/4 = 0.31
    assert b["0.6–0.7"]["base_rate_brier"] == pytest.approx(0.31)
    # 没有样本的档：n=0，数字 None
    assert b["0.4–0.6"]["n"] == 0 and b["0.4–0.6"]["hit_rate"] is None
    # 各档 Brier 按 n 加权 = 整体；基率同理
    for key, total in (("brier", cal["brier"]), ("base_rate_brier", cal["base_rate_brier"])):
        assert sum(x["n"] * x[key] for x in cal["bins"] if x[key] is not None) / 10 == pytest.approx(total)


@pytest.mark.parametrize("conf,idx", [(0.0, 0), (0.4, 0), (0.41, 1), (0.58, 1), (0.6, 2), (0.62, 2),
                                      (0.69, 2), (0.7, 3), (0.8, 3), (0.81, 4), (0.85, 4)])
def test_confidence_bin_edges(conf, idx):
    """0.6 归 0.6–0.7（降级常量），0.8 归 0.7–0.8，>0.8 才是最高档。"""
    assert rc._confidence_bin(conf) == idx


def test_calibration_float_noise_does_not_cross_edge():
    assert rc._confidence_bin(0.1 + 0.2 + 0.3) == 2     # 0.6000000000000001 仍在 0.6–0.7


def test_calibration_bin_suppressed_below_30():
    """档 n<30 不出数字（n 保留）；整体 n≥30 照出，且整体仍含这档的行。"""
    rows = [_rv(conf=0.62, hit=i % 2 == 0) for i in range(30)] + [_rv(conf=0.85, hit=True)] * 5
    cal = rc.calibration_summary(rows)
    b = {x["label"]: x for x in cal["bins"]}
    assert b["0.6–0.7"]["n"] == 30 and b["0.6–0.7"]["hit_rate"] == 0.5
    assert b[">0.8"]["n"] == 5
    assert all(b[">0.8"][k] is None for k in ("hit_rate", "mean_confidence", "brier", "base_rate_brier"))
    assert cal["n"] == 35 and cal["ece"] is not None


def test_calibration_overall_suppressed_below_30():
    cal = rc.calibration_summary([_rv(conf=0.62)] * 29)
    assert cal["n"] == 29
    assert all(cal[k] is None for k in ("hit_rate", "ece", "brier", "base_rate_brier"))
    assert all(x["hit_rate"] is None for x in cal["bins"])
    assert rc.calibration_summary([_rv(conf=0.62)] * 30)["ece"] is not None


def test_calibration_only_rows_with_hits():
    """UNCLEAR（不打 hits）、窗口未成熟（无 hits[window]）都不算；换窗口读别的键。"""
    rows = [_rv("UNCLEAR", conf=0.0, hits={}, directions={"30d": "flat"}),
            _rv(conf=0.6, hits={}),                                  # 30d 未成熟
            _rv(conf=0.6, hits={"7d": False})]
    assert rc.calibration_summary(rows, min_n=1)["n"] == 0
    assert rc.calibration_summary(rows, "7d", min_n=1)["n"] == 1
    assert rc.calibration_summary([], min_n=1)["ece"] is None


def test_calibration_excludes_zero_confidence_rows_and_counts_them():
    """自报恰为 0.0 = cio_parse 解析失败兜底，不是"0% 把握"：不进校准，单列 n_zero_conf（评审 #455-4）。"""
    ref = rc.calibration_summary(_hand_example(), min_n=2)
    cal = rc.calibration_summary(_hand_example() + [_rv(conf=0.0, hit=True)] * 2, min_n=2)
    assert (ref["n"], ref["n_zero_conf"]) == (10, 0)
    assert (cal["n"], cal["n_zero_conf"]) == (10, 2)
    for k in ("hit_rate", "ece", "brier", "base_rate_brier"):
        assert cal[k] == ref[k]
    assert cal["bins"][0]["n"] == 2                                   # ≤0.4 档没被 0.0 行撑大
    md = "\n".join(rc._calibration_lines(cal))
    assert "另有 2 条自报恰为 0.0" in md and "另有" not in "\n".join(rc._calibration_lines(ref))
    only_zero = rc.calibration_summary([_rv(conf=0.0)] * 3, min_n=1)  # 全是 0.0：n=0，只剩计数
    assert (only_zero["n"], only_zero["n_zero_conf"], only_zero["ece"]) == (0, 3, None)


# ---------- 中性度 ----------

def _neutrality_rows():
    # 8 HOLD / 1 ACCUMULATE / 1 TRIM / 2 UNCLEAR = 12；市场 6 flat / 4 down / 2 up
    mk = ["flat"] * 6 + ["down"] * 4 + ["up"] * 2
    vs = ["HOLD"] * 8 + ["ACCUMULATE", "TRIM"] + ["UNCLEAR"] * 2
    return [_rv(v, hits={} if v == "UNCLEAR" else {"30d": True}, mkt=m) for v, m in zip(vs, mk)]


def test_neutrality_distribution_and_sentence():
    neu = rc.neutrality_summary(_neutrality_rows(), min_n=1)
    assert neu["n"] == 12
    assert {v: c["n"] for v, c in neu["verdicts"].items()} == {
        "BUY": 0, "ACCUMULATE": 1, "HOLD": 8, "TRIM": 1, "SELL": 0, "UNCLEAR": 2}
    assert neu["verdicts"]["HOLD"]["share"] == pytest.approx(0.667, abs=1e-3)
    assert {d: c["n"] for d, c in neu["market"].items()} == {"up": 2, "down": 4, "flat": 6}
    assert neu["market"]["flat"]["share"] == 0.5
    s = neu["sentence"]
    assert "过去 12 条" in s and "67% 是 HOLD" in s and "50% 横盘" in s and "BUY 0 条、SELL 0 条" in s


def test_neutrality_n_below_30_has_no_sentence_and_ignores_immature():
    rows = _neutrality_rows() + [_rv("HOLD", directions={})]        # 窗口未成熟的行不进分母
    neu = rc.neutrality_summary(rows)
    assert neu["n"] == 12 and neu["sentence"] is None
    assert rc.neutrality_summary([])["n"] == 0


def test_neutrality_unknown_verdict_counted_as_unclear():
    neu = rc.neutrality_summary([_rv("???", hits={})], min_n=1)
    assert neu["verdicts"]["UNCLEAR"]["n"] == 1


# ---------- summarize / 报告接线 ----------

def test_summarize_adds_sections_live_holdout_only():
    live = [_rv(conf=0.62, date="2026-09-01")] * 3
    reviews = (live
               + [_rv(conf=0.62, source="backtest", date="2026-09-01")] * 4
               + [_rv(conf=0.62, contaminated=True, date="2024-06-03")] * 5)
    s = vr.summarize(reviews)
    assert s["calibration"]["n"] == 3 and s["neutrality"]["n"] == 3
    assert set(s) == {"total", "weekend_dup_excluded", "cutoff", "calibration", "neutrality",
                      "holdout", "contaminated"}


def test_write_report_renders_both_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    rows = _hand_example() * 3 + _neutrality_rows() * 3          # 30 + 36 条
    summary = vr.summarize(rows)
    md = vr.write_report(rows, summary).read_text(encoding="utf-8")
    assert "## 置信度校准" in md and "| 自报档 | N |" in md and "| 0.6–0.7 | 42 |" in md
    assert "ECE" in md and "基率 Brier" in md
    assert "## 裁决中性度" in md and "| HOLD |" in md and "同期市场" in md


def test_report_tables_degrade_when_small(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    rows = [_rv(conf=0.62)] * 5
    md = vr.write_report(rows, vr.summarize(rows)).read_text(encoding="utf-8")
    assert "按红线 #2 不展示具体数字" in md and "不出结论" in md


def test_neutrality_persisted_and_loaded_for_daily_report(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "MemoryStore", lambda: MemoryStore(tmp_path))
    assert vr.load_neutrality() is None                              # 没复盘过
    summary = vr.summarize(_neutrality_rows() * 3)                   # 36 条 ≥30
    vr.write_neutrality(summary)
    neu = vr.load_neutrality()
    assert neu["sentence"].startswith("过去 36 条") and len(neu["as_of"]) == 10
    vr.write_neutrality(vr.summarize(_neutrality_rows()))            # 样本掉到 <30 → 没有 sentence → 不出行
    assert vr.load_neutrality() is None
    (tmp_path / ".dreams" / "verdict_neutrality.json").write_text("{bad")
    assert vr.load_neutrality() is None                              # 坏文件只降级


def test_load_neutrality_staleness_gate(tmp_path, monkeypatch):
    """复盘 cron 停了：generated_at 超过 3 天 → None（日报不出这行）；缺失/非法同样视为过期（评审 #455-2）。"""
    import json
    from datetime import datetime, timedelta
    monkeypatch.setattr(vr, "MemoryStore", lambda: MemoryStore(tmp_path))
    vr.write_neutrality(vr.summarize(_neutrality_rows() * 3))
    path = tmp_path / ".dreams" / "verdict_neutrality.json"
    gen = datetime.fromisoformat(json.loads(path.read_text())["generated_at"])
    assert vr.load_neutrality()["sentence"]                                              # 刚写的：新鲜
    assert vr.load_neutrality(now=gen + timedelta(days=3))["as_of"] == gen.date().isoformat()   # 恰好 3 天：仍可用
    assert vr.load_neutrality(now=gen + timedelta(days=3, seconds=1)) is None            # 超过 3 天：过期
    data = json.loads(path.read_text())
    for bad in (None, "", "not-a-date"):
        path.write_text(json.dumps({**data, "generated_at": bad}))
        assert vr.load_neutrality() is None
    path.write_text(json.dumps({k: v for k, v in data.items() if k != "generated_at"}))
    assert vr.load_neutrality() is None


# ---------- 日报一行（email / chat 同一文本）----------

def _report(target, neutrality):
    return assemble_full_report(
        today="2026-10-17", macro_view="", gold_snapshot_text="", friction_report="",
        target_assets=[], asset_committees={}, skipped_assets=set(), total_assets_cny=0.0,
        final_decision_gemini="", render_target=target, neutrality=neutrality)


def test_daily_report_renders_neutrality_line_in_both_variants():
    neu = {"sentence": "过去 246 条已成熟裁决（30d）中 79% 是 HOLD，同期市场 69% 横盘（20% 下跌 / 12% 上涨）；"
                       "BUY 0 条、SELL 0 条。", "as_of": "2026-10-17"}
    bodies = [_report(t, neu) for t in ("email", "chat")]
    for body in bodies:
        assert "**裁决中性度**（2026-10-17复盘）: 过去 246 条已成熟裁决（30d）中 79% 是 HOLD" in body
    line = [ln for ln in bodies[0].splitlines() if ln.startswith("**裁决中性度**（")]
    assert line == [ln for ln in bodies[1].splitlines() if ln.startswith("**裁决中性度**（")]   # 同一文本


def test_daily_report_omits_neutrality_line_without_review_result():
    for neu in (None, {}, {"sentence": None, "as_of": "2026-10-17"}):
        for t in ("email", "chat"):
            assert "裁决中性度**（" not in _report(t, neu)
