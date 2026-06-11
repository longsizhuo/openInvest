"""core.config — 参数管理模块

公共 API:
- load_config()      加载 tunable config（带缓存）
- reset_config()     清除缓存
- set_config_override()  注入 override
- get_locked()       获取 locked 参数（不经过注入链）
"""
from __future__ import annotations

from ._loader import load_config, reset_config, set_config_override
from .locked import (
    LockedCommitteeDefaults,
    LockedDreamingScoring,
    LockedPromptIdentity,
    LockedVerdictScoring,
    get_locked,
)
from .tunable import (
    DreamingTunableConfig,
    MacroBucketConfig,
    OracleAccuracyConfig,
    RegimeConfig,
    RegimePerAssetConfig,
    PathConfig,
    RewardConfig,
    SentimentConfig,
    TunableConfig,
    ValuationConfig,
    VerdictConfig,
)

__all__ = [
    # 公共 API
    "load_config",
    "reset_config",
    "set_config_override",
    "get_locked",
    # Tunable dataclasses
    "TunableConfig",
    "RegimeConfig",
    "RegimePerAssetConfig",
    "VerdictConfig",
    "DreamingTunableConfig",
    "MacroBucketConfig",
    "OracleAccuracyConfig",
    "RewardConfig",
    "SentimentConfig",
    "ValuationConfig",
    "PathConfig",
    # Locked dataclasses
    "LockedVerdictScoring",
    "LockedDreamingScoring",
    "LockedCommitteeDefaults",
    "LockedPromptIdentity",
]
