"""portfolio_cmds —— 写操作类 skill 子命令（存取现金 / 买卖 / 删持仓）

逐字搬运自 scripts/skill.py：
- _resolve_pm：统一拿 PortfolioManager（避免每个 cmd 重复 import）。
- cmd_deposit / cmd_withdraw / cmd_buy / cmd_sell / cmd_delete_holding：
  薄封装 core.portfolio_manager.PortfolioManager 的对应写方法（与 /api/skill/* 共享）。

CLAUDE.md 产品哲学：Agent 必须拥有全部功能（不能只读不写）。
错误路径保持历史行为：部分 cmd 用 sys.exit(1)，部分 raise SystemExit(json.dumps(...)) 走 stderr。
"""
from __future__ import annotations

import argparse
import json
import sys

from openinvest.skill_cmds._helpers import _print_json

__all__ = [
    "_resolve_pm",
    "cmd_deposit",
    "cmd_withdraw",
    "cmd_buy",
    "cmd_sell",
    "cmd_delete_holding",
    "cmd_import_holdings",
    "cmd_record_dividend",
    "cmd_record_fee",
    "cmd_record_split",
    "cmd_record_return_of_capital",
    "cmd_place_fund_order",
    "cmd_confirm_fund_order",
    "cmd_cancel_fund_order",
]


def _resolve_pm():
    """统一拿 PortfolioManager，避免每个 cmd 重复 import"""
    from openinvest.core.portfolio_manager import PortfolioManager
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
        out = pm.deposit_cash(args.currency, args.amount, source="skill_cli",
                              idempotency_key=getattr(args, "idempotency_key", None))
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
        out = pm.withdraw_cash(args.currency, amount, source="skill_cli",
                               idempotency_key=getattr(args, "idempotency_key", None))
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
        skill buy --symbol 510300.SS --units 3000 --price 4.2 --kind etf --existing-position

    --existing-position：用户用系统前就持有的仓位（onboarding 补录）不是一笔现金买入——
    走已有的 external_funding（不扣现金），history source 标 ':existing_position' 以便区分。

    写逻辑在 core/portfolio_manager.py:PortfolioManager.buy（与 /api/skill/buy 共享）
    """
    pm = _resolve_pm()
    existing = bool(getattr(args, "existing_position", False))
    try:
        out = pm.buy(
            args.symbol, float(args.units), float(args.price),
            currency=args.currency, kind=args.kind, unit_label=args.unit_label,
            source="skill_cli:existing_position" if existing else "skill_cli",
            source_type="external_funding" if existing else "cash_deduct",
            idempotency_key=getattr(args, "idempotency_key", None),
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
        out = pm.sell(args.symbol, units, price, source="skill_cli",
                      idempotency_key=getattr(args, "idempotency_key", None))
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


def cmd_import_holdings(args: argparse.Namespace) -> None:
    """把自由文本/CSV 持仓描述用 LLM 解析成结构化持仓（等价 POST /api/holdings/import）。

    示例:
        skill import --file holdings.csv            # 只预览解析结果，不落盘
        skill import --text "510300 ETF 3000股 4.2元 + 余额宝 5万" --commit
        cat brokerage.csv | skill import --commit   # 不给 --file/--text 则读 stdin

    --commit 走**非破坏**写入：只加 portfolio 里还没有的 symbol、cash 只填当前为 0 的币种，
    已存在的跳过并在 summary 报告（重复导入幂等，不覆盖真实数据）。需要 LLM_API_KEY / DEEPSEEK_API_KEY。
    """
    from pathlib import Path

    from openinvest.services.holdings_import import commit_parsed, parse_holdings

    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        content = args.text
    else:
        content = sys.stdin.read()
    if not content.strip():
        _print_json({"status": "error", "error": "无输入（用 --file / --text，或管道喂 stdin）"})
        sys.exit(1)

    try:
        parsed = parse_holdings(content)
    except ValueError as e:  # 无 LLM key
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)

    out = {"status": "ok", "parsed": parsed, "committed": False}
    if args.commit:
        out["summary"] = commit_parsed(_resolve_pm(), parsed)
        out["committed"] = True
    _print_json(out)


# ---- ADR-027 PR2：分红 / 利息 / 费用 / 税 / 拆股 / 退回本金（与 MCP 同名工具同语义，写逻辑在 PortfolioManager.record_*）----

def _run_record(call) -> None:
    try:
        out = call()
    except ValueError as e:
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
    _print_json(out)


def cmd_record_dividend(args: argparse.Namespace) -> None:
    """记分红（--kind dividend，默认）或利息（--kind interest）。现金 +净额，不是外部现金流。

    示例:
        skill record_dividend --symbol NDQ.AX --amount 120 --currency AUD --trade-date 2026-07-01
        skill record_dividend --symbol AAPL --amount 50 -c USD --tax-withheld 7.5
        skill record_dividend --symbol AAPL --amount 50 -c USD --reinvest --reinvest-price 190.5   # DRIP
        skill record_dividend --kind interest --amount 3.2 -c CNY --symbol 000198.OF

    写逻辑在 core/portfolio_manager.py:PortfolioManager.record_dividend / record_interest
    """
    pm = _resolve_pm()
    key = getattr(args, "idempotency_key", None)
    if args.kind == "interest":
        if args.tax_withheld or args.reinvest or args.reinvest_price:
            _print_json({"status": "error", "error": "interest 不支持 --tax-withheld / --reinvest（预扣税请另记 record_fee --kind tax）"})
            sys.exit(1)
        _run_record(lambda: pm.record_interest(
            args.amount, args.currency, symbol=args.symbol, trade_date=args.trade_date, note=args.note,
            source="skill_cli", idempotency_key=key))
        return
    if not args.symbol:
        _print_json({"status": "error", "error": "分红必须给 --symbol"})
        sys.exit(1)
    _run_record(lambda: pm.record_dividend(
        args.symbol, args.amount, args.currency, tax_withheld=args.tax_withheld, reinvest=args.reinvest,
        trade_date=args.trade_date, note=args.note, reinvest_price=args.reinvest_price,
        source="skill_cli", idempotency_key=key))


def cmd_record_fee(args: argparse.Namespace) -> None:
    """记费用（--kind fee，默认）或税（--kind tax）：现金 −金额，是账户成本，不是外部现金流。

    示例:
        skill record_fee --amount 15 --currency AUD --note "account fee"
        skill record_fee --kind tax --amount 7.5 --currency USD --symbol AAPL

    写逻辑在 core/portfolio_manager.py:PortfolioManager.record_fee
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.record_fee(
        args.amount, args.currency, symbol=args.symbol, kind=args.kind, trade_date=args.trade_date,
        note=args.note, source="skill_cli", idempotency_key=getattr(args, "idempotency_key", None)))


def cmd_record_split(args: argparse.Namespace) -> None:
    """记拆股 / 并股：units × ratio、avg_cost ÷ ratio，现金不变。ratio = 新/旧（10 拆 1 → 10；10 合 1 → 0.1）。

    示例:
        skill record_split --symbol NVDA --ratio 10 --trade-date 2024-06-10
        skill record_split --symbol NVDA --ratio 10 --trade-date 2024-06-10 --ledger-only   # 快照份数已是拆股后的

    补记历史拆股：除权日之后若已记过该标的的买卖 / 修正（按拆股后口径记的），整体 × ratio 会凭空多出份数，
    所以会被拒绝；确认快照份数本来就对时加 --ledger-only 只补流水。

    写逻辑在 core/portfolio_manager.py:PortfolioManager.record_split
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.record_split(
        args.symbol, args.ratio, trade_date=args.trade_date, note=args.note,
        ledger_only=bool(getattr(args, "ledger_only", False)), source="skill_cli",
        idempotency_key=getattr(args, "idempotency_key", None)))


def cmd_record_return_of_capital(args: argparse.Namespace) -> None:
    """记退回本金：现金 +金额，avg_cost 降低 amount/持仓量，数量不变（成本降低，不是收益）。

    示例:
        skill record_return_of_capital --symbol VNQ --amount 30 --currency USD

    写逻辑在 core/portfolio_manager.py:PortfolioManager.record_return_of_capital
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.record_return_of_capital(
        args.symbol, args.amount, args.currency, trade_date=args.trade_date, note=args.note,
        source="skill_cli", idempotency_key=getattr(args, "idempotency_key", None)))


# ---- ADR-027 PR3：场外基金两段式（与 MCP 同名工具同语义，写逻辑在 PortfolioManager.place/confirm/cancel_fund_order）----

def cmd_place_fund_order(args: argparse.Namespace) -> None:
    """场外基金申购第一段：现金 −金额，订单记在途；总资产按面值含它，下单到确认之间不会假跌。

    示例:
        skill place_fund_order --symbol FUND:110020 --amount 2000
        skill place_fund_order --symbol FUND:270042 --amount 1000 --order-ts 2026-10-19T16:20:00   # 15:00 后下单 → 按下一交易日净值

    份额由 fund_confirm job（工作日晚间）在定价日净值公布后自动确认，也可手动 confirm_fund_order。
    写逻辑在 core/portfolio_manager.py:PortfolioManager.place_fund_order
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.place_fund_order(
        args.symbol, args.amount, args.currency, order_ts=args.order_ts, note=args.note,
        idempotency_key=getattr(args, "idempotency_key", None), source="skill_cli"))


def cmd_confirm_fund_order(args: argparse.Namespace) -> None:
    """场外基金申购第二段：在途单按定价日净值换成份额。已确认返回 already_confirmed、净值没公布返回 pending_nav，都不改账。

    示例:
        skill confirm_fund_order --order-id 3f2a...            # 取定价日已公布的净值
        skill confirm_fund_order --order-id 3f2a... --nav 2.0153 --fee 1.5

    写逻辑在 core/portfolio_manager.py:PortfolioManager.confirm_fund_order
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.confirm_fund_order(args.order_id, nav=args.nav, fee=args.fee, source="skill_cli"))


def cmd_cancel_fund_order(args: argparse.Namespace) -> None:
    """撤销在途申购单：金额退回现金。已确认的拒绝（份额已到账，要卖用 sell）。

    示例:
        skill cancel_fund_order --order-id 3f2a...

    写逻辑在 core/portfolio_manager.py:PortfolioManager.cancel_fund_order
    """
    pm = _resolve_pm()
    _run_record(lambda: pm.cancel_fund_order(args.order_id, source="skill_cli"))
