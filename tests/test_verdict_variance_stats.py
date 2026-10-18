"""A4 研究脚本 experiments/verdict_variance/run_variance.py 的统计部分（手算例子；不调 LLM）"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parent.parent / "experiments" / "verdict_variance" / "run_variance.py"
_spec = importlib.util.spec_from_file_location("run_variance", _PATH)
rv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rv)


def _run(verdict, conf, alloc):
    return {"verdict": verdict, "confidence": conf, "alloc": alloc}


def _items():
    return [
        {"date": "d1", "symbol": "X", "orig": {"verdict": "HOLD"},
         "runs": [_run("HOLD", 0.6, 0), _run("ACCUMULATE", 0.7, 1000), _run("ACCUMULATE", 0.8, 3000)]},
        {"date": "d2", "symbol": "X", "orig": {"verdict": "ACCUMULATE"},
         "runs": [_run("HOLD", 0.6, 0)] * 3},
        {"date": "d3", "symbol": "Y", "orig": {"verdict": "HOLD"},       # 只有 1 次有效重跑 → 算不出方差，剔除
         "runs": [_run("HOLD", 0.6, 0), {"error": "boom"}, {"error": "pending"}]},
    ]


def test_item_stats_hand_computed():
    s = rv.item_stats(_items()[0]["runs"])
    assert s["direction_flip"] and s["label_flip"]
    assert s["pair_flip_rate"] == pytest.approx(2 / 3)          # (flat,up)(flat,up)(up,up)
    assert s["conf_std"] == pytest.approx(0.1)                  # stdev(0.6,0.7,0.8)
    # |alloc| = 0,1000,3000 → 均值 4000/3，样本标准差 sqrt(7e6/3) → CV = sqrt(7e6/3)/(4000/3)
    assert s["alloc_cv"] == pytest.approx((7e6 / 3) ** 0.5 / (4000 / 3))
    assert rv.item_stats(_items()[2]["runs"]) is None


def test_item_stats_all_hold_zero_alloc():
    s = rv.item_stats(_items()[1]["runs"])
    assert not s["direction_flip"] and s["pair_flip_rate"] == 0 and s["conf_std"] == 0
    assert s["alloc_cv"] is None                                 # 均值 0 不算 CV


def test_summarize_hand_computed():
    s = rv.summarize(_items())
    assert (s["n_items"], s["distinct_symbols"], s["n_runs"]) == (2, 1, 6)
    assert s["direction_flip_rate"] == 0.5
    assert s["pair_flip_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["label_flip_rate"] == 0.5
    assert s["conf_std_mean"] == pytest.approx(0.05)
    assert s["alloc_cv_n_items"] == 1 and s["alloc_all_zero_items"] == 1
    # 重跑 vs 原决议：d1 flat,up,up vs flat → 2/3 不同；d2 flat×3 vs up → 全不同 → 5/6
    assert s["rerun_vs_orig_direction_diff_rate"] == pytest.approx(5 / 6, abs=1e-3)
    assert s["by_symbol"]["X"]["verdict_counts"] == {"ACCUMULATE": 2, "HOLD": 4}


def test_unclear_is_its_own_direction():
    assert rv.direction_of("UNCLEAR") == "none" and rv.direction_of("trim") == "down"


# ---------- 可提交的匿名汇总（评审 #455-1：逐日裁决 + 金额不进仓库）----------

def test_public_summary_is_anonymous_and_aggregate_only():
    import json
    items = [{"date": "2030-01-02", "symbol": "AAA.HK", "orig": {"verdict": "HOLD", "alloc": 777.0},
              "runs": [_run("HOLD", 0.6, 0), _run("ACCUMULATE", 0.62, 777), _run("HOLD", 0.6, 0)]},
             {"date": "2030-01-03", "symbol": "BBB.AX", "orig": {"verdict": "HOLD"},
              "runs": [_run("HOLD", 0.6, 0)] * 3},
             {"date": "2030-01-04", "symbol": "BBB.AX", "orig": {"verdict": "HOLD"},
              "runs": [_run("HOLD", 0.6, 0)] * 3}]
    pub = rv.public_summary(items, {"n": 3, "limit": 3, "max_rounds": 1, "generated_at": "2030-01-05T00:00:00+00:00"})
    text = json.dumps(pub, ensure_ascii=False)
    for forbidden in ("AAA.HK", "BBB", "2030-", "777", '"items"', '"runs"', '"date"', '"alloc"', "generated_at"):
        assert forbidden not in text, forbidden
    assert pub["meta"] == {"n": 3, "limit": 3, "max_rounds": 1}
    by = pub["summary"]["by_symbol"]
    assert list(by) == ["A", "B"] and by["A"]["n_runs"] == 6 and by["B"]["n_runs"] == 3   # 重跑数降序
    assert by["B"]["verdict_counts"] == {"ACCUMULATE": 1, "HOLD": 2}
    assert pub["summary"]["n_items"] == 3 and pub["summary"]["distinct_symbols"] == 2


# ---------- 守卫（评审 #455-3）----------

def test_guard_home_needs_explicit_copy_declaration(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    with pytest.raises(SystemExit):
        rv.guard_home("")                                          # 没设 INVEST_HOME
    with pytest.raises(SystemExit):
        rv.guard_home(str(copy))                                   # 没哨兵、没显式声明：比如 PyPI 形态的生产数据目录
    assert rv.guard_home(str(copy), acknowledged=True) == copy.resolve()      # 显式 --i-know-this-is-a-copy
    (copy / rv.SENTINEL).touch()
    assert rv.guard_home(str(copy)) == copy.resolve()                          # 哨兵文件
    for marker in (".git", "pyproject.toml"):                      # checkout 一律拒绝，哨兵 / 声明都救不了
        co = tmp_path / ("co" + marker)
        co.mkdir()
        (co / marker).touch()
        (co / rv.SENTINEL).touch()
        with pytest.raises(SystemExit):
            rv.guard_home(str(co), acknowledged=True)


def test_guard_out_refuses_paths_inside_repo(tmp_path):
    with pytest.raises(SystemExit):
        rv.guard_out(rv.REPO / "experiments" / "verdict_variance" / "data" / "variance.json")
    with pytest.raises(SystemExit):
        rv.guard_out(rv.REPO / "variance.json")
    ok = tmp_path / "memory" / ".dreams" / "verdict_variance" / "variance.json"
    assert rv.guard_out(ok) == ok.resolve()
