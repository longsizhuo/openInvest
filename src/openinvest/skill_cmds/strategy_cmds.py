"""strategy 写操作 CLI 子命令（issue #179：agent 必须拥有全部功能，读写对等）。

与 MCP 工具同名同语义，实现共用 services/strategy_write.py：
- set_allocations   改股票/现金目标配比（和必须 ≈1）
- track_asset       跟踪标的（upsert：不存在则新建，已存在只更新传入字段——
                    "track AAPL" 重复调用幂等不报错）
- untrack_asset     移除跟踪标的（schema 保证至少剩 1 个）

错误处理与 portfolio_cmds 同款：业务 ValueError → {"status":"error"} + exit 1。
"""
from __future__ import annotations

import argparse
import sys

from openinvest.skill_cmds._helpers import _print_json

__all__ = [
    "cmd_set_allocations",
    "cmd_track_asset",
    "cmd_untrack_asset",
]


def _run(fn, *args, **kwargs) -> None:
    try:
        out = fn(*args, **kwargs)
    except ValueError as e:  # 含 StrategyConflict / StrategyNotFound / schema 错
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _print_json(out)


def cmd_set_allocations(args: argparse.Namespace) -> None:
    """改资产配置目标。示例:
        skill set_allocations --stock 0.8 --cash 0.2
    """
    from openinvest.services import strategy_write as svc

    _run(svc.set_allocations, args.stock, args.cash)


def cmd_track_asset(args: argparse.Namespace) -> None:
    """跟踪标的（新建或更新，幂等）。示例:
        skill track_asset --symbol AAPL --max-single-invest-cny 8000
        skill track_asset --symbol GC=F --sell-fee-pct 0.0038   # 只改一个字段
    新建时 schema 要求 max_single_invest_cny 必填。
    """
    from openinvest.services import strategy_write as svc

    fields = {
        "display_name": args.display_name,
        "channel": args.channel,
        "max_single_invest_cny": args.max_single_invest_cny,
        "price_offset_pct": args.price_offset_pct,
        "sell_fee_pct": args.sell_fee_pct,
    }
    _run(svc.upsert_target_asset, args.symbol, fields)


def cmd_untrack_asset(args: argparse.Namespace) -> None:
    """移除跟踪标的。示例:
        skill untrack_asset --symbol AAPL
    """
    from openinvest.services import strategy_write as svc

    _run(svc.remove_target_asset, args.symbol)
