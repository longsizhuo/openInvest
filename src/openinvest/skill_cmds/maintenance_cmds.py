"""maintenance_cmds —— 运维清理子命令（评审路线 D7）。

`maintenance cleanup [--dry-run|--apply] [--keep N] [--days D]`：memory/ 里 `*.bak.<ts>` 保留策略 +
历史遗留 `*.lock` sidecar。默认 dry-run。逻辑在 services/maintenance.py（含写入点与策略说明）。
"""
from __future__ import annotations

import argparse

from openinvest.skill_cmds._helpers import _print_json

__all__ = ["cmd_maintenance"]


def cmd_maintenance(args: argparse.Namespace) -> None:
    from openinvest.core import memory_store  # 读模块属性：测试 patch MEMORY_ROOT 即生效；不实例化 MemoryStore（会 mkdir）
    from openinvest.services.maintenance import cleanup

    _print_json(cleanup(memory_store.MEMORY_ROOT, apply=args.apply, keep=args.keep, days=args.days))
