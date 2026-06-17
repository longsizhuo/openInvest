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
    # force-HOLD 时统一的 confidence 上限（Sanity 4：兜底充足覆盖集中度减仓用）。
    # 与 worker_unavailable_confidence_floor 同默认但语义独立，便于分别 tune。
    forced_hold_confidence_ceiling: float = 0.4
    # 集中度 lens 开关（2026-06 用户裁决）：单资产 / 刻意集中策略可关掉"超配减仓"视角。
    # True（默认）= 现有行为不变；False = 无条件 force-HOLD 掉 TRIM_REASON=concentration
    # （不再依赖 solvency_strong），并在 Risk/CIO prompt 压掉超配规则；仍保留波动/回撤/止损/
    # 估值/压力测试风险。env: INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=false
    concentration_lens_enabled: bool = True
    # 零花钱账户 + 强破产兜底时的 TRIM 约束阈值
    # 默认 0 = 禁用（等 sweep ADR-011 出 OOS 验证后再设真实值，遵守 ADR-010 rule 4）
    trim_no_trim_loss_pct: float = 0.0   # 浮亏 < 此值禁止 TRIM；0 = 不启用
    trim_caution_loss_pct: float = 0.0   # 浮亏 < 此值倾向 HOLD；0 = 不启用
    # 风险档（2026-06 regime 方向锁消融结论的显式化）：
    # - steady（默认）= 无方向锁（B 链）：Sharpe 最优、回撤最浅、熊市稳
    # - aggressive = uptrend 顺势加仓杠杆：HOLD→ACCUMULATE（消融证明 = 纯杠杆，
    #   牛市 +CR 但 −Sharpe + 深 MaxDD + 熊市更亏；是风险偏好，不是 alpha）
    # env: INVEST_VERDICT_RISK_PROFILE=aggressive
    risk_profile: str = "steady"
    # 黄金 VIX/ATR 防御腿"分批 DCA"（2026-06-13 用户裁决，wiki 18 §5）：高 VIX 区不再
    # 全拦黄金买入，改"放行但强制分批"——尊重中位右偏(典型涨,不该禁)，用时间分散吃
    # 厚左尾(挤兑坑真实且 fit 期更深)。两条腿(VIX OR ATR)合成单一分批计划。
    # 参数=机制选定的圆整值（左尾样本 ~6-20 太脆，非数据优化），靠反事实账本将来调。
    # dataclass 默认关=退回全拦旧行为（安全）；defaults.yaml 开=用户裁决的生产值。
    gold_defense_dca_enabled: bool = False
    gold_defense_dca_n_tranches: int = 3          # 最多分几批
    gold_defense_dca_fraction: float = 0.3333     # 每批=意图金额×此比例（≈1/3）
    gold_defense_dca_min_spacing_days: int = 5    # 两批至少隔几个交易日
    gold_defense_dca_window_days: int = 20        # N 批配额的滚动计数窗（交易日，≈3-4周）


@dataclass(frozen=True)
class DreamingTunableConfig:
    """Dreaming 可调参数 — 簇 7-8

    来源: jobs/dreaming.py:64-66
    """
    min_recall: int = 3
    lookback_days: int = 90
    windows: tuple[int, ...] = (7, 30)
    # Deep Sleep 写 insights 前过一次廉价 LLM 验伪（挑 spurious correlation）。默认关=零 LLM 成本。
    # 原 INVEST_DREAMING_LLM_VERIFY 裸 os.getenv 已收进 config（_loader 的 _LEGACY_MAP 向后兼容）。
    llm_verify_enabled: bool = False


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
class SentimentConfig:
    """情绪表盘阈值（VIX 近 2 年分位的恐慌/贪婪分档 + 快崩哨兵线）

    来源: utils/sentiment.py（2026-06-09 加维度时的模块常量，迁入 config 统一维护）
    分位口径: 高 VIX = 恐慌。fear/greed 两侧分档 + defense 哨兵线独立可调。
    """
    vix_extreme_fear_q: float = 0.85
    vix_fear_q: float = 0.65
    vix_greed_q: float = 0.35
    vix_extreme_greed_q: float = 0.15
    # VIX 分位 ≥ 此值 → INDEP_DEFENSE_FLAG=on（独立于 MA regime 的快崩哨兵，
    # 触发 parse_cio_memo 确定性买侧降级）。注意低波动期的"高分位"也会触发
    # （如 VIX=21 站上 2y 87% 分位）——嫌敏感就抬这条线。
    vix_defense_quantile: float = 0.85
    # 独立快崩防御 ATR 腿（资产级，通用口径）：当日 ATR% / 自身近 1 年滚动中位
    # ATR% ≥ 此比值 → 触发。尺度无关（任何资产自校准，无 per-asset magic number），
    # 对历史尖峰稳健（分位制会被窗内旧尖峰脱敏，中位基线不受尾部影响）。
    # 2.0 = 波动较自身常态翻倍。确定性 sweep 依据见 scripts/tune_defense_thresholds.py：
    # OR 组合 capture NDQ 3/3、GC 13/16，ATR 腿独立 on_rate 3.6%/3.7%。
    atr_defense_spike_ratio: float = 2.0
    # EVENT_STANCE 聚合（utils/sentiment.py）：severity 权重 + 时效半衰期。
    # **默认值 = 逐位等价旧纯计数**（等权 + half_life=0 禁用衰减 + band=0）。
    # 2026-06-11 经 scripts/eval_event_stance.py 验证判 INSUFFICIENT_DATA
    # （事件史仅 ~25d、信号日 7-17）——未经该脚本重跑给出达标结论前**不许改默认值**
    # （ADR-010 rule 4 同款纪律，先例 trim_no_trim_loss_pct）。
    event_stance_w_low: float = 1.0
    event_stance_w_mid: float = 1.0
    event_stance_w_high: float = 1.0
    event_stance_half_life_hours: float = 0.0   # 0 = 禁用时效衰减
    event_stance_neutral_band: float = 0.0      # |score| ≤ band → neutral


@dataclass(frozen=True)
class PathConfig:
    """路径分布校准层参数（core/regime_probability.calibrate_profile）

    2026-06 walk-forward 时代分桶（n=1579，2007→2026）诊断：带覆盖 7 个时代
    6 个 <80% 目标、小样本 regime 桶（downtrend）最差。两参数经 fit(≤2017)/
    OOS(2018+) 验证后才设非禁用默认值（ADR-010 rule 4，先例 trim_*_pct）。

    **dataclass 默认=禁用(0/1) 是有意的安全模式，不是与 YAML 漂移**：经验证的
    生产值(80/1.1)在 defaults.yaml；dataclass 走"配置缺失→graceful 退化到不校准"
    而非应用未验证参数。改默认值前必须先过 scripts/fit_path_calibration.py。
    """
    shrinkage_k: float = 0.0    # 条件→无条件收缩强度；0 = 禁用（生产值见 defaults.yaml）
    band_gamma: float = 1.0     # P10/P90/downside 带宽扩张系数；1 = 禁用（同上）


@dataclass(frozen=True)
class ValuationConfig:
    """估值 brief 阈值（权益类 trailing PE 绝对水平分档）

    来源: utils/valuation.py（2026-06-09 加维度时的模块常量，迁入 config 统一维护）
    """
    pe_expensive: float = 30.0   # trailing PE ≥ 此值判"偏贵"（纳指类成长股经验阈值）
    pe_cheap: float = 18.0       # trailing PE < 此值判"中性偏低"


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
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    valuation: ValuationConfig = field(default_factory=ValuationConfig)
    path: PathConfig = field(default_factory=PathConfig)
    oracle_accuracy: OracleAccuracyConfig = field(default_factory=OracleAccuracyConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
