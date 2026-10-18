"""_helpers —— skill 子命令共享的小工具

逐字搬运自 scripts/skill.py：
- _safe_close：拉单 symbol 最新收盘价——重导出 services/skill_views.py 的唯一实现
  （#236：原先逐字复制两份，一份改了另一份还是 0.0 哨兵）。拉不到返回 None。
- _print_json：直写真实 stdout 输出 JSON（绕过 utils/* 的 print noise），全部
  子命令都依赖。
"""
from __future__ import annotations

import json
import sys
from typing import Any

from openinvest.services.skill_views import _safe_close  # noqa: F401  单一实现，见模块 docstring

__all__ = ["_safe_close", "_print_json"]


def _print_json(obj: Any) -> None:
    """直接写到原始 stdout，避免被 utils/* 的 print noise 污染"""
    real_stdout = getattr(sys, "__stdout__", sys.stdout)
    real_stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    real_stdout.write("\n")
    real_stdout.flush()
