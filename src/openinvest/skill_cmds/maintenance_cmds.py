"""maintenance_cmds —— 运维清理子命令（评审路线 D7）。

`maintenance cleanup [--dry-run|--apply] [--keep N] [--days D]`：memory/ 里 `*.bak.<ts>` 保留策略 +
历史遗留 `*.lock` sidecar。默认 dry-run。逻辑在 services/maintenance.py（含写入点与策略说明）。

`backfill_raw_close [--symbols A,B]`：给行情库已有行回填原始价 close_raw（ADR-027 PR2，估值不再把分红算两次）。
逻辑与「哪些标的不回填、为什么」在 services/raw_close.py。
"""
from __future__ import annotations

import argparse

from openinvest.skill_cmds._helpers import _print_json

__all__ = ["cmd_maintenance", "cmd_backfill_raw_close"]


def cmd_maintenance(args: argparse.Namespace) -> None:
    from openinvest.core import memory_store  # 读模块属性：测试 patch MEMORY_ROOT 即生效；不实例化 MemoryStore（会 mkdir）
    from openinvest.services.maintenance import cleanup

    _print_json(cleanup(memory_store.MEMORY_ROOT, apply=args.apply, keep=args.keep, days=args.days))


def cmd_backfill_raw_close(args: argparse.Namespace) -> None:
    """回填 daily_prices.close_raw（联网拉 yfinance period=max，只 UPDATE 已有行，幂等）。

    示例:
        skill backfill_raw_close                       # 当前持仓 + target_assets
        skill backfill_raw_close --symbols NDQ.AX,SPY
    """
    from openinvest.services.raw_close import backfill_raw_close

    symbols = [s for s in args.symbols.split(",")] if args.symbols else None
    _print_json(backfill_raw_close(symbols))
