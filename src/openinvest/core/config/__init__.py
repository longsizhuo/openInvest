"""openinvest.core.config — 参数管理模块

公共 API:
- load_config()      加载 tunable config（带缓存）
- reset_config()     清除缓存
- set_config_override()  注入 override
- get_locked()       获取 locked 参数（不经过注入链）
"""
from __future__ import annotations

from ._loader import (
    API_SETTABLE,
    clear_persisted_override,
    effective_api_config,
    load_config,
    reset_config,
    set_config_override,
    set_persisted_override,
)
from .locked import (
    LockedCommitteeDefaults,
    LockedDreamingScoring,
    LockedPromptIdentity,
    LockedVerdictScoring,
    get_locked,
)
from .tunable import (
    DCAConfig,
    DreamingTunableConfig,
    EventConfig,
    MacroBucketConfig,
    OracleAccuracyConfig,
    RegimeConfig,
    PathConfig,
    RewardConfig,
    SentimentConfig,
    StalenessConfig,
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
    # 持久化 API 配置层（ADR-017）
    "API_SETTABLE",
    "effective_api_config",
    "set_persisted_override",
    "clear_persisted_override",
    # Tunable dataclasses
    "TunableConfig",
    "RegimeConfig",
    "VerdictConfig",
    "DreamingTunableConfig",
    "MacroBucketConfig",
    "OracleAccuracyConfig",
    "RewardConfig",
    "SentimentConfig",
    "ValuationConfig",
    "PathConfig",
    "DCAConfig",
    "EventConfig",
    "StalenessConfig",
    # Locked dataclasses
    "LockedVerdictScoring",
    "LockedDreamingScoring",
    "LockedCommitteeDefaults",
    "LockedPromptIdentity",
]
