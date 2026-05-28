"""core.config 模块测试

验证:
- 默认值 = 硬编码值（与 core/regime.py, jobs/dreaming.py 等一致）
- YAML 覆盖生效
- env 覆盖生效
- locked 参数无法被 override
- reset_config() 后恢复默认
- set_config_override() 后反映新值
- autouse fixture 隔离
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from core.config import (
    DreamingTunableConfig,
    LockedCommitteeDefaults,
    LockedDreamingScoring,
    LockedPromptIdentity,
    LockedVerdictScoring,
    MacroBucketConfig,
    OracleAccuracyConfig,
    RegimeConfig,
    RegimePerAssetConfig,
    RewardConfig,
    TunableConfig,
    VerdictConfig,
    get_locked,
    load_config,
    reset_config,
    set_config_override,
)


# ---------- autouse fixture ----------


@pytest.fixture(autouse=True)
def _reset_config():
    """每个 test 自动 reset config，防止 test 之间互相污染。"""
    reset_config()
    yield
    reset_config()


# ---------- 默认值 = 硬编码值 ----------


class TestDefaultsMatchHardcoded:
    """验证 config 默认值与源码硬编码完全一致。"""

    def test_regime_defaults(self):
        """RegimeConfig 默认值 = core/regime.py:35-55 THRESHOLDS"""
        cfg = RegimeConfig()
        assert cfg.trend_ma_spread_pct == 3.0
        assert cfg.crash_atr_pct_min == 5.0
        assert cfg.crash_drawdown_30d_pct == 20.0
        assert cfg.crash_deep_drawdown_30d_pct == 30.0
        assert cfg.recovery_rebound_pct == 10.0
        assert cfg.recovery_quantile_max == 0.50
        assert cfg.low_quantile_threshold == 0.20
        assert cfg.high_quantile_threshold == 0.80

    def test_verdict_defaults(self):
        """VerdictConfig 默认值 = core/committee.py:206-220 THRESHOLDS"""
        cfg = VerdictConfig()
        assert cfg.buy_confidence_overdrive == 0.95
        assert cfg.buy_confidence_downgrade_to == 0.6
        assert cfg.alloc_cny_ceiling == 100_000
        assert cfg.worker_unavailable_confidence_floor == 0.4
        assert cfg.forced_hold_confidence_ceiling == 0.4

    def test_dreaming_defaults(self):
        """DreamingTunableConfig 默认值 = jobs/dreaming.py:64-66"""
        cfg = DreamingTunableConfig()
        assert cfg.min_recall == 3
        assert cfg.lookback_days == 90
        assert cfg.windows == (7, 30)

    def test_macro_bucket_defaults(self):
        """MacroBucketConfig 默认值 = jobs/dreaming.py:227-238"""
        cfg = MacroBucketConfig()
        assert cfg.vix_low == 18.0
        assert cfg.vix_high == 25.0
        assert cfg.tnx_low == 4.0
        assert cfg.tnx_high == 4.5

    def test_oracle_accuracy_defaults(self):
        """OracleAccuracyConfig 默认值 = core/backtest_reward.py:150-178"""
        cfg = OracleAccuracyConfig()
        assert cfg.buy_positive == 5.0
        assert cfg.buy_negative == -3.0
        assert cfg.accumulate_positive == 3.0
        assert cfg.accumulate_negative == -3.0
        assert cfg.hold_neutral == 3.0
        assert cfg.hold_wrong == 8.0
        assert cfg.trim_positive == -3.0
        assert cfg.trim_negative == 5.0
        assert cfg.sell_positive == -5.0
        assert cfg.sell_negative == 3.0

    def test_reward_defaults(self):
        """RewardConfig 默认值 = core/backtest_reward.py:54-58,91-92"""
        cfg = RewardConfig()
        assert cfg.weight_annualized_return == 1.0
        assert cfg.weight_max_drawdown == -0.5
        assert cfg.weight_alpha_vs_yuebao == 0.5
        assert cfg.weight_sharpe_bonus == 0.2
        assert cfg.sharpe_bonus_threshold == 1.0
        assert cfg.lam_mdd == 1.0
        assert cfg.lam_return == 0.05


# ---------- load_config 默认值一致 ----------


class TestLoadConfigDefaults:
    """验证 load_config() 返回的默认值与 TunableConfig() 一致。"""

    def test_load_config_returns_defaults(self):
        cfg = load_config()
        assert isinstance(cfg, TunableConfig)
        assert cfg.regime.trend_ma_spread_pct == 3.0
        assert cfg.regime.crash_atr_pct_min == 5.0
        assert cfg.verdict.buy_confidence_overdrive == 0.95
        assert cfg.dreaming.lookback_days == 90
        assert cfg.macro_buckets.vix_low == 18.0
        assert cfg.oracle_accuracy.buy_positive == 5.0
        assert cfg.reward.weight_max_drawdown == -0.5

    def test_load_config_has_per_asset_defaults(self):
        """defaults.yaml 里的 per_asset 应该被加载。"""
        cfg = load_config()
        assert "GC=F" in cfg.regime_per_asset
        assert cfg.regime_per_asset["GC=F"].trend_ma_spread_pct == 5.0
        assert cfg.regime_per_asset["GC=F"].crash_atr_pct_min == 3.5
        assert "NDQ.AX" in cfg.regime_per_asset
        assert cfg.regime_per_asset["NDQ.AX"].trend_ma_spread_pct == 4.0
        assert cfg.regime_per_asset["NDQ.AX"].crash_atr_pct_min is None
        assert "BTC-USD" in cfg.regime_per_asset
        assert cfg.regime_per_asset["BTC-USD"].trend_ma_spread_pct == 8.0
        assert cfg.regime_per_asset["BTC-USD"].crash_atr_pct_min == 8.0
        assert "ETH-USD" in cfg.regime_per_asset
        assert cfg.regime_per_asset["ETH-USD"].trend_ma_spread_pct == 8.0
        assert cfg.regime_per_asset["ETH-USD"].crash_atr_pct_min == 8.0


# ---------- YAML 覆盖 ----------


class TestYamlOverride:
    """验证 YAML 覆盖生效。"""

    def test_yaml_override_single_field(self, tmp_path):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            regime:
              trend_ma_spread_pct: 4.5
        """))
        cfg = load_config(yaml_path=yaml_file)
        assert cfg.regime.trend_ma_spread_pct == 4.5
        # 其他字段保持默认
        assert cfg.regime.crash_atr_pct_min == 5.0

    def test_yaml_override_nested(self, tmp_path):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            oracle_accuracy:
              buy_positive: 7.0
              buy_negative: -5.0
        """))
        cfg = load_config(yaml_path=yaml_file)
        assert cfg.oracle_accuracy.buy_positive == 7.0
        assert cfg.oracle_accuracy.buy_negative == -5.0
        # 其他字段保持默认
        assert cfg.oracle_accuracy.hold_neutral == 3.0


# ---------- env 覆盖 ----------


class TestEnvOverride:
    """验证 INVEST_* 环境变量覆盖生效。"""

    def test_env_override_regime(self, monkeypatch):
        monkeypatch.setenv("INVEST_REGIME_TREND_MA_SPREAD_PCT", "4.5")
        cfg = load_config()
        assert cfg.regime.trend_ma_spread_pct == 4.5

    def test_env_override_dreaming(self, monkeypatch):
        monkeypatch.setenv("INVEST_DREAMING_LOOKBACK_DAYS", "180")
        cfg = load_config()
        assert cfg.dreaming.lookback_days == 180

    def test_env_override_verdict(self, monkeypatch):
        monkeypatch.setenv("INVEST_VERDICT_BUY_CONFIDENCE_OVERDRIVE", "0.90")
        cfg = load_config()
        assert cfg.verdict.buy_confidence_overdrive == 0.90


# ---------- CLI override ----------


class TestCliOverride:
    """验证 cli_overrides dict 覆盖生效。"""

    def test_cli_override_regime(self):
        cfg = set_config_override({"regime": {"trend_ma_spread_pct": 5.5}})
        assert cfg.regime.trend_ma_spread_pct == 5.5
        # 其他字段保持默认
        assert cfg.regime.crash_atr_pct_min == 5.0

    def test_cli_override_reward(self):
        cfg = set_config_override({"reward": {"weight_max_drawdown": -1.0}})
        assert cfg.reward.weight_max_drawdown == -1.0
        assert cfg.reward.weight_alpha_vs_yuebao == 0.5


# ---------- 注入优先级链 ----------


class TestInjectionPriority:
    """验证优先级: default → YAML → CLI → env"""

    def test_cli_overrides_yaml(self, tmp_path):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text("regime:\n  trend_ma_spread_pct: 4.0\n")
        cfg = load_config(yaml_path=yaml_file, cli_overrides={"regime": {"trend_ma_spread_pct": 6.0}})
        assert cfg.regime.trend_ma_spread_pct == 6.0

    def test_env_overrides_cli(self, monkeypatch):
        monkeypatch.setenv("INVEST_REGIME_TREND_MA_SPREAD_PCT", "7.0")
        cfg = set_config_override({"regime": {"trend_ma_spread_pct": 6.0}})
        assert cfg.regime.trend_ma_spread_pct == 7.0


# ---------- Locked 参数不受注入链影响 ----------


class TestLockedIsolation:
    """验证 locked 参数无法被 YAML/CLI/env 覆盖。"""

    def test_get_locked_returns_hardcoded(self):
        scoring, dreaming, committee, prompt = get_locked()
        assert isinstance(scoring, LockedVerdictScoring)
        assert scoring.k_flat == 1.0
        assert scoring.flat_ceiling_pct == 8.0
        assert scoring.default_daily_vol_pct == 2.0

        assert isinstance(dreaming, LockedDreamingScoring)
        assert dreaming.min_score == 0.8
        assert dreaming.score_hit_rate_weight == 0.7
        assert dreaming.score_sample_weight == 0.3
        assert dreaming.caution_min_base_down == 0.15
        assert dreaming.caution_lift_full == 0.20

        assert isinstance(committee, LockedCommitteeDefaults)
        assert committee.llm_max_attempts == 3
        assert committee.llm_base_delay == 2.0
        assert committee.llm_max_delay == 20.0
        assert committee.temperature == 0.2
        assert committee.max_tool_iterations == 4
        assert committee.max_debate_rounds_default == 1
        assert committee.max_debate_rounds_live == 4

        assert isinstance(prompt, LockedPromptIdentity)
        assert prompt.cio_zero_shot is True
        assert prompt.cash_opportunity_cost_rule is True

    def test_locked_unaffected_by_set_config_override(self):
        """set_config_override 不能改变 locked 参数。"""
        # 先确认 locked 的初始值
        scoring, _, _, _ = get_locked()
        original_k_flat = scoring.k_flat
        assert original_k_flat == 1.0

        # 尝试用 override 覆盖（这应该不会影响 locked）
        set_config_override({"locked": {"k_flat": 999.0}})

        # locked 值不变
        scoring_after, _, _, _ = get_locked()
        assert scoring_after.k_flat == 1.0

    def test_locked_unaffected_by_env(self, monkeypatch):
        """环境变量不能改变 locked 参数。"""
        monkeypatch.setenv("INVEST_LOCKED_K_FLAT", "999.0")
        scoring, _, _, _ = get_locked()
        assert scoring.k_flat == 1.0

    def test_locked_is_frozen(self):
        """locked dataclass 是 frozen，不能修改字段。"""
        scoring, _, _, _ = get_locked()
        with pytest.raises(AttributeError):
            scoring.k_flat = 999.0  # type: ignore[misc]


# ---------- frozen dataclass ----------


class TestFrozenDataclass:
    """验证所有 config dataclass 是 frozen。"""

    def test_regime_frozen(self):
        cfg = RegimeConfig()
        with pytest.raises(AttributeError):
            cfg.trend_ma_spread_pct = 99.0  # type: ignore[misc]

    def test_tunable_frozen(self):
        cfg = TunableConfig()
        with pytest.raises(AttributeError):
            cfg.regime = RegimeConfig(trend_ma_spread_pct=99.0)  # type: ignore[misc]


# ---------- reset_config ----------


class TestResetConfig:
    """验证 reset_config() 清除缓存。"""

    def test_reset_restores_defaults(self):
        # 修改 config
        cfg1 = set_config_override({"regime": {"trend_ma_spread_pct": 9.0}})
        assert cfg1.regime.trend_ma_spread_pct == 9.0

        # reset
        reset_config()

        # 重新加载应该回到默认值
        cfg2 = load_config()
        assert cfg2.regime.trend_ma_spread_pct == 3.0

    def test_reset_clears_cache(self):
        """reset 后 load_config 应该重新构建（不是返回旧缓存）。"""
        cfg1 = load_config()
        reset_config()
        cfg2 = load_config()
        # 值相同但确实是重新构建的（通过检查引用不同来验证）
        assert cfg1 is not cfg2


# ---------- 缓存行为 ----------


class TestCaching:
    """验证 load_config() 的缓存语义。"""

    def test_same_args_returns_cached(self):
        cfg1 = load_config()
        cfg2 = load_config()
        assert cfg1 is cfg2

    def test_different_yaml_path_rebuilds(self, tmp_path):
        yaml_a = tmp_path / "a.yaml"
        yaml_a.write_text("regime:\n  trend_ma_spread_pct: 4.0\n")
        yaml_b = tmp_path / "b.yaml"
        yaml_b.write_text("regime:\n  trend_ma_spread_pct: 5.0\n")

        cfg_a = load_config(yaml_path=yaml_a)
        cfg_b = load_config(yaml_path=yaml_b)
        assert cfg_a is not cfg_b
        assert cfg_a.regime.trend_ma_spread_pct == 4.0
        assert cfg_b.regime.trend_ma_spread_pct == 5.0

    def test_force_reload(self):
        cfg1 = load_config()
        cfg2 = load_config(_force_reload=True)
        assert cfg1 is not cfg2
        assert cfg1.regime.trend_ma_spread_pct == cfg2.regime.trend_ma_spread_pct


# ---------- autouse fixture 隔离 ----------


class TestFixtureIsolation:
    """验证 autouse fixture 在 test 之间隔离 config 状态。"""

    def test_first_test_modifies_config(self):
        """这个 test 修改 config — 下一个 test 应该看不到。"""
        cfg = set_config_override({"regime": {"trend_ma_spread_pct": 99.0}})
        assert cfg.regime.trend_ma_spread_pct == 99.0

    def test_second_test_sees_defaults(self):
        """这个 test 应该看到默认值，不受上一个 test 影响。"""
        cfg = load_config()
        assert cfg.regime.trend_ma_spread_pct == 3.0
