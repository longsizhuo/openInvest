"""config_cmds —— 读/改可经 API 配置的白名单参数（ADR-017 config-via-API）。

与 `/api/config`（connectors/web_api/routers/config.py）共享同一份 core.config 底层
（effective_api_config / set_persisted_override / clear_persisted_override）——GUI 小白
点开关、agent 跑 CLI，殊途同归，落同一个 memory/.state/config_overrides.json。

CLAUDE.md 产品哲学：Agent 必须拥有全部功能。GUI 能在「委员会配置」里改的，agent 都能 CLI 改。
"""
from __future__ import annotations

import argparse
import sys

from openinvest.skill_cmds._helpers import _print_json

__all__ = ["cmd_config"]


def cmd_config(args: argparse.Namespace) -> None:
    """读/改白名单 tunable（concentration_lens / risk_profile / gold_defense_dca / dreaming.llm_verify）。

    示例:
        skill config                                                  # 看全部生效值
        skill config --set verdict.concentration_lens_enabled false   # 单资产关掉超配减仓
        skill config --set verdict.risk_profile aggressive
        skill config --clear verdict.concentration_lens_enabled       # 回退默认
    """
    from openinvest.core.config import (
        clear_persisted_override,
        effective_api_config,
        set_persisted_override,
    )

    try:
        if args.set:
            key, value = args.set
            set_persisted_override(key, value)  # _coerce_and_validate 处理 true/false + enum 校验
        elif args.clear:
            clear_persisted_override(args.clear)
    except ValueError as e:
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)

    _print_json({"status": "ok", "config": effective_api_config()})
