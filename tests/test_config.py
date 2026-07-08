"""openinvest.core.config 模块测试

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

from openinvest.core.config import (
    DCAConfig,
    DreamingTunableConfig,
    LanguageConfig,
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
    API_SETTABLE,
    effective_api_config,
    set_persisted_override,
    clear_persisted_override,
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

    def test_language_defaults(self):
        cfg = LanguageConfig()
        assert cfg.invest_lang == "zh"


# ---------- load_config 默认值一致 ----------


class TestLoadConfigDefaults:
    """验证 load_config() 返回的默认值与 TunableConfig() 一致。"""

    def test_load_config_returns_defaults(self):
        cfg = load_config()
        assert isinstance(cfg, TunableConfig)
        assert cfg.language.invest_lang == "zh"
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

    def test_env_override_invest_lang(self, monkeypatch):
        monkeypatch.setenv("INVEST_LANG", "en")
        cfg = load_config()
        assert cfg.language.invest_lang == "en"


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


# ---------- config-via-API 持久层（ADR-017）----------


class TestApiConfig:
    """白名单校验 + 优先级（持久 API override > env）+ 落盘往返。落盘隔离到 tmp。"""

    @pytest.fixture(autouse=True)
    def _tmp_memory(self, tmp_path, monkeypatch):
        from openinvest.core import memory_store as ms
        monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
        yield

    def test_effective_view_defaults(self):
        view = {it["key"]: it for it in effective_api_config()}
        assert set(view) == set(API_SETTABLE)
        assert view["language.invest_lang"]["value"] == "zh"
        assert view["language.invest_lang"]["choices"] == ["zh", "en"]
        assert view["verdict.concentration_lens_enabled"]["value"] is False  # ADR-020: default OFF
        assert view["verdict.concentration_lens_enabled"]["overridden"] is False
        assert view["verdict.risk_profile"]["choices"] == ["steady", "aggressive"]

    def test_set_persists_and_survives_reload(self):
        """set → 落盘 → reset 后重 load 仍生效（模拟另一进程读同一文件）。"""
        # 必须用非默认值 True：ADR-020 后默认是 False，若这里仍 set False，
        # 「reload 后仍是 False」无论持久化是否生效都成立 → 断言空转。
        cfg = set_persisted_override("verdict.concentration_lens_enabled", True)
        assert cfg.verdict.concentration_lens_enabled is True
        reset_config()
        assert load_config().verdict.concentration_lens_enabled is True
        ov = [it["overridden"] for it in effective_api_config()
              if it["key"] == "verdict.concentration_lens_enabled"][0]
        assert ov is True

    def test_str_bool_coercion(self):
        cfg = set_persisted_override("verdict.concentration_lens_enabled", "false")
        assert cfg.verdict.concentration_lens_enabled is False

    def test_enum_validation(self):
        assert set_persisted_override("verdict.risk_profile", "aggressive").verdict.risk_profile == "aggressive"
        with pytest.raises(ValueError):
            set_persisted_override("verdict.risk_profile", "yolo")

    def test_non_whitelist_rejected(self):
        with pytest.raises(ValueError):
            set_persisted_override("verdict.alloc_cny_ceiling", 1)

    def test_language_enum_validation(self):
        assert set_persisted_override("language.invest_lang", "en").language.invest_lang == "en"
        with pytest.raises(ValueError):
            set_persisted_override("language.invest_lang", "fr")

    def test_bad_bool_rejected(self):
        with pytest.raises(ValueError):
            set_persisted_override("verdict.concentration_lens_enabled", "maybe")

    def test_clear_reverts_to_default(self):
        set_persisted_override("verdict.concentration_lens_enabled", True)
        cfg = clear_persisted_override("verdict.concentration_lens_enabled")
        assert cfg.verdict.concentration_lens_enabled is False  # ADR-020: default OFF
        with pytest.raises(ValueError):
            clear_persisted_override("verdict.alloc_cny_ceiling")  # 非白名单

    def test_api_override_beats_env(self, monkeypatch):
        """ADR-017 核心：持久 API override 优先级高于 env。"""
        monkeypatch.setenv("INVEST_VERDICT_CONCENTRATION_LENS_ENABLED", "true")
        set_persisted_override("verdict.concentration_lens_enabled", False)
        reset_config()
        assert load_config().verdict.concentration_lens_enabled is False  # API 赢 env

    def test_env_applies_when_no_override(self, monkeypatch):
        """无持久 override 时 env 仍是 bootstrap 默认（向后兼容）。"""
        monkeypatch.setenv("INVEST_VERDICT_RISK_PROFILE", "aggressive")
        reset_config()
        assert load_config().verdict.risk_profile == "aggressive"

    def test_dreaming_llm_verify_legacy_env(self, monkeypatch):
        """#3 向后兼容：旧 INVEST_DREAMING_LLM_VERIFY=1 经 _LEGACY_MAP 进 config。"""
        monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
        reset_config()
        assert load_config().dreaming.llm_verify_enabled in (True, 1)

    # ---------- event.watch_schedule（cron 类型，2026-07-03 扫描窗口修正）----------

    def test_watch_schedule_default(self):
        """默认窗口=北京 8:00-次日 2:30（修正旧值实跑北京 0-7:30 错开美盘的 bug）。"""
        assert load_config().event.watch_schedule == "*/30 0-2,8-23 * * *"

    def test_watch_schedule_roundtrip(self):
        cfg = set_persisted_override("event.watch_schedule", "*/15 8-23 * * 1-5")
        assert cfg.event.watch_schedule == "*/15 8-23 * * 1-5"
        reset_config()
        assert load_config().event.watch_schedule == "*/15 8-23 * * 1-5"

    def test_watch_schedule_rejects_bad_cron(self):
        for bad in ("not a cron", "", "99 99 * * *", 123):
            with pytest.raises(ValueError):
                set_persisted_override("event.watch_schedule", bad)

    def test_watch_schedule_env_comma_split_repaired(self, monkeypatch):
        """env 层通用强转把含逗号值拆成 list——构造层必须拼回字符串。"""
        monkeypatch.setenv("INVEST_EVENT_WATCH_SCHEDULE", "*/30 0-2,8-23 * * *")
        reset_config()
        assert load_config().event.watch_schedule == "*/30 0-2,8-23 * * *"


# ---------- 自动定投配置（DCAConfig，子弹池模型）----------


class TestDCAConfig:
    """DCAConfig：默认禁用（安全）+ env / 持久 API 可配。

    落盘隔离到 tmp（同 TestApiConfig），避免污染真实 memory/.state/。
    """

    @pytest.fixture(autouse=True)
    def _tmp_memory(self, tmp_path, monkeypatch):
        from openinvest.core import memory_store as ms
        monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
        yield

    def test_dca_defaults(self):
        """默认：禁用、¥100/次、无 symbols（dataclass 默认=安全模式）"""
        cfg = DCAConfig()
        assert cfg.auto_dca_enabled is False
        assert cfg.auto_dca_amount_cny == 100.0
        assert cfg.auto_dca_symbols == ()

    def test_load_config_has_dca_defaults(self):
        cfg = load_config()
        assert cfg.dca.auto_dca_enabled is False
        assert cfg.dca.auto_dca_amount_cny == 100.0
        assert cfg.dca.auto_dca_symbols == ()

    def test_dca_frozen(self):
        cfg = DCAConfig()
        with pytest.raises(AttributeError):
            cfg.auto_dca_enabled = True  # type: ignore[misc]

    def test_env_override_enabled(self, monkeypatch):
        monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
        reset_config()
        assert load_config().dca.auto_dca_enabled is True

    def test_env_override_amount(self, monkeypatch):
        monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "150")
        reset_config()
        assert load_config().dca.auto_dca_amount_cny == 150.0

    def test_env_override_symbols_single(self, monkeypatch):
        """单 symbol（env 值无逗号 → 是裸 str）也要归一成单元素 tuple"""
        monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
        reset_config()
        assert load_config().dca.auto_dca_symbols == ("510300.SS",)

    def test_env_override_symbols_multi(self, monkeypatch):
        """逗号分隔多 symbol → tuple（去空格）"""
        monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS, GC=F")
        reset_config()
        assert load_config().dca.auto_dca_symbols == ("510300.SS", "GC=F")

    def test_dca_in_api_whitelist(self):
        """enabled(bool) + amount(float) 进 API 白名单（GUI/agent 经 /api/config 改）"""
        assert API_SETTABLE["dca.auto_dca_enabled"]["type"] == "bool"
        assert API_SETTABLE["dca.auto_dca_amount_cny"]["type"] == "float"

    def test_set_persisted_enabled(self):
        assert set_persisted_override("dca.auto_dca_enabled", True).dca.auto_dca_enabled is True

    def test_set_persisted_amount_float_coercion(self):
        """float 白名单项：str '200' → 200.0"""
        assert set_persisted_override("dca.auto_dca_amount_cny", "200").dca.auto_dca_amount_cny == 200.0

    def test_amount_bad_value_rejected(self):
        with pytest.raises(ValueError):
            set_persisted_override("dca.auto_dca_amount_cny", "abc")

    def test_effective_view_includes_dca(self):
        view = {it["key"]: it for it in effective_api_config()}
        assert "dca.auto_dca_enabled" in view
        assert view["dca.auto_dca_enabled"]["value"] is False


# ---------- 事件与陈旧度配置（EventConfig & StalenessConfig）----------


class TestEventAndStalenessConfig:
    """验证 EventConfig 和 StalenessConfig 的默认值、注入覆盖与 API 白名单"""

    @pytest.fixture(autouse=True)
    def _tmp_memory(self, tmp_path, monkeypatch):
        from openinvest.core import memory_store as ms
        monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
        yield

    def test_event_and_staleness_defaults(self):
        cfg = load_config()
        # EventConfig 默认值
        assert cfg.event.enabled is True
        assert cfg.event.rag_window_days == 7
        assert cfg.event.rag_min_severity == "mid"
        assert cfg.event.rag_top_k == 8
        assert cfg.event.max_per_source == 15
        assert cfg.event.min_severity == "mid"
        assert cfg.event.max_rounds == 2
        # StalenessConfig 默认值
        assert cfg.staleness.price_stale_days == 3
        assert cfg.staleness.hard_abort_stale_days == 7

    def test_env_override_event_and_staleness(self, monkeypatch):
        monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "false")
        monkeypatch.setenv("INVEST_EVENT_RAG_WINDOW_DAYS", "14")
        monkeypatch.setenv("INVEST_EVENT_RAG_MIN_SEVERITY", "high")
        monkeypatch.setenv("INVEST_PRICE_STALE_DAYS", "5")
        monkeypatch.setenv("INVEST_HARD_ABORT_STALE_DAYS", "10")
        reset_config()
        cfg = load_config()
        assert cfg.event.enabled is False
        assert cfg.event.rag_window_days == 14
        assert cfg.event.rag_min_severity == "high"
        assert cfg.staleness.price_stale_days == 5
        assert cfg.staleness.hard_abort_stale_days == 10

    def test_api_whitelist_registration(self):
        assert "event.enabled" in API_SETTABLE
        assert API_SETTABLE["event.enabled"]["type"] == "bool"
        assert "event.rag_window_days" in API_SETTABLE
        assert API_SETTABLE["event.rag_window_days"]["type"] == "int"
        assert "event.rag_min_severity" in API_SETTABLE
        assert API_SETTABLE["event.rag_min_severity"]["type"] == "enum"
        assert "staleness.price_stale_days" in API_SETTABLE
        assert API_SETTABLE["staleness.price_stale_days"]["type"] == "int"

    def test_set_persisted_event_and_staleness(self):
        # bool coercion
        cfg = set_persisted_override("event.enabled", "false")
        assert cfg.event.enabled is False

        # int coercion
        cfg = set_persisted_override("event.rag_top_k", "12")
        assert cfg.event.rag_top_k == 12

        # enum validation
        cfg = set_persisted_override("event.rag_min_severity", "high")
        assert cfg.event.rag_min_severity == "high"
        with pytest.raises(ValueError):
            set_persisted_override("event.rag_min_severity", "invalid_sev")

        # staleness int coercion
        cfg = set_persisted_override("staleness.price_stale_days", 4)
        assert cfg.staleness.price_stale_days == 4

