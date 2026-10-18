"""strategy 写操作 CLI 子命令（issue #179：agent 必须拥有全部功能，读写对等）。

与 MCP 工具同名同语义，实现共用 services/strategy_write.py：
- set_allocations   改股票/现金目标配比（和必须 ≈1）和/或按标的的 target_pct / alerts（报警线）
- track_asset       跟踪标的（upsert：不存在则新建，已存在只更新传入字段——
                    "track AAPL" 重复调用幂等不报错）
- untrack_asset     移除跟踪标的（schema 保证至少剩 1 个）

另有只读的 rebalance_plan（目标配置 → 该买 / 卖多少的清单，只出清单不下单），
实现在 services/rebalance_plan.py，归在这里因为它消费的就是 strategy.md。

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
    "cmd_rebalance_plan",
]


def _run(fn, *args, **kwargs) -> None:
    try:
        out = fn(*args, **kwargs)
    except ValueError as e:  # 含 StrategyConflict / StrategyNotFound / schema 错
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _print_json(out)


def _parse_target_pcts(items) -> dict:
    """['510300.SS=0.3', 'GC=F=none'] → {'510300.SS': 0.3, 'GC=F': None}（none/null/空 = 清掉该标的的 target_pct）。
    rpartition('=')：symbol 本身可含 '='（GC=F）。"""
    out = {}
    for item in items or []:
        sym, eq, val = item.rpartition("=")
        if not eq or not sym.strip():
            raise ValueError(f"--target-pct 要写成 SYMBOL=0~1 的小数（如 510300.SS=0.3），收到 {item!r}")
        if val.strip().lower() in ("", "none", "null"):
            out[sym.strip()] = None
            continue
        try:
            out[sym.strip()] = float(val)
        except ValueError:
            raise ValueError(f"--target-pct {item!r} 的权重不是数字（0~1 的小数，如 0.3）") from None
    return out


def _parse_alerts(items) -> dict:
    """['GC=F:price_below=900', 'GC=F:day_drop_pct=none', '0700.HK=none']
    → {'GC=F': {'price_below': 900.0, 'day_drop_pct': None}, '0700.HK': None}
    （RULE=none 清这一条；不带 :RULE 的 SYMBOL=none 清空该标的全部）。
    rpartition：symbol 可含 '=' / ':'（GC=F）；":" 后面不是已知规则名就整段当 symbol。"""
    from openinvest.calc.price_alerts import RULES

    out: dict = {}
    for item in items or []:
        lhs, eq, val = item.rpartition("=")
        head, _, tail = lhs.rpartition(":")
        sym, rule = (head, tail) if tail in RULES else (lhs, None)
        sym, val = sym.strip(), val.strip().lower()
        if not eq or not sym:
            raise ValueError(f"--alert 要写成 SYMBOL:RULE=VALUE（如 GC=F:price_below=900；RULE ∈ {', '.join(RULES)}），收到 {item!r}")
        clear = val in ("", "none", "null")
        if rule is None:
            if not clear:
                raise ValueError(f"--alert {item!r} 缺规则名：写成 SYMBOL:RULE=VALUE（RULE ∈ {', '.join(RULES)}），或 SYMBOL=none 清空该标的全部")
            if out.get(sym):
                raise ValueError(f"--alert：{sym} 前面已设了规则，后面的 {sym}=none 会把它们整体清空，二选一")
            out[sym] = None
            continue
        try:
            num = None if clear else float(val)
        except ValueError:
            raise ValueError(f"--alert {item!r} 的值不是数字") from None
        cur = out.setdefault(sym, {})
        if cur is None:
            raise ValueError(f"--alert：{sym} 已被 {sym}=none 整体清空，不能再给它设 {rule}")
        cur[rule] = num
    return out


def cmd_set_allocations(args: argparse.Namespace) -> None:
    """改资产配置目标：股票/现金配比和/或按标的的目标权重 / 报警线。示例:
        skill set_allocations --stock 0.8 --cash 0.2
        skill set_allocations --target-pct 510300.SS=0.3 --target-pct GC=F=0.2
        skill set_allocations --stock 0.5 --cash 0.5 --target-pct 510300.SS=0.3   # 降股票目标要同次调低权重
        skill set_allocations --target-pct GC=F=none                               # 清掉某标的的权重
        skill set_allocations --alert GC=F:price_below=900 --alert GC=F:day_drop_pct=2   # 设报警线
        skill set_allocations --alert GC=F:price_below=none                        # 清掉一条；GC=F=none 清空该标的全部
    """
    from openinvest.services import strategy_write as svc

    try:
        pcts = _parse_target_pcts(getattr(args, "target_pct", None))
        alerts = _parse_alerts(getattr(args, "alert", None))
    except ValueError as e:
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _run(svc.set_allocations, args.stock, args.cash, target_pcts=pcts or None, alerts=alerts or None)


def cmd_track_asset(args: argparse.Namespace) -> None:
    """跟踪标的（新建或更新，幂等）。示例:
        skill track_asset --symbol AAPL --max-single-invest-cny 8000
        skill track_asset --symbol GC=F --sell-fee-pct 0.0038   # 只改一个字段
        skill track_asset --symbol 510300.SS --target-pct 0.3   # 目标权重（占总资产），rebalance_plan 读它
        skill track_asset --symbol GC=F --alert-price-below 900 --alert-day-drop-pct 2   # 报警线（哨兵每个 tick 判）
    新建时不带 --max-single-invest-cny 则沿用策略的单次入场上限。
    各标的 target_pct 之和必须 ≤ 股票目标，超了拒绝并报出当前合计。
    报警线只覆盖传入的那几条；清除用 set_allocations --alert SYMBOL:RULE=none。
    """
    from openinvest.services import strategy_write as svc

    alerts = {k: v for k, v in {
        "price_above": getattr(args, "alert_price_above", None),
        "price_below": getattr(args, "alert_price_below", None),
        "day_drop_pct": getattr(args, "alert_day_drop_pct", None),
        "day_rise_pct": getattr(args, "alert_day_rise_pct", None),
        "drawdown_from_cost_pct": getattr(args, "alert_drawdown_from_cost_pct", None),
    }.items() if v is not None}
    fields = {
        "display_name": args.display_name,
        "channel": args.channel,
        "max_single_invest_cny": args.max_single_invest_cny,
        "price_offset_pct": args.price_offset_pct,
        "sell_fee_pct": args.sell_fee_pct,
        "target_pct": getattr(args, "target_pct", None),
        "alerts": alerts or None,
    }
    _run(svc.upsert_target_asset, args.symbol, fields)


def cmd_untrack_asset(args: argparse.Namespace) -> None:
    """移除跟踪标的。示例:
        skill untrack_asset --symbol AAPL
    """
    from openinvest.services import strategy_write as svc

    _run(svc.remove_target_asset, args.symbol)


def cmd_rebalance_plan(args: argparse.Namespace) -> None:
    """目标配置 → 该买 / 卖多少的清单（只出清单，不下单，由你执行）。示例:
        skill rebalance_plan
        skill rebalance_plan --mode cash_only --band-pct 3
    """
    from openinvest.services.rebalance_plan import build_rebalance_plan

    out = build_rebalance_plan(mode=args.mode, band_pct=args.band_pct)
    _print_json(out)
    if out.get("status") == "error":
        sys.exit(1)
