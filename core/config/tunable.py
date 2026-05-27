"""可调参数 dataclass — 走注入链（default → YAML → CLI → env）

这些参数可以被 sweep runner / CLI / env 自由覆盖。
locked 参数在 locked.py 里，不经过这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegimeConfig:
    """Regime 分类阈值 — 簇 1-4

    来源: core/regime.py:35-55 THRESHOLDS dict
    """
    # 簇 1: Crash 触发器（紧密耦合，联合 sweep）
    crash_atr_pct_min: float = 5.0
    crash_drawdown_30d_pct: float = 20.0
    crash_deep_drawdown_30d_pct: float = 30.0
    # 簇 2: Trend 趋势（半独立）
    trend_ma_spread_pct: float = 3.0
    # 簇 3: Recovery 判定（紧密耦合）
    recovery_rebound_pct: float = 10.0
    recovery_quantile_max: float = 0.50
    # 簇 4: 价格分位（弱耦合）
    low_quantile_threshold: float = 0.20
    high_quantile_threshold: float = 0.80


@dataclass(frozen=True)
class RegimePerAssetConfig:
    """单资产 Regime 覆盖

    来源: core/regime.py:69-85 ASSET_OVERRIDES dict
    None = 无覆盖，fallback 到 RegimeConfig 默认值
    """
    trend_ma_spread_pct: float | None = None
    crash_atr_pct_min: float | None = None


@dataclass(frozen=True)
class VerdictConfig:
    """Verdict sanity check 阈值 — Type B 事后口径

    来源: core/committee.py:206-220 THRESHOLDS dict
    """
    buy_confidence_overdrive: float = 0.95
    buy_confidence_downgrade_to: float = 0.6
    alloc_cny_ceiling: float = 100_000
    worker_unavailable_confidence_floor: float = 0.4


@dataclass(frozen=True)
class DreamingTunableConfig:
    """Dreaming 可调参数 — 簇 7-8

    来源: jobs/dreaming.py:64-66
    """
    min_recall: int = 3
    lookback_days: int = 90
    windows: tuple[int, ...] = (7, 30)


@dataclass(frozen=True)
class MacroBucketConfig:
    """VIX/TNX 分桶 — 簇 6

    来源: jobs/dreaming.py:227-238 _classify_regime()
    """
    vix_low: float = 18.0
    vix_high: float = 25.0
    tnx_low: float = 4.0
    tnx_high: float = 4.5


@dataclass(frozen=True)
class OracleAccuracyConfig:
    """Verdict Oracle Accuracy 阈值 — 簇 9

    来源: core/backtest_reward.py:150-178 verdict_oracle_accuracy()
    """
    buy_positive: float = 5.0
    buy_negative: float = -3.0
    accumulate_positive: float = 3.0
    accumulate_negative: float = -3.0
    hold_neutral: float = 3.0
    hold_wrong: float = 8.0
    trim_positive: float = -3.0
    trim_negative: float = 5.0
    sell_positive: float = -5.0
    sell_negative: float = 3.0


@dataclass(frozen=True)
class RewardConfig:
    """Reward 函数权重

    来源: core/backtest_reward.py:54-58 compute_strategy_reward()
           core/backtest_reward.py:91-92 forward_window_reward()
    """
    weight_annualized_return: float = 1.0
    weight_max_drawdown: float = -0.5
    weight_alpha_vs_yuebao: float = 0.5
    weight_sharpe_bonus: float = 0.2
    sharpe_bonus_threshold: float = 1.0
    lam_mdd: float = 1.0
    lam_return: float = 0.05


@dataclass(frozen=True)
class TunableConfig:
    """所有可调参数的顶层容器"""
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    regime_per_asset: dict[str, RegimePerAssetConfig] = field(default_factory=dict)
    verdict: VerdictConfig = field(default_factory=VerdictConfig)
    dreaming: DreamingTunableConfig = field(default_factory=DreamingTunableConfig)
    macro_buckets: MacroBucketConfig = field(default_factory=MacroBucketConfig)
    oracle_accuracy: OracleAccuracyConfig = field(default_factory=OracleAccuracyConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
