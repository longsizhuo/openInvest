"""fund_rules.py — 场外公募基金两段式（下单扣钱记在途 → 确认日按净值到账）的日期规则（calc 层 T0，ADR-026：禁 IO / 禁时钟）

时间是输入不是环境：下单时刻由调用方传入，本模块不读时钟。

规则（ADR-027 D5）：
- **T（定价日）**：北京时间 15:00 之前下单、且当天是交易日 → T = 当日；15:00 整点及之后、或非交易日下单 → T = 下一个交易日。
  申购份额按 T 日的单位净值（``nav_date`` = T）确认，对所有基金类型都一样；QDII / FOF 只是净值公布与券商确认更晚。
- **确认日 = T + lag 个交易日**：``lag`` 按基金类型取配置 ``fund.confirm_lag_days``（默认 ``{default: 1, qdii: 2, fof: 2}``）。
  确认日只是"预计看到份额"的日期（status 展示 / 超期提示用）；``fund_confirm`` job 的触发条件是 T 日净值已公布，不是确认日到了。
- **交易日历 = 周一到周五**（``calendar="weekday_only"``）：没有 A 股节假日表。节假日当天的 T 会落在休市日——
  :func:`pick_nav` 会顺延用之后第一个已公布的净值（节假日没有净值，下一个交易日的净值就是实际成交价），
  但预计确认日仍可能比券商晚几天，所以每条记录都带 ``calendar`` 字段，读方知道这是估计。
- **基金类型**：从东方财富基金概况（基金全称 / 简称 / 类型）推断，名字或类型里带 ``QDII`` → ``qdii``，带 ``FOF`` → ``fof``，
  其余 ``default``（:func:`classify_fund_kind`）。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

# 北京时间固定 +8（中国无夏令时）；不用 ZoneInfo，保持 calc 层不碰系统时区库
CN_TZ = timezone(timedelta(hours=8))
CUTOFF = time(15, 0)

CALENDAR_WEEKDAY_ONLY = "weekday_only"
_TRADING_DAY: Dict[str, Callable[[date], bool]] = {CALENDAR_WEEKDAY_ONLY: lambda d: d.weekday() < 5}

DEFAULT_LAG_DAYS = {"default": 1, "qdii": 2, "fof": 2}


def to_cn(ts: datetime) -> datetime:
    """下单时刻 → 北京时间。naive 视为已经是北京时间。"""
    return ts.astimezone(CN_TZ) if ts.tzinfo is not None else ts.replace(tzinfo=CN_TZ)


def _is_trading_day(d: date, calendar: str) -> bool:
    try:
        return _TRADING_DAY[calendar](d)
    except KeyError:
        raise ValueError(f"未知交易日历 {calendar!r}（目前只有 {'/'.join(_TRADING_DAY)}）") from None


def next_trading_day(d: date, calendar: str = CALENDAR_WEEKDAY_ONLY) -> date:
    """严格晚于 ``d`` 的下一个交易日。"""
    d += timedelta(days=1)
    while not _is_trading_day(d, calendar):
        d += timedelta(days=1)
    return d


def add_trading_days(d: date, n: int, calendar: str = CALENDAR_WEEKDAY_ONLY) -> date:
    """``d`` 往后数 ``n`` 个交易日（n=0 返回 ``d`` 自己；``d`` 须已是交易日）。"""
    if n < 0:
        raise ValueError("n 不能为负")
    for _ in range(n):
        d = next_trading_day(d, calendar)
    return d


def pricing_date(order_ts: datetime, calendar: str = CALENDAR_WEEKDAY_ONLY) -> date:
    """T：15:00（北京时间）前下单且当天是交易日 → 当日，否则下一个交易日。15:00:00 整点算收盘后。"""
    ts = to_cn(order_ts)
    if _is_trading_day(ts.date(), calendar) and ts.time() < CUTOFF:
        return ts.date()
    return next_trading_day(ts.date(), calendar)


def expected_confirm_date(order_ts: datetime, lag_days: int, calendar: str = CALENDAR_WEEKDAY_ONLY) -> date:
    """预计确认日 = T + ``lag_days`` 个交易日。"""
    return add_trading_days(pricing_date(order_ts, calendar), int(lag_days), calendar)


def classify_fund_kind(*texts: Optional[str]) -> str:
    """``qdii`` | ``fof`` | ``default``：基金全称 / 简称 / 类型里任一处含 QDII 或 FOF（不分大小写）。QDII 优先。"""
    blob = " ".join(t for t in texts if t).upper()
    for kind in ("qdii", "fof"):
        if kind.upper() in blob:
            return kind
    return "default"


def resolve_lag_days(kind: Optional[str], table: Optional[Mapping[str, Any]]) -> int:
    """按基金类型取确认延迟（交易日）。类型缺 / 表里没有 / 值不是非负整数 → ``default``（再不行 1）。"""
    def _ok(v: Any) -> Optional[int]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 and n == float(v) else None

    t: Mapping[str, Any] = table if isinstance(table, Mapping) else {}   # env 误设成标量时当没配
    for key in (kind, "default"):
        if key is not None and (n := _ok(t.get(key))) is not None:
            return n
    return DEFAULT_LAG_DAYS["default"]


def pick_nav(history: Sequence[Tuple[str, float]], nav_date: str) -> Optional[Tuple[str, float]]:
    """订单定价日 ``nav_date`` 的单位净值 → ``(实际净值日, 净值)``；还没公布 → ``None``。

    ``history`` = ``[(YYYY-MM-DD, nav), ...]`` 日期升序（``fetch_fund_nav_history``）。``nav_date`` 当天有就用；
    当天没有、但更晚的净值已公布（休市日 / 节假日 / QDII 海外休市：我们的日历不认识这些）→ 顺延用第一个 ≥ ``nav_date`` 的，
    那才是实际成交价；最新净值仍早于 ``nav_date`` → 还没公布。
    """
    for d, nav in history:
        if d >= nav_date:
            return d, nav
    return None


def _money(ccy: str, amount: float) -> str:
    return f"¥{amount:,.2f}" if ccy == "CNY" else f"{ccy} {amount:,.2f}"


def pending_summary_line(orders: Sequence[Mapping[str, Any]], today: str) -> str:
    """status 的在途一行人话：``在途待确认 N 笔（¥5,000.00），预计 2026-10-20``。没有在途单 → 空串。

    - 预计 = 在途单里最早的预计确认日。
    - 有单的确认延迟是按默认值估的（基金类型没取到，``lag_source=default``）→ 标"按 T+N 估计"。
    - 预计确认日已过但还没确认（净值没取到）→ 提示手填净值或撤销。
    """
    if not orders:
        return ""
    by_ccy: Dict[str, float] = {}
    for o in orders:
        ccy = str(o.get("currency") or "CNY").upper()
        by_ccy[ccy] = by_ccy.get(ccy, 0.0) + float(o.get("amount") or 0)
    amounts = " + ".join(_money(c, v) for c, v in sorted(by_ccy.items()))
    dates = [str(o["expected_confirm_date"]) for o in orders if o.get("expected_confirm_date")]
    line = f"在途待确认 {len(orders)} 笔（{amounts}）" + (f"，预计 {min(dates)}" if dates else "")
    guessed = next((o for o in orders if o.get("lag_source") == "default"), None)
    if guessed is not None:
        line += f"（基金类型未取到，按 T+{guessed.get('lag_days', 1)} 估计）"
    overdue = sum(1 for d in dates if d < today)
    if overdue:
        line += f"；{overdue} 笔已超预计确认日仍未取到净值——可用 confirm_fund_order 手填净值，或 cancel_fund_order 撤销"
    return line


__all__ = [
    "CALENDAR_WEEKDAY_ONLY", "CN_TZ", "CUTOFF", "DEFAULT_LAG_DAYS", "add_trading_days", "classify_fund_kind",
    "expected_confirm_date", "next_trading_day", "pending_summary_line", "pick_nav", "pricing_date",
    "resolve_lag_days", "to_cn",
]
