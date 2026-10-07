#!/usr/bin/env python3
"""自动定投（dca_daily）幻影买入 / 漏记交易日对账 —— 默认 dry-run，只读不写。

为什么存在（2026-10）：
1. APScheduler from_crontab 星期 0=周一 → dca_daily 的 "1-5" 实际跑周二到周六：
   周六按周五价重复记账、周一从不记账（修：scheduler/cron.py）；
2. dca_daily 不看当天开没开盘：国庆 / 中秋按节前旧价记账（修：dca_daily 休市闸）。
代码已修，本脚本对账存量账本。

口径：交易日 = db/market_data.db 里该 symbol 有日线 bar 的日期（yfinance 行情，非官方日历）。
  (a) 幻影：dca_daily 记账日没有 bar（周末 / 节假日）
  (b) 漏记：有 bar 的交易日没有定投记账（不含 job_runs 显示 auto_dca_disabled 的停用期、不含今天）
  (c) 净差：对照"每个交易日 ¥amount @ 当日收盘"的正确口径，持仓份额 / 外部注资差多少
      （external_funding 不动子弹池现金 → portfolio cash 差额恒为 0）
--since 默认 = max(自动定投首笔, 该 symbol 最近一次 delete_holding 的次日)：持仓删了重建过，
重建已吸收更早的差异，再修就是重复修正；显式 --since 早于重建日时拒绝 --apply。
没留流水的手工校准（直接改 units 对齐真实基金份额）脚本看不见，同理把 --since 设为校准日。

--apply（人工确认后再跑；只动该 symbol 持仓 + 追加审计流水）：
  - 幻影：units 扣回该笔，avg_cost 按成本 units×avg 反解；流水 action=dca_reconcile_reverse
  - 漏记：按当日收盘补一笔外部注资买入；流水 action=dca_reconcile_backfill
  - 幂等（ADR-016）：漏记日 claim 与 dca_daily 同一把 dca_applied 键（互斥）；幻影 claim
    dca_reconcile_reversed；整批一个 with_portfolio_tx，失败全部 unclaim

    uv run python -m scripts.reconcile_dca_phantom                    # dry-run
    uv run python -m scripts.reconcile_dca_phantom --since 2026-07-02 # 从校准日起
    uv run python -m scripts.reconcile_dca_phantom --apply            # 人工确认后
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from openinvest.paths import INVEST_ROOT

SH = ZoneInfo("Asia/Shanghai")
DCA_START = "2026-06-23"  # 自动定投首笔
SOURCE = "reconcile_dca_phantom"


def _sh_date(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(SH).strftime("%Y-%m-%d")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_bars(db: Path, symbol: str, since: str, until: str) -> Dict[str, float]:
    """{date: close}；只读打开，不经 MarketStore（其构造会 checkpoint / 建表）"""
    with closing(_ro(db)) as c:
        return dict(c.execute(
            "SELECT date, close FROM daily_prices WHERE symbol=? AND date BETWEEN ? AND ? "
            "AND close IS NOT NULL ORDER BY date", (symbol, since, until)))


def load_runs(db: Path) -> List[Tuple[str, bool]]:
    """dca_daily 每次运行的 (北京日期, 是否 auto_dca_disabled)，按时间升序；无库 → []"""
    if not db.exists():
        return []
    with closing(_ro(db)) as c:
        rows = c.execute("SELECT started_at, output_excerpt FROM job_runs "
                         "WHERE job_name='dca_daily' ORDER BY started_at").fetchall()
    return [(_sh_date(ts), "auto_dca_disabled" in (out or "")) for ts, out in rows]


def _disabled_on(day: str, runs: List[Tuple[str, bool]]) -> bool:
    """当天及之前最近一次运行是 auto_dca_disabled → 用户主动停用期，不算漏记"""
    state = False
    for d, disabled in runs:
        if d > day:
            break
        state = disabled
    return state


def rebuild_floor(history: List[Dict[str, Any]], symbol: str) -> Optional[str]:
    """该 symbol 最近一次 delete_holding 的次日（北京日期）；从没删过 → None"""
    ds = [_sh_date(r.get("ts_origin") or r["ts"]) for r in history
          if r.get("symbol") == symbol and r.get("action") == "delete_holding"]
    return (date.fromisoformat(max(ds)) + timedelta(days=1)).isoformat() if ds else None


def plan(history: List[Dict[str, Any]], bars: Dict[str, float], runs: List[Tuple[str, bool]],
         symbol: str, since: str, until: str, today: str) -> Dict[str, Any]:
    dca: Dict[str, Dict[str, Any]] = {}
    reversed_, backfilled = set(), set()
    for r in history:
        if r.get("symbol") != symbol:
            continue
        d = r.get("trade_date") or _sh_date(r.get("ts_origin") or r["ts"])
        if not since <= d <= until:
            continue
        if r.get("action") == "buy" and r.get("source") == "dca_daily":
            dca[d] = r
        elif r.get("action") == "dca_reconcile_reverse":
            reversed_.add(d)
        elif r.get("action") == "dca_reconcile_backfill":
            backfilled.add(d)
    bars = {d: c for d, c in bars.items() if since <= d <= until}
    phantom = [(d, r) for d, r in sorted(dca.items()) if d not in bars and d not in reversed_]
    missing, disabled = [], []
    for d in sorted(bars):
        if d in dca or d in backfilled or d >= today:  # 今天归 dca_daily 自己管
            continue
        (disabled if _disabled_on(d, runs) else missing).append((d, bars[d]))
    return {"phantom": phantom, "missing": missing, "disabled": disabled,
            "last_bar": max(bars) if bars else None}


def apply(pm, symbol: str, p: Dict[str, Any], amount_cny: float) -> int:
    """按 plan 修账（with_portfolio_tx 单事务 + state_claim 幂等）；返回实际修正笔数"""
    store = pm.store
    items = []  # (claim_name, date, units_delta, price)
    for d, r in p["phantom"]:
        if store.state_claim("dca_reconcile_reversed", f"{d}:{symbol}"):
            items.append(("dca_reconcile_reversed", d, -float(r["units"]), float(r["price"])))
    for d, close in p["missing"]:
        if store.state_claim("dca_applied", f"{d}:{symbol}"):
            items.append(("dca_applied", d, round(amount_cny / close, 6), close))
    if not items:
        return 0
    try:
        with pm.with_portfolio_tx() as doc:
            holdings = list(doc.get("holdings") or [])
            h = next((x for x in holdings if x.get("symbol") == symbol), None)
            if h is None:
                raise ValueError(f"{symbol} 不在持仓")
            units = float(h["units"])
            cost = units * float(h["avg_cost"])
            for _, _, du, px in items:
                units += du
                cost += du * px
            if units <= 0:
                raise ValueError(f"修正后 {symbol} units={units} ≤ 0，拒绝写入")
            h["units"], h["avg_cost"] = round(units, 6), round(cost / units, 6)
            doc["holdings"] = holdings
    except Exception:
        for name, d, _, _ in items:
            store.state_unclaim(name, f"{d}:{symbol}")
        raise
    now = datetime.now(SH).isoformat(timespec="seconds")
    for _, d, du, px in items:
        store.append_history({
            "ts_origin": now, "trade_date": d, "symbol": symbol, "units": du, "price": px,
            "action": "dca_reconcile_reverse" if du < 0 else "dca_reconcile_backfill",
            "currency": "CNY", "source": SOURCE, "funding_source": "external_funding",
        })
    return len(items)


def _wd(d: str) -> str:
    return "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]


def report(p: Dict[str, Any], holding: Dict[str, Any], amount_cny: float,
           symbol: str, since: str, until: str) -> None:
    print(f"== dca 对账 {symbol}  {since} ~ {until}  (交易日=market_data.db 有 bar，最新 bar {p['last_bar']})")
    ph_u = sum(float(r["units"]) for _, r in p["phantom"])
    ph_cny = sum(float(r["units"]) * float(r["price"]) for _, r in p["phantom"])
    print(f"\n(a) 幻影买入（记账日无 bar）: {len(p['phantom'])} 笔")
    for d, r in p["phantom"]:
        print(f"    {d} 周{_wd(d)}  {float(r['units']):>11.6f} 份 @ {float(r['price']):.4f}")
    mi = [(d, c, round(amount_cny / c, 6)) for d, c in p["missing"]]
    mi_u = sum(u for _, _, u in mi)
    print(f"\n(b) 漏记交易日: {len(mi)} 天（停用期不计 {len(p['disabled'])} 天"
          f"{': ' + ', '.join(d for d, _ in p['disabled']) if p['disabled'] else ''}）")
    for d, c, u in mi:
        print(f"    {d} 周{_wd(d)}  收盘 {c:.4f} → 应记 {u:>11.6f} 份（¥{amount_cny:.0f}）")
    units = float(holding["units"])
    cost = units * float(holding["avg_cost"])
    new_units = units - ph_u + mi_u
    new_cost = cost - ph_cny + sum(u * c for _, c, u in mi)
    print("\n(c) 对照正确交易日口径的净差（正=账本少记）")
    print(f"    份额: -{ph_u:.6f}（幻影） +{mi_u:.6f}（漏记） = {mi_u - ph_u:+.6f} 份")
    print(f"    外部注资: -¥{ph_cny:,.2f} +¥{len(mi) * amount_cny:,.2f} = ¥{len(mi) * amount_cny - ph_cny:+,.2f}"
          "（external_funding，子弹池 cash 差额 = 0）")
    if new_units > 0:
        print(f"    持仓: {units:.6f} 份 @ {float(holding['avg_cost']):.6f} → "
              f"{new_units:.6f} 份 @ {new_cost / new_units:.6f}")
    late = [d for d, _ in p["phantom"] if p["last_bar"] and d > p["last_bar"]]
    if late:
        print(f"\n⚠️  {', '.join(late)} 晚于最新 bar {p['last_bar']}：'无 bar' 尚未被后续交易日确认，"
              "下一交易日行情入库后复跑；--apply 会拒绝")


def main() -> int:
    ap = argparse.ArgumentParser(description="dca_daily 幻影买入 / 漏记交易日对账（默认 dry-run）")
    ap.add_argument("--symbol", default="510300.SS")
    ap.add_argument("--since", default=None,
                    help=f"起始日（默认 max({DCA_START}, 最近一次 delete_holding 次日)；无流水的手工校准就设校准日）")
    ap.add_argument("--until", default=None, help="截止日（默认今天，北京日期）")
    ap.add_argument("--amount-cny", type=float, default=None, help="每日定投额（默认取 config.dca.auto_dca_amount_cny）")
    ap.add_argument("--apply", action="store_true", help="真正修账（人工确认后再用）")
    args = ap.parse_args()

    from openinvest.core.config import load_config
    from openinvest.core.portfolio_manager import PortfolioManager

    today = datetime.now(SH).strftime("%Y-%m-%d")
    until = args.until or today
    amount = args.amount_cny if args.amount_cny is not None else float(load_config().dca.auto_dca_amount_cny)
    pm = PortfolioManager()
    holding = pm.find_holding(args.symbol)
    if holding is None or str(holding.get("cost_currency", "CNY")).upper() != "CNY":
        print(f"{args.symbol} 不在持仓或非 CNY 计价，本脚本只对账 CNY 标的", file=sys.stderr)
        return 1
    history = pm.store.read_history()
    floor = rebuild_floor(history, args.symbol)
    since = args.since or max(DCA_START, floor or DCA_START)
    p = plan(history, load_bars(INVEST_ROOT / "db" / "market_data.db", args.symbol, since, until),
             load_runs(INVEST_ROOT / "db" / "jobs.sqlite"), args.symbol, since, until, today)
    report(p, holding, amount, args.symbol, since, until)
    if floor and since < floor:
        print(f"\n⚠️  --since {since} 早于持仓重建日 {floor}（之前 delete_holding 过），这段差异已被重建吸收")
    if not args.apply:
        print("\n(dry-run：未写任何东西；确认后加 --apply)")
        return 0
    if floor and since < floor:
        print("\n拒绝 --apply：窗口跨过持仓重建，会重复修正", file=sys.stderr)
        return 1
    if any(d > (p["last_bar"] or "") for d, _ in p["phantom"]):
        print("\n拒绝 --apply：有幻影日期晚于最新 bar，等行情入库确认后再跑", file=sys.stderr)
        return 1
    print(f"\n--apply：已修正 {apply(pm, args.symbol, p, amount)} 笔（幂等，重跑为 0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
