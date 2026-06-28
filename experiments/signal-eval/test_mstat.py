"""M-stat 已知答案 fixture 单测(优化计划 v5)。
跑法:`uv run --with scipy --with statsmodels python -m pytest experiments/signal-eval/test_mstat.py -q`
base CI(无 scipy)会 importorskip 跳过——这是研究依赖、不是安全护栏,跳过可接受。"""
import math
import os
import sys

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("statsmodels")

sys.path.insert(0, os.path.dirname(__file__))
import mstat  # noqa: E402


def test_rank_ic_perfect_and_reversed():
    assert mstat.rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert mstat.rank_ic([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_rank_ic_vs_scipy_and_nan_drop():
    from scipy.stats import spearmanr
    s, f = [1, 2, 3, 4, 5], [2, 1, 4, 3, 5]
    assert mstat.rank_ic(s, f) == pytest.approx(spearmanr(s, f)[0])
    # NaN 成对剔除:加一对含 nan,结果不变
    assert mstat.rank_ic(s + [float("nan")], f + [9.0]) == pytest.approx(spearmanr(s, f)[0])
    assert math.isnan(mstat.rank_ic([1, 2], [1, 2]))  # <3 有效对


def test_icir():
    assert math.isnan(mstat.icir([0.1, 0.1, 0.1, 0.1]))  # 零方差
    assert math.isnan(mstat.icir([0.1]))                 # <2
    assert mstat.icir([0.0, 0.2]) == pytest.approx(0.1 / np.std([0.0, 0.2], ddof=1))


def test_n_eff_breadth():
    assert mstat.n_eff_breadth([0.25, 0.25, 0.25, 0.25]) == pytest.approx(4.0)
    assert mstat.n_eff_breadth([1, 0, 0, 0]) == pytest.approx(1.0)
    assert mstat.n_eff_breadth([0.5, 0.5]) == pytest.approx(2.0)
    assert mstat.n_eff_breadth([0.7, 0.1, 0.1, 0.1]) < 4.0  # 不等权 < N
    assert mstat.n_eff_breadth([3, 3, 3, 3]) == pytest.approx(4.0)  # 未归一也对


def test_nw_auto_lag():
    assert mstat.nw_auto_lag(100) == math.floor(4 * (100 / 100.0) ** (2 / 9))  # = 4
    assert mstat.nw_auto_lag(0) == 0


def test_nw_tstat_vs_statsmodels():
    import statsmodels.api as sm
    rng = np.random.default_rng(0)
    x = list(rng.normal(0.05, 1.0, 200))
    L = mstat.nw_auto_lag(len(x))
    ref = sm.OLS(np.asarray(x), np.ones(len(x))).fit(
        cov_type="HAC", cov_kwds={"maxlags": L, "use_correction": True}
    ).tvalues[0]
    assert mstat.nw_tstat(x) == pytest.approx(float(ref))
    assert np.isfinite(mstat.nw_tstat(x, lag=0))     # White 路径
    assert math.isnan(mstat.nw_tstat([0.1, 0.2]))    # <3


def test_psr_normal_case():
    # Bailey-LdP 用 (γ4−1)/4 → 正态(kurt=3)分母 = √(1+0.5·SR²)(Lo 2002 修正),
    # 不是裸 Φ(SR√(T−1))。这正是"实现得松会出错"的那类:公式对、不是漏项。
    from scipy.stats import norm
    sr, T = 0.1, 50
    denom = math.sqrt(1 + 0.5 * sr * sr)
    assert mstat.psr(sr, T) == pytest.approx(norm.cdf(sr * math.sqrt(T - 1) / denom))
    # 非正态:负偏 + 厚尾 拉低 PSR(分母变大)
    assert mstat.psr(sr, T, skew=-1.0, kurt=6.0) < mstat.psr(sr, T)
    # 正偏抬高 PSR(−γ3·SR 使分母变小)
    assert mstat.psr(sr, T, skew=1.0, kurt=3.0) > mstat.psr(sr, T)


def test_expected_max_sharpe_monotonic():
    assert mstat.expected_max_sharpe(0.5, 1) == 0.0
    assert mstat.expected_max_sharpe(0.5, 0) == 0.0
    e10 = mstat.expected_max_sharpe(0.5, 10)
    e100 = mstat.expected_max_sharpe(0.5, 100)
    assert 0 < e10 < e100  # 试验越多,光靠运气的最大 SR 越高


def test_deflated_sharpe_invariants():
    # n_trials=1 → 基准 0 → 退化 PSR(sr vs 0)
    assert mstat.deflated_sharpe(0.2, 100, 1, 0.1) == pytest.approx(mstat.psr(0.2, 100))
    # 试过越多腿 → DSR 越低(越难显著)
    assert mstat.deflated_sharpe(0.2, 100, 50, 0.1) < mstat.deflated_sharpe(0.2, 100, 1, 0.1)
    # DSR ∈ [0,1]
    d = mstat.deflated_sharpe(0.2, 100, 20, 0.1)
    assert 0.0 <= d <= 1.0


def test_holm_known_and_vs_statsmodels():
    from statsmodels.stats.multitest import multipletests
    # 手算:p=[.01,.04,.03] sorted .01,.03,.04 ×(3,2,1)=.03,.06,.04 → cummax .03,.06,.06
    adj = mstat.holm([0.01, 0.04, 0.03])
    assert adj[0] == pytest.approx(0.03)
    assert adj[1] == pytest.approx(0.06)
    assert adj[2] == pytest.approx(0.06)
    ref = multipletests([0.01, 0.04, 0.03], method="holm")[1]
    assert mstat.holm([0.01, 0.04, 0.03]) == pytest.approx(list(ref))
    assert mstat.holm([]) == []


def test_two_sample_diff():
    r = mstat.two_sample_diff([0.1] * 10, [0.1] * 10)
    assert r["effect"] == pytest.approx(0.0)
    a = list(np.linspace(0.1, 0.2, 20))
    b = list(np.linspace(-0.2, -0.1, 20))
    r = mstat.two_sample_diff(a, b)
    assert r["p"] < 0.01 and r["effect"] > 0
    r = mstat.two_sample_diff([0.1, 0.2], [0.1] * 5)  # 一桶 <3
    assert math.isnan(r["p"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
