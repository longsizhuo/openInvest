"""portfolio_cmds —— 写操作类 skill 子命令（存取现金 / 买卖 / 删持仓）

逐字搬运自 scripts/skill.py：
- _resolve_pm：统一拿 PortfolioManager（避免每个 cmd 重复 import）。
- cmd_deposit / cmd_withdraw / cmd_buy / cmd_sell / cmd_delete_holding：
  薄封装 core.portfolio_manager.PortfolioManager 的对应写方法（与 /api/skill/* 共享）。

CLAUDE.md 产品哲学：Agent 必须拥有全部功能（不能只读不写）。
错误路径保持历史行为：部分 cmd 用 sys.exit(1)，部分 raise SystemExit(json.dumps(...)) 走 stderr。
"""
from __future__ import annotations

import json
import sys

from scripts.skill_cmds._helpers import _print_json

__all__ = [
    "_resolve_pm",
    "cmd_deposit",
    "cmd_withdraw",
    "cmd_buy",
    "cmd_sell",
    "cmd_delete_holding",
]


def _resolve_pm():
    """统一拿 PortfolioManager，避免每个 cmd 重复 import"""
    from core.portfolio_manager import PortfolioManager
    return PortfolioManager()


def cmd_deposit(args: argparse.Namespace) -> None:
    """存入现金（任意币种）。等价 /api/skill/deposit。

    示例:
        skill deposit --currency CNY --amount 5000
        skill deposit -c AUD -a 300

    写逻辑在 core/portfolio_manager.py:PortfolioManager.deposit_cash
    """
    pm = _resolve_pm()
    try:
        out = pm.deposit_cash(args.currency, args.amount, source="skill_cli")
    except ValueError as e:
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _print_json(out)


def cmd_withdraw(args: argparse.Namespace) -> None:
    """取出现金（任意币种）。余额不足返回 error。

    示例:
        skill withdraw -c CNY -a 1000

    写逻辑在 core/portfolio_manager.py:PortfolioManager.withdraw_cash
    """
    amount = float(args.amount)
    if amount <= 0:
        _print_json({"status": "error", "error": "amount 必须 > 0"})
        sys.exit(1)

    pm = _resolve_pm()
    try:
        out = pm.withdraw_cash(args.currency, amount, source="skill_cli")
    except ValueError as e:
        # 历史行为：余额不足时 JSON 走 stderr + exit 1（SystemExit(str)）
        raise SystemExit(json.dumps({
            "status": "error", "error": str(e),
        }, ensure_ascii=False))
    _print_json(out)


def cmd_buy(args: argparse.Namespace) -> None:
    """加仓（已有 symbol 增加 units + 加权平均成本；新 symbol 直接建仓）。

    示例:
        skill buy --symbol 510300.SS --units 1000 --price 4.2 --currency CNY --kind etf
        skill buy --symbol AAPL --units 10 --price 175.5 --currency USD --kind equity
        skill buy --symbol GC=F --units 5 --price 700 --currency CNY --kind metal --unit-label 克

    写逻辑在 core/portfolio_manager.py:PortfolioManager.buy（与 /api/skill/buy 共享）
    """
    pm = _resolve_pm()
    try:
        out = pm.buy(
            args.symbol, float(args.units), float(args.price),
            currency=args.currency, kind=args.kind, unit_label=args.unit_label,
            source="skill_cli",
        )
    except ValueError as e:
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _print_json(out)


def cmd_sell(args: argparse.Namespace) -> None:
    """减仓（units 减少，cost_avg 不变）。units 减完后保留为 0（用 skill delete_holding 真删）。

    示例:
        skill sell --symbol 510300.SS --units 500 --price 4.5

    写逻辑在 core/portfolio_manager.py:PortfolioManager.sell（与 /api/skill/sell 共享）
    """
    units = float(args.units)
    price = float(args.price)
    if units <= 0 or price <= 0:
        _print_json({"status": "error", "error": "units / price 必须 > 0"})
        sys.exit(1)

    pm = _resolve_pm()
    try:
        out = pm.sell(args.symbol, units, price, source="skill_cli")
    except ValueError as e:
        # 历史行为：持仓校验失败时 JSON 走 stderr + exit 1（SystemExit(str)）
        raise SystemExit(json.dumps({
            "status": "error", "error": str(e),
        }, ensure_ascii=False))
    _print_json(out)


def cmd_delete_holding(args: argparse.Namespace) -> None:
    """删除持仓行（units 必须为 0，否则拒绝，让你先 sell 或 set 到 0）。

    示例:
        skill delete_holding --symbol 510300.SS
        skill delete_holding --symbol 510300.SS --force   # 强删（units > 0 也删，慎用）

    写逻辑在 core/portfolio_manager.py:PortfolioManager.delete_holding
    """
    pm = _resolve_pm()
    try:
        out = pm.delete_holding(args.symbol, force=args.force, source="skill_cli")
    except ValueError as e:
        raise SystemExit(json.dumps({
            "status": "error", "error": str(e),
        }, ensure_ascii=False))
    _print_json(out)
