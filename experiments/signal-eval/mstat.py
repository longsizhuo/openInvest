"""mstat — 评测统计层(M-stat,优化计划 v5 第一等公民)。

每个函数钉参考实现,对已知答案 fixture 单测(见 test_mstat.py)。闸子的可信度全吊在这层,
所以宁可慢、宁可对着 scipy/statsmodels 的 canonical 调用钉死,也不自己手搓出微妙错的统计。

研究依赖(scipy/statsmodels)经 `uv run --with scipy --with statsmodels` 注入,**不进 production
pyproject**(同 repo DSPy 用 `--with` 的约定)。scipy/statsmodels 在函数内惰性 import,使本模块
在没装这些包的环境也能 import(只有真调用才需要)。numpy 是 pandas 的传递依赖,base env 即有。

参考:
- Spearman rank-IC:scipy.stats.spearmanr
- Newey-West HAC t:statsmodels OLS(cov_type='HAC');auto 带宽 = Newey-West(1994) floor(4·(n/100)^(2/9))
- PSR / Deflated Sharpe:Bailey & López de Prado(2012 PSR;2014 DSR,期望最大 SR 用 Gumbel 近似)
- Holm:step-down,等价 statsmodels.stats.multitest.multipletests('holm')
- 两样本桶差异:scipy.stats.mannwhitneyu(非参,稳健于非正态收益)

⚠ 多重比较族:Q1("所有试过的腿")与 Q2("3 资产 × 窗口 × regime 对")是两个独立族,各自 holm()。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence

import numpy as np

_EULER_MASCHERONI = 0.5772156649015329


def rank_ic(signal: Sequence[float], fwd: Sequence[float]) -> float:
    """单期横截面 Spearman rank-IC(signal vs forward return)。NaN 成对剔除;
    有效对 < 3 返回 nan。构造上差掉了"所有标的共有的 forward return"(只测相对排序)。"""
    from scipy.stats import spearmanr

    s = np.asarray(signal, dtype=float)
    f = np.asarray(fwd, dtype=float)
    m = np.isfinite(s) & np.isfinite(f)
    if m.sum() < 3:
        return float("nan")
    rho, _ = spearmanr(s[m], f[m])
    return float(rho)


def icir(ics: Sequence[float]) -> float:
    """IC 信息比率 = mean(IC)/std(IC)(跨期,ddof=1)。<2 个有效值或零方差 → nan。"""
    a = np.asarray([x for x in ics if np.isfinite(x)], dtype=float)
    if len(a) < 2:
        return float("nan")
    sd = a.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(a.mean() / sd)


def nw_auto_lag(n: int) -> int:
    """Newey-West(1994) 自动带宽 floor(4·(n/100)^(2/9))。"""
    if n < 1:
        return 0
    return int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0)))


def nw_tstat(x: Sequence[float], lag: Optional[int] = None) -> float:
    """序列 x 检验 H0:mean=0 的 Newey-West(HAC)t 统计量。
    lag=None → 自动带宽(nw_auto_lag);lag=0 → White。<3 个有效值 → nan。
    实现走 statsmodels OLS(x~1, cov_type='HAC') 的 const t 值——canonical,fixture 钉死。"""
    import statsmodels.api as sm

    a = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    n = len(a)
    if n < 3:
        return float("nan")
    L = nw_auto_lag(n) if lag is None else int(lag)
    res = sm.OLS(a, np.ones(n)).fit(
        cov_type="HAC", cov_kwds={"maxlags": L, "use_correction": True}
    )
    return float(res.tvalues[0])


def n_eff_breadth(weights: Sequence[float]) -> float:
    """有效宽度 N_eff = 1/Σwᵢ²(w 归一到和=1)。等权 N → N;集中 → <N。
    ⚠ 这是"参与度/breadth"口径,不是重叠窗口的有效独立样本数——后者 Q2 走
    regime_probability 的 effective_n(已有)。不要拿 raw N 套 √breadth,用本函数。"""
    w = np.asarray(weights, dtype=float)
    w = w[np.isfinite(w) & (w > 0)]
    if w.sum() <= 0:
        return 0.0
    w = w / w.sum()
    return float(1.0 / np.square(w).sum())


def psr(sr: float, T: int, *, sr_benchmark: float = 0.0,
        skew: float = 0.0, kurt: float = 3.0) -> float:
    """Probabilistic Sharpe Ratio(Bailey-LdP 2012)。sr / sr_benchmark 同周期(非年化);
    skew/kurt 为收益偏度/峰度(正态 kurt=3)。返回 P(真 SR > sr_benchmark)。
    skew=0,kurt=3,benchmark=0 → 退化 Φ(sr·√(T−1))。"""
    from scipy.stats import norm

    if T < 2:
        return float("nan")
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr))
    z = (sr - sr_benchmark) * math.sqrt(T - 1.0) / denom
    return float(norm.cdf(z))


def expected_max_sharpe(sr_std_trials: float, n_trials: int) -> float:
    """N 次独立试验下 SR 最大值的期望(Bailey-LdP 2014,Gumbel 近似):
    sr_std·[(1−γ)·Z⁻¹(1−1/N) + γ·Z⁻¹(1−1/(N·e))],γ=Euler-Mascheroni。N≤1 → 0。
    这是 DSR 的"选择偏差基准"——试过越多腿,光靠运气能达到的最大 SR 越高。"""
    from scipy.stats import norm

    if n_trials <= 1 or sr_std_trials <= 0:
        return 0.0
    g = _EULER_MASCHERONI
    return float(
        sr_std_trials
        * ((1 - g) * norm.ppf(1 - 1.0 / n_trials) + g * norm.ppf(1 - 1.0 / (n_trials * math.e)))
    )


def deflated_sharpe(sr: float, T: int, n_trials: int, sr_std_trials: float,
                    *, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Deflated Sharpe Ratio(Bailey-LdP 2014):PSR 相对"N 次试验期望最大 SR"的基准。
    sr_std_trials = 各试验 SR 的标准差(选择效应规模)。返回 DSR∈[0,1];铁律阈值 >0.95。
    n_trials=1 → 基准 0 → 退化 PSR(sr vs 0)。"""
    sr0 = expected_max_sharpe(sr_std_trials, n_trials)
    return psr(sr, T, sr_benchmark=sr0, skew=skew, kurt=kurt)


def holm(pvalues: Sequence[float]) -> list:
    """Holm-Bonferroni step-down 校正,返回 adjusted p(与输入同序)。
    对【一个比较族】调用;Q1 与 Q2 是不同族,各调各的。等价 statsmodels multipletests('holm')。"""
    p = np.asarray(pvalues, dtype=float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p, kind="stable")
    adj = np.empty(n, dtype=float)
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (n - rank) * p[idx])
        adj[idx] = min(running, 1.0)
    return adj.tolist()


def two_sample_diff(a: Sequence[float], b: Sequence[float]) -> Dict[str, Any]:
    """两桶 forward return 差异检验(Mann-Whitney U,非参,双侧)。
    返回 {p, median_a, median_b, n_a, n_b, effect}(effect = 中位数差)。任一桶 <3 → p=nan。"""
    from scipy.stats import mannwhitneyu

    aa = np.asarray([x for x in a if np.isfinite(x)], dtype=float)
    bb = np.asarray([x for x in b if np.isfinite(x)], dtype=float)
    if len(aa) < 3 or len(bb) < 3:
        return {"p": float("nan"), "median_a": float("nan"), "median_b": float("nan"),
                "n_a": int(len(aa)), "n_b": int(len(bb)), "effect": float("nan")}
    _, p = mannwhitneyu(aa, bb, alternative="two-sided")
    return {"p": float(p), "median_a": float(np.median(aa)), "median_b": float(np.median(bb)),
            "n_a": int(len(aa)), "n_b": int(len(bb)), "effect": float(np.median(aa) - np.median(bb))}


__all__ = [
    "rank_ic", "icir", "nw_auto_lag", "nw_tstat", "n_eff_breadth",
    "psr", "expected_max_sharpe", "deflated_sharpe", "holm", "two_sample_diff",
]
