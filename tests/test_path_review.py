"""path_review 后验校准单测 — 实际路径口径、评分、汇总、成熟度过滤。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import openinvest.jobs.path_review as pr


@pytest.fixture(autouse=True)
def _clear_hist():
    pr._HIST.clear()
    yield
    pr._HIST.clear()


def _stub_closes(monkeypatch, values, start="2024-01-01"):
    s = pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"))
    monkeypatch.setattr(pr, "_closes", lambda sym: s)
    return s


def test_realized_path_exact(monkeypatch):
    """线性上行：fwd/min/max/tmin/tmax 逐位精确（与 frame 同口径：日历日对齐）"""
    n = 200
    _stub_closes(monkeypatch, [100.0 + i for i in range(n)])
    real = pr.realized_path("X", "2024-02-01")  # i=31, base=131
    assert real is not None
    assert abs(real["fwd_30d"] - (161 - 131) / 131 * 100) < 1e-3  # 输出 round 3 位
    assert abs(real["fwd_90d"] - (221 - 131) / 131 * 100) < 1e-3
    # 严格递增：窗内最低=次日、最高=窗末
    assert real["tmin_90d"] == 1
    assert abs(real["min_90d"] - (132 - 131) / 131 * 100) < 1e-3
    assert real["tmax_90d"] == 90
    assert abs(real["max_90d"] - (221 - 131) / 131 * 100) < 1e-3


def test_realized_path_maturity_filter(monkeypatch):
    """30d 未走完 → None；60/90 未走完 → 字段缺省"""
    _stub_closes(monkeypatch, [100.0] * 50)  # 数据只到 2024-02-19
    assert pr.realized_path("X", "2024-02-15") is None      # 30d 不成熟
    real = pr.realized_path("X", "2024-01-05")              # 30d 成熟、60/90 不成熟
    assert real is not None and "fwd_30d" in real
    assert "fwd_60d" not in real and "fwd_90d" not in real


def test_realized_shape_classes():
    atr = 1.0
    assert pr.realized_shape({"fwd_90d": 5.0, "min_90d": -2.0, "max_90d": 6.0}, atr) == "dip_then_up"
    assert pr.realized_shape({"fwd_90d": 5.0, "min_90d": -0.5, "max_90d": 6.0}, atr) == "up_no_dip"
    assert pr.realized_shape({"fwd_90d": -3.0, "min_90d": -5.0, "max_90d": 4.0}, atr) == "pop_then_down"
    assert pr.realized_shape({"fwd_90d": -3.0, "min_90d": -5.0, "max_90d": 0.5}, atr) == "down_no_pop"
    assert pr.realized_shape({"fwd_90d": -3.0, "min_90d": -5.0, "max_90d": 0.5}, None) is None


def _snap(profile):
    return {"schema": 1, "date": "2024-02-01", "symbol": "X", "regime": "uptrend",
            "current_price": 131.0, "atr_pct": 1.0, "profile": profile}


def test_score_one_band_and_brier_inputs(monkeypatch):
    n = 200
    _stub_closes(monkeypatch, [100.0 + i for i in range(n)])
    profile = {
        "windows": {
            "30d": {"median_pct": 10.0, "p_below": 0.3, "p10_pct": -5.0, "p90_pct": 40.0},
            "90d": {"median_pct": 50.0, "p_below": 0.2, "p10_pct": 60.0, "p90_pct": 90.0},
        },
        "shape": {
            "pct_dip_then_up": 0.1, "pct_up_no_dip": 0.6,
            "pct_pop_then_down": 0.2, "pct_down_no_pop": 0.1,
            "days_to_trough_median": 10,
        },
    }
    rv = pr.score_one(_snap(profile), "recompute")
    assert rv is not None
    w30 = rv.windows["30d"]   # 实际 fwd30 ≈ +22.9%
    assert w30["in_band"] is True and w30["below_current"] is False
    assert abs(w30["err_vs_median"] - ((161 - 131) / 131 * 100 - 10.0)) < 1e-3
    w90 = rv.windows["90d"]   # 实际 ≈ +68.7%，带 [60,90] → in
    assert w90["in_band"] is True
    # 严格递增无显著回踩 → up_no_dip；预测 top1 也是 up_no_dip → 命中
    assert rv.shape_real == "up_no_dip"
    assert rv.shape_top1_hit is True
    assert rv.shape_prob_score == 0.6


def test_summarize_math():
    rvs = []
    for in_band, below, p_below in [(True, False, 0.2), (False, True, 0.2)]:
        rv = pr.PathReview(date="d", asset="X", regime="uptrend",
                           source="recompute", current_price=1.0)
        rv.windows["30d"] = {"fwd_real": 1.0, "in_band": in_band,
                             "below_current": below, "p_below": p_below,
                             "err_vs_median": 1.0}
        rvs.append(rv)
    s = pr.summarize(rvs)
    x = s["windows"]["30d"]
    assert x["band_coverage"] == 0.5
    # Brier = mean((0.2-0)^2, (0.2-1)^2) = (0.04+0.64)/2 = 0.34
    assert abs(x["p_below_brier"] - 0.34) < 1e-6
    # 基率=0.5 → Brier=0.25
    assert abs(x["base_rate_brier"] - 0.25) < 1e-6


def test_collect_live_snapshots(tmp_path, monkeypatch):
    import openinvest.core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    d = tmp_path / ".committee" / "2026-06-01"
    d.mkdir(parents=True)
    (d / "X_path.json").write_text('{"date": "2026-06-01", "symbol": "X"}')
    (d / "bad_path.json").write_text("{broken")
    snaps = pr.collect_live_snapshots()
    assert len(snaps) == 1 and snaps[0]["symbol"] == "X"


# ---------- run() 端到端（回归：本体之前零覆盖，只测过 collect/score/summarize 子函数）----------

def test_run_live_path_end_to_end(monkeypatch, tmp_path):
    """种 .committee/<date>/<sym>_path.json → run() 真的 collect → score → summarize
    → write_outputs 落盘非空。run() 是 cron 实际调用的入口，之前没有任何测试驱动过
    它本体——MEMORY_ROOT 解析错、字段名对不上、summarize 输入形状变了，都不会被
    现有测试（只测子函数）发现。"""
    import openinvest.core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(pr, "ROOT", tmp_path)  # docs/path_calibration.md 别写进真仓库
    (tmp_path / "docs").mkdir()  # write_outputs 只 mkdir jsonl 的父目录，md 的要自己建

    _stub_closes(monkeypatch, [100.0 + i for i in range(200)])  # 2024-01-01 起线性上行

    profile = {
        "windows": {
            "30d": {"median_pct": 10.0, "p_below": 0.3, "p10_pct": -5.0, "p90_pct": 40.0},
            "60d": {"median_pct": 30.0, "p_below": 0.25, "p10_pct": 10.0, "p90_pct": 60.0},
            "90d": {"median_pct": 50.0, "p_below": 0.2, "p10_pct": 40.0, "p90_pct": 90.0},
        },
    }
    d = tmp_path / ".committee" / "2024-02-01"
    d.mkdir(parents=True)
    (d / "AAPL_path.json").write_text(
        json.dumps({"date": "2024-02-01", "symbol": "AAPL", "regime": "uptrend",
                    "current_price": 131.0, "atr_pct": 1.0, "profile": profile}),
        encoding="utf-8",
    )

    out = pr.run()

    assert out["reviews"] == 1, "应评到种入的这条 live 快照"
    assert out["summary"]["n"] == 1
    assert set(out["summary"]["windows"]) == {"30d", "60d", "90d"}
    jl = Path(out["jsonl"])
    assert jl.exists() and jl.read_text().strip() != ""
    md = Path(out["report"])
    assert md.exists() and "路径预测校准报告" in md.read_text()
