"""openInvest RL 训练参数空间 —— 单一可信源

什么是这个文件？
==================
fork 用户 / 研究者想自己跑训练时，**所有可调 hyperparameter 都在这里列出**。
`scripts/rl_train.py` 会读这个 config 决定 Optuna 搜什么。

为什么不放 .env.example？
==========================
1. .env 是"用户必须配的运行时设置"（API key / 路径 / 端口）—— 错填会让程序跑不起来
2. 训练参数是"研究者想调实验时的旋钮"—— 默认值就能跑 (代码默认 = v0 行为),
   只有真要做实验的人才动
3. 把训练参数塞 .env.example 会让 99% 不调实验的 fork 用户困惑（"这数字啥意思？"）

`confirmed_effective` 字段
============================
2026-05-11 跑 30 trial 后**实测**归纳：
- True  = 调这个参数 reward 真的会动
- False = 调了也没用（placebo），保留是因为下次换 prompt / 资产组合后可能就有用

不要根据 confirmed_effective=False 就删字段——这是当前 prompt 架构下的观察，
prompt 变了 placebo 字段可能就 unblock 了。

怎么用
========
1. 改这个文件覆盖默认 range
2. 跑 `python -m scripts.rl_train` 自动用新 range
3. 不需要 export 任何 env var

例：
    cfg = TrainConfig()
    cfg.alloc_aggressiveness.high = 0.40  # 放宽上限到 40%

"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from openinvest.core.config.tunable import RewardConfig


@dataclass(frozen=True)
class ParamRange:
    """单个 hyperparameter 的搜索范围 + 元数据"""
    low: float
    high: float
    type: str  # "float" / "int"
    description: str
    confirmed_effective: bool   # True=实测有效 / False=placebo（2026-05-11 30-trial 结论）
    consumed_by: str            # 这个参数在哪里被消费（代码定位用）

    def suggest(self, trial, name: str):
        """给 optuna.Trial.suggest_* 用"""
        if self.type == "int":
            return trial.suggest_int(name, int(self.low), int(self.high))
        return trial.suggest_float(name, self.low, self.high)


@dataclass
class TrainConfig:
    """完整训练参数空间"""

    # ============ 5 个 hyperparameter ============

    alloc_aggressiveness: ParamRange = field(default_factory=lambda: ParamRange(
        low=0.05, high=0.30, type="float",
        description="单笔 alloc 上限 = ¥100k baseline × 这个比例。"
                    "0.10 → 单笔 ≤¥10k（保守，多笔小试探）；"
                    "0.30 → 单笔 ≤¥30k（激进，少笔大仓位）",
        confirmed_effective=True,
        consumed_by="core/committee.py:parse_cio_memo (via INVEST_ALLOC_AGGRESSIVENESS)",
    ))

    # #113 尺度无关化：两个 regime 参数从绝对 % 换为比值单位，范围覆盖新默认（3.6 / 2.0）
    regime_uptrend: ParamRange = field(default_factory=lambda: ParamRange(
        low=2.0, high=6.0, type="float",
        description="REGIME=uptrend 触发阈值。MA spread ÷ 自身中位 ATR%（典型日波数）超此比值判 uptrend",
        confirmed_effective=False,  # 弱（旧 % 口径下 trial 3.47 vs 5.59 reward 几乎相同）
        consumed_by="core/regime.py:THRESHOLDS['trend_spread_atr_ratio'] (via set_config_override)",
    ))

    regime_atr: ParamRange = field(default_factory=lambda: ParamRange(
        low=1.3, high=3.0, type="float",
        description="REGIME=crash 波动腿。atr_spike_ratio（当日 ATR% ÷ 自身 1 年中位）超此倍数判崩盘",
        confirmed_effective=False,
        consumed_by="core/regime.py:THRESHOLDS['crash_atr_spike_ratio_min'] (via set_config_override)",
    ))

    max_rounds: ParamRange = field(default_factory=lambda: ParamRange(
        low=1, high=3, type="int",
        description="cross-challenge 轮数。1 = round 1 完直接 CIO；3 = 3 轮辩论",
        confirmed_effective=False,  # placebo（max_rounds=1 vs 3 reward 同）
        consumed_by="scripts/backtest_committee.py:run_committee (via INVEST_MAX_DEBATE_ROUNDS)",
    ))

    cio_confidence_cap: ParamRange = field(default_factory=lambda: ParamRange(
        low=0.7, high=0.95, type="float",
        description="CIO confidence 上限 clamp。0.7 = 把 LLM 给的 0.9 confidence 砍到 0.7",
        confirmed_effective=False,  # placebo（cio_cap 0.75 vs 0.94 reward 同）
        consumed_by="core/committee.py:parse_cio_memo (via INVEST_CIO_CONFIDENCE_CAP)",
    ))

    # ============ Reward function 权重 ============

    reward: RewardConfig = field(default_factory=RewardConfig)

    # ============ 训练 / 验证 / 测试集分割 ============

    train_start: str = "2024-05-13"   # yfinance 10y 数据起点附近
    train_end: str = "2024-11-15"     # 留 ~6w 给 hold-out
    holdout_start: str = "2024-11-18"
    holdout_end: str = "2024-12-31"
    step_days: int = 7                 # walk-forward 步长（每 7 天决策一次）
    assets: Tuple[str, ...] = ("NDQ.AX", "GC=F")
    initial_cash_cny: float = 100_000.0

    def hyperparams(self) -> List[Tuple[str, ParamRange]]:
        """枚举所有 hyperparameter (name, range) 给 Optuna 用"""
        return [
            ("alloc_aggressiveness", self.alloc_aggressiveness),
            ("regime_uptrend", self.regime_uptrend),
            ("regime_atr", self.regime_atr),
            ("max_rounds", self.max_rounds),
            ("cio_confidence_cap", self.cio_confidence_cap),
        ]

    def summary(self) -> str:
        """终端打印用 / 落 training_report 用"""
        lines = ["TrainConfig 参数空间："]
        for name, pr in self.hyperparams():
            mark = "✅" if pr.confirmed_effective else "⚠️"
            lines.append(f"  {mark} {name}: [{pr.low}, {pr.high}] ({pr.type})")
            lines.append(f"     {pr.description[:80]}")
        return "\n".join(lines)


DEFAULT = TrainConfig()


if __name__ == "__main__":
    print(DEFAULT.summary())
