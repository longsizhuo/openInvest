"""盈透（IBKR）实盘 autopilot —— Claude 选标的、委员会定买卖、本模块下**真单**。opt-in，只应在独立 INVEST_HOME 开。

三段（cron 按美股时段跑；日期一律北京日期，与 .committee/<date>/ 同口径）：
  pick       每周一（或 target_assets 为空）：LLM 在硬限制内选 ≤5 只美股 / 美股 ETF，整表替换本 home 的
             target_assets。LLM 输出必须过 _check_tradable（美股上市、USD、股价、流动性、非杠杆 / 反向），不过的丢掉。
  submit     美股开盘前：①持有但已不在 target_assets 的 → 清仓；②当日委员会 ACCUMULATE/BUY/TRIM/SELL 裁决 →
             DAY 限价单（参考价 ±1%，开盘跳空超过就不成交、收盘作废）。下单成功即 record_execution(True)
             （程序照做了；成没成交以 reconcile 记的真实成交为准）。
  reconcile  美股收盘后：拉当日成交，按 execId 幂等记账（buy/sell + 佣金 fee）；持仓和券商对不上 → 写 STOP 停手。

硬闸（代码层，LLM / 委员会给什么都先过这里）——金额全按 USD：
  ibkr.enabled 默认关；INVEST_HOME/STOP 存在就不下单；连上的账户 ≠ ibkr.account 就什么都不做；
  只交易 target_assets 里的标的（清仓例外：卖的是券商里已有的持仓）；单笔 ≤ max_order_usd；当日累计
  （已挂未成交 + 当日已成交）≤ max_day_usd；< min_order_usd 不下（整笔清仓例外）；只挂限价单；
  卖出量 ≤ 券商持仓（现金账户本身不能融资 / 做空）。
ibkr.dry_run=True（默认）：whatIfOrder 试算——盈透校验合约 / 权限 / 碎股 / 佣金，不下单、不记 execution、不占 claim。

幂等（ADR-016）：submit 每个 (date, symbol, side) 一把 state_claim，下单后不 unclaim；盈透侧已有同 orderRef 的
挂单 / 成交也跳过（claim 丢失兜底）。reconcile 每笔成交带 idempotency_key=ibkr-<execId>，重跑只会 duplicate。
"""
from __future__ import annotations

import html
import json
import logging
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from openinvest.core.config import load_config
from openinvest.core.decision_ledger import list_decisions, record_execution
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from openinvest.jobs.paper_autopilot import _BUY, _SELL, _holding_for, round_down_units
from openinvest.paths import INVEST_ROOT
from openinvest.utils.quotes import get_quote

log = logging.getLogger(__name__)

_SRC = "ibkr_autopilot"
_REF = "oi:"            # orderRef 前缀：只把带它的挂单 / 成交算进本程序的当日额度
_PORT = 4001            # docker compose 把网关 live API 映到 127.0.0.1:4001
_CLIENT_ID = 17
_BAND = 0.01            # 限价 = 参考价 ±1%
_MAX_PICKS = 5
_MIN_PRICE = 5.0
_MIN_DOLLAR_VOL = 20e6  # 3 个月日均成交额（USD）下限：挡小盘 / 易操纵的票
_US_EXCH = {"NMS", "NGM", "NCM", "NYQ", "PCX", "ASE", "BTS"}
# ponytail: 名字关键词挡杠杆 / 反向 ETF，够挡 ProShares / Direxion / 2x-3x 系列；漏网的靠单笔 / 单日上限兜底
_LEVERAGED = re.compile(r"(?i)\b(\d(\.\d+)?x|-\d+x|ultra\w*|inverse|bear|short(?!-term)|leveraged|daily)\b")
_QUOTE_MAX_AGE_DAYS = 5  # 参考价最晚是 5 个自然日前的收盘（周末 + 长假）


class _Skip(Exception):
    """terminal=True：判定已定（太小 / 没仓位 / 没钱 / 被拒），留 claim；False：什么都没下、条件可能自愈 → unclaim。"""
    def __init__(self, reason: str, terminal: bool = False):
        super().__init__(reason)
        self.reason, self.terminal = reason, terminal


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _floor_cent(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def _stop_file():
    return INVEST_ROOT / "STOP"


def _halt(reason: str) -> None:
    """写 STOP（之后 submit 一律不下单，人看过删掉才恢复）+ 发邮件。"""
    _stop_file().write_text(f"{datetime.now().isoformat(timespec='seconds')} {reason}\n", encoding="utf-8")
    log.error(f"[ibkr] HALT: {reason}")
    _notify("⛔ 盈透 autopilot 已停手", [reason, f"处理完删掉 {_stop_file()} 才会恢复下单。"])


def _notify(subject: str, lines: List[str]) -> None:
    try:
        from openinvest.services.notifier import send_email_html
        body = "\n".join(lines)   # 含 LLM 写的理由 / 券商回的拒单文本 → HTML 版必须转义
        send_email_html(subject=subject, plain_body=body, html_body=f"<pre>{html.escape(body)}</pre>")
    except Exception as e:  # noqa: BLE001 — 通知失败不影响账本
        log.warning(f"[ibkr] 邮件发送失败: {e}")


# ---------------------------------------------------------------- broker 读


def _connect():
    from ib_async import IB
    ib = IB()
    ib.connect("127.0.0.1", _PORT, clientId=_CLIENT_ID, timeout=20)
    return ib


def _broker_cash(ib, acct: str) -> float:
    for v in ib.accountValues(acct):
        if v.tag == "CashBalance" and v.currency == "USD":
            return float(v.value)
    return 0.0


def _broker_positions(ib, acct: str) -> Dict[str, float]:
    return {p.contract.symbol: float(p.position) for p in ib.positions(acct)
            if p.contract.secType == "STK" and float(p.position)}


def _fills(ib, acct: str) -> list:
    ib.reqExecutions()
    ib.sleep(2)   # 佣金报告在成交回报之后到，等一下再读 ib.fills()
    return [f for f in ib.fills() if f.execution.acctNumber == acct]


def _used_today(ib, acct: str, fills: list) -> tuple[float, float, set]:
    """(当日已用额度, 挂着的买单占用现金, 已存在的 orderRef)。只算本程序的单（orderRef 带前缀）。"""
    used = reserved = 0.0
    refs = set()
    for t in ib.openTrades():
        o = t.order
        if not str(o.orderRef or "").startswith(_REF) or (o.account and o.account != acct):
            continue
        left = float(o.totalQuantity) - float(t.orderStatus.filled or 0)
        used += left * float(o.lmtPrice)
        if o.action == "BUY":
            reserved += left * float(o.lmtPrice)
        refs.add(o.orderRef)
    for f in fills:
        if str(f.execution.orderRef or "").startswith(_REF):
            used += float(f.execution.shares) * float(f.execution.price)
            refs.add(f.execution.orderRef)
    return used, reserved, refs


# ---------------------------------------------------------------- pick


def _check_tradable(symbol: str, info: Dict[str, Any]) -> Optional[str]:
    """None = 可以；否则返回拒绝原因。info = yfinance Ticker.info。"""
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", symbol):
        return "not_us_ticker"
    if str(info.get("quoteType") or "").upper() not in ("EQUITY", "ETF"):
        return "not_stock_or_etf"
    if str(info.get("currency") or "").upper() != "USD":
        return "not_usd"
    if str(info.get("exchange") or "").upper() not in _US_EXCH:
        return "not_us_listed"
    name = str(info.get("longName") or info.get("shortName") or "")
    if _LEVERAGED.search(name):
        return "leveraged_or_inverse"
    price = float(info.get("regularMarketPrice") or info.get("previousClose") or 0)
    if price < _MIN_PRICE:
        return "price_below_min"
    vol = float(info.get("averageDailyVolume3Month") or info.get("averageVolume") or 0)
    if price * vol < _MIN_DOLLAR_VOL:
        return "illiquid"
    return None


def _ticker_info(symbol: str) -> Dict[str, Any]:
    import yfinance as yf
    return dict(yf.Ticker(symbol).info or {})


def _pick_agent(system_prompt: str):
    from openinvest.capabilities.sdk_agent import SDKAgent
    from openinvest.utils.llm import get_llm_config_safe
    api_key, base_url, model, provider = get_llm_config_safe()
    if not api_key:
        raise RuntimeError("LLM_API_KEY 缺失")
    return SDKAgent(system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url,
                    temperature=0.3, enable_tools=True, max_tool_iterations=8, provider=provider)


_PICK_SYSTEM = """你在管理一个盈透（Interactive Brokers）实盘现金账户，钱是用户的真钱。每周一由你决定本周要持有的标的。
选中的标的之后每个交易日由投资委员会决定买卖时机和金额；**不在你名单里的现有持仓会被清仓**。

硬限制（代码会逐条校验，不满足的直接丢掉，不要浪费名额）：
- 最多 5 只；美股上市的普通股或 ETF，USD 计价，股价 ≥ $5，3 个月日均成交额 ≥ $2000 万
- 不要杠杆 / 反向 / 做空类 ETF
- 现金账户：不能融资、不能做空；单笔 ≤ USD {max_order}，单日 ≤ USD {max_day}；支持碎股；每单佣金最低约 USD 0.35

你可以调用工具查历史行情、多周期技术面和宏观快照。只输出一个 JSON，不要别的文字：
{{"picks": [{{"symbol": "代码", "why": "一句话理由"}}], "note": "本周整体思路，一两句"}}"""


def _pick_prompt(pm: PortfolioManager) -> str:
    held = []
    for h in pm.holdings:
        if float(h.get("units") or 0) <= 0:
            continue
        snap = get_quote(dict(h))
        px = f"{snap.price:.2f}" if snap and snap.price else "?"
        held.append(f"- {h['symbol']}: {float(h['units']):g} 股，成本 {float(h.get('avg_cost') or 0):.2f}，现价 {px}")
    targets = ", ".join(t["symbol"] for t in pm.strategy.get("target_assets") or []) or "（无）"
    body = (MemoryStore().read("strategy").body or "").strip()
    return "\n".join([
        f"今天 {_today()}。账户现金 USD {pm.cash_amount('USD'):.2f}。",
        "当前持仓：", *(held or ["（空仓）"]),
        f"上周名单：{targets}",
        "", "账户 / 用户背景（strategy.md 正文）：", body or "（无）",
    ])


def _parse_picks(raw: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("LLM 没输出 JSON")
    data = json.loads(m.group(0))
    picks = data.get("picks")
    if not isinstance(picks, list):
        raise ValueError("JSON 缺 picks 列表")
    return data


def pick(today: Optional[str] = None, *, force: bool = False, agent=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    today = today or _today()
    pm = PortfolioManager()
    current = pm.strategy.get("target_assets") or []
    if not force and current and datetime.strptime(today, "%Y-%m-%d").weekday() != 0:
        return {"status": "skipped", "reason": "not_pick_day"}

    agent = agent or _pick_agent(_PICK_SYSTEM.format(max_order=f"{cfg.max_order_usd:g}", max_day=f"{cfg.max_day_usd:g}"))
    data = _parse_picks(agent.run(_pick_prompt(pm)))

    ok: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for p in data["picks"]:
        sym = str((p or {}).get("symbol") or "").strip().upper()
        if not sym or any(a["symbol"] == sym for a in ok):
            continue
        if len(ok) >= _MAX_PICKS:
            rejected.append({"symbol": sym, "reason": "over_max_picks"})
            continue
        try:
            info = _ticker_info(sym)
        except Exception as e:  # noqa: BLE001
            rejected.append({"symbol": sym, "reason": f"no_info: {e}"})
            continue
        why = _check_tradable(sym, info)
        if why:
            rejected.append({"symbol": sym, "reason": why})
            continue
        ok.append({"symbol": sym, "display_name": str(info.get("shortName") or sym)[:40],
                   "channel": "IBKR", "max_single_invest_cny": _cap_cny(cfg.max_order_usd),
                   "_why": str(p.get("why") or "")[:200]})

    log_dir = MemoryStore().root / ".ibkr" / "picks"
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {"date": today, "picks": ok, "rejected": rejected, "note": data.get("note"),
              "previous": [t["symbol"] for t in current]}
    (log_dir / f"{today}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    if not ok:   # LLM 全选错时别把名单清空（会触发全仓清仓）
        return {"status": "error", "reason": "no_valid_picks", **record}

    from openinvest.services.strategy_write import replace_target_assets
    # ponytail: 新标的历史由委员会首次 get_history_data 按需从 yfinance 拉；要全量 period=max 时跑 scripts.backfill_history
    replace_target_assets([{k: v for k, v in a.items() if not k.startswith("_")} for a in ok])
    _notify("🧭 盈透 autopilot 本周名单", [f"{a['symbol']}: {a['_why']}" for a in ok]
            + [f"丢弃 {r['symbol']}: {r['reason']}" for r in rejected] + [str(data.get("note") or "")])
    return {"status": "ok", **record}


def _cap_cny(usd: float) -> int:
    """单笔上限换成 strategy 的人民币标定口径（委员会按它钳建议金额）；拉不到汇率给 0 = 不设限（代码闸照样钳）。"""
    try:
        from openinvest.utils.fx import get_fx_rate
        r = get_fx_rate("USD", "CNY")
        return int(math.floor(usd * r)) if r and r > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- submit


def _ref_price(pm: PortfolioManager, symbol: str, today: str) -> float:
    snap = get_quote(_holding_for(pm, symbol))
    if snap is None or not snap.price or snap.price <= 0:
        raise _Skip("no_price")
    if snap.is_stale:
        raise _Skip("stale_quote")
    if str(snap.currency or "").upper() != "USD":
        raise _Skip("not_usd", terminal=True)
    if snap.last_updated:
        age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(snap.last_updated, "%Y-%m-%d")).days
        if age > _QUOTE_MAX_AGE_DAYS:
            raise _Skip("stale_quote")
    return float(snap.price)


def _alloc_usd(d: Dict[str, Any]) -> float:
    amt = abs(float(d.get("alloc_cny") or 0))
    ccy = str(d.get("alloc_currency") or "USD").upper()
    if not amt or ccy == "USD":
        return amt
    from openinvest.utils.fx import get_fx_rate
    r = get_fx_rate(ccy, "USD")
    if not r or r <= 0:
        raise _Skip("no_fx")
    return amt * float(r)


def _size(a: Dict[str, Any], ref: float, cash: float, pos: float, room: float, cfg) -> tuple[float, float]:
    """(qty, lmt)。room = min(单笔上限, 当日剩余额度)。"""
    if room < cfg.min_order_usd:
        raise _Skip("day_cap_reached", terminal=True)
    if a["side"] == "BUY":
        lmt = round(ref * (1 + _BAND), 2)
        usd = min(a["usd"], room, cash - 1.0)   # 留 1 美元给佣金，现金账户不能透支
        qty = round_down_units(usd / lmt, None)
        if qty * lmt < cfg.min_order_usd:
            raise _Skip("no_cash" if cash - 1.0 < cfg.min_order_usd else "too_small", terminal=True)
        return qty, lmt
    if pos <= 0:
        raise _Skip("no_position", terminal=True)
    lmt = round(ref * (1 - _BAND), 2)
    want = pos if a["full"] else min(round_down_units(a["usd"] / ref, None), pos)
    qty = min(want, round_down_units(room / ref, None))
    if qty <= 0 or (qty * ref < cfg.min_order_usd and qty < pos - 1e-9):
        raise _Skip("too_small", terminal=True)
    return qty, lmt


def _actions(pm: PortfolioManager, positions: Dict[str, float], today: str) -> List[Dict[str, Any]]:
    """当日要做的事：先清仓（不在名单里的持仓），再委员会裁决；卖在买前（回款当天可再买）。"""
    targets = {t["symbol"] for t in pm.strategy.get("target_assets") or []}
    out = [{"symbol": s, "side": "SELL", "full": True, "usd": 0.0, "decision": None,
            "ref": f"{_REF}exit:{today}/{s}"} for s in sorted(positions) if s not in targets]
    for d in list_decisions(days=3):
        if d["date"] != today or d["execution"] is not None or d["verdict"] not in _BUY + _SELL:
            continue
        if d["symbol"] not in targets:
            continue   # 只交易名单里的标的（白名单）
        out.append({"symbol": d["symbol"], "side": "BUY" if d["verdict"] in _BUY else "SELL",
                    "full": d["verdict"] == "SELL", "usd": None, "decision": d,
                    "ref": f"{_REF}{d['decision_id']}"})
    return sorted(out, key=lambda a: (a["side"] == "BUY", a["symbol"]))


def _caps_error(cfg) -> Optional[str]:
    """金额闸本身不合法（NaN / inf / ≤0 / 最小单大于单笔上限）→ 一单都不下（fail closed）。"""
    caps = (cfg.max_order_usd, cfg.max_day_usd, cfg.min_order_usd)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in caps):
        return f"caps_not_finite: {caps}"
    if cfg.max_order_usd <= 0 or cfg.max_day_usd <= 0 or cfg.min_order_usd < 0 or cfg.min_order_usd > cfg.max_order_usd:
        return f"caps_invalid: {caps}"
    return None


def submit(today: Optional[str] = None, *, ib=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    bad = _caps_error(cfg)
    if bad:
        return {"status": "error", "reason": bad}
    if _stop_file().exists():
        return {"status": "halted", "reason": _stop_file().read_text(encoding="utf-8").strip()}
    today = today or _today()
    ib = ib or _connect()
    acct = cfg.account
    if not acct or acct not in ib.managedAccounts():
        return {"status": "error", "reason": f"account_mismatch: 配置 {acct!r}，网关 {ib.managedAccounts()}"}

    from ib_async import LimitOrder, Stock
    pm = PortfolioManager()
    positions = _broker_positions(ib, acct)
    cash = _broker_cash(ib, acct)
    used, reserved, refs = _used_today(ib, acct, _fills(ib, acct))
    cash -= reserved
    placed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for a in _actions(pm, positions, today):
        sym, d = a["symbol"], a["decision"]
        if d is not None and d["verdict"] in _BUY + ("TRIM",) and not abs(float(d.get("alloc_cny") or 0)):
            skipped.append({"symbol": sym, "reason": "no_alloc"})   # 和 HOLD 一样没事做
            continue
        if a["ref"] in refs:
            skipped.append({"symbol": sym, "reason": "already_at_broker"})
            continue
        key = f"{today}:{sym}:{a['side'].lower()}"
        if not cfg.dry_run and not pm.store.state_claim(_SRC, key):
            skipped.append({"symbol": sym, "reason": "already_done_today"})
            continue
        try:
            if d is not None:
                a["usd"] = _alloc_usd(d)
            ref = _ref_price(pm, sym, today)
            qty, lmt = _size(a, ref, cash, positions.get(sym, 0.0),
                             min(cfg.max_order_usd, cfg.max_day_usd - used), cfg)
            q = ib.qualifyContracts(Stock(sym, "SMART", "USD"))
            contract = q[0] if q else None   # 查不到 / 有歧义时该位置是 None 或候选 list
            if contract is None or isinstance(contract, list):
                raise _Skip("not_tradable_at_ibkr", terminal=True)
            order = LimitOrder(a["side"], qty, lmt, tif="DAY", account=acct, orderRef=a["ref"],
                               outsideRth=False)
            row = {"symbol": sym, "side": a["side"], "qty": qty, "lmt": lmt, "ref_price": ref,
                   "max_usd": round(qty * lmt, 2), "exit": d is None}
            if cfg.dry_run:
                st = ib.whatIfOrder(contract, order)
                row.update(dry_run=True, commission=getattr(st, "commission", None),
                           warning=getattr(st, "warningText", "") or None)
            else:
                trade = ib.placeOrder(contract, order)
                ib.sleep(2)
                status = trade.orderStatus.status
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    msg = "; ".join(e.message for e in trade.log if e.message)
                    raise _Skip(f"rejected: {status} {msg}".strip(), terminal=True)
                row.update(status=status, order_id=order.orderId)
                if d is not None:
                    try:
                        record_execution(d["decision_id"], True, reason=f"ibkr:{status}:{order.orderId}")
                    except Exception as e:  # noqa: BLE001 — 单已下，记账失败只告警
                        log.warning(f"[ibkr] {sym} record_execution 失败（已下单）: {e}")
            used += qty * (lmt if a["side"] == "BUY" else ref)
            if a["side"] == "BUY":
                cash -= qty * lmt
            placed.append(row)
            log.info(f"📨 [ibkr] {row}")
        except _Skip as s:
            if s.terminal and not cfg.dry_run:
                if d is not None:
                    try:
                        record_execution(d["decision_id"], False, reason=f"ibkr:{s.reason}")
                    except Exception as e:  # noqa: BLE001
                        log.warning(f"[ibkr] {sym} 记 skip 失败: {e}")
            elif not cfg.dry_run:
                pm.store.state_unclaim(_SRC, key)
            skipped.append({"symbol": sym, "reason": s.reason})
        except Exception as e:  # noqa: BLE001 — 下单前失败 → 没下 → unclaim 可重试
            if not cfg.dry_run:
                pm.store.state_unclaim(_SRC, key)
            skipped.append({"symbol": sym, "reason": f"error: {e}"})
            log.warning(f"[ibkr] {sym} 失败: {e}")

    out = {"status": "ok", "date": today, "dry_run": cfg.dry_run, "placed": placed, "skipped": skipped,
           "used_usd": round(used, 2)}
    if placed:
        _notify(f"{'🧪 盈透试算' if cfg.dry_run else '📨 盈透下单'} {today}",
                [f"{r['side']} {r['symbol']} {r['qty']:g} @ ≤{r['lmt']}（≈USD {r['max_usd']}）"
                 + ("（清仓：已不在名单）" if r["exit"] else "") for r in placed]
                + [f"跳过 {s['symbol']}: {s['reason']}" for s in skipped])
    return out


# ---------------------------------------------------------------- reconcile


def reconcile(*, ib=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    ib = ib or _connect()
    acct = cfg.account
    if not acct or acct not in ib.managedAccounts():
        return {"status": "error", "reason": f"account_mismatch: 配置 {acct!r}，网关 {ib.managedAccounts()}"}

    pm = PortfolioManager()
    booked: List[Dict[str, Any]] = []
    problems: List[str] = []
    for f in _fills(ib, acct):
        c, e, cr = f.contract, f.execution, f.commissionReport
        if c.secType != "STK":
            continue   # 换汇（CASH）不在本程序口径：USD 现金对不上时邮件提醒人手记
        sym, qty, px = c.symbol, float(e.shares), float(e.price)
        key = f"ibkr-{e.execId}"
        try:
            if e.side == "BOT":
                h = pm.find_holding(sym) or {}
                res = pm.buy(sym, qty, px, currency="USD", kind=h.get("kind") or _guess_kind_from_symbol(sym),
                             unit_label="股", source=_SRC, idempotency_key=key)
            else:
                res = pm.sell(sym, qty, px, source=_SRC, idempotency_key=key)
        except ValueError as ex:
            problems.append(f"{sym} {e.side} {qty:g} 记账失败: {ex}")
            continue
        if res.get("status") != "duplicate":
            booked.append({"symbol": sym, "side": e.side, "qty": qty, "price": px, "exec_id": e.execId})
        fee = float(getattr(cr, "commission", 0) or 0)
        if math.isfinite(fee) and 0 < fee < 1e6:
            try:
                pm.record_fee(round(fee, 4), (getattr(cr, "currency", "") or "USD"), sym,
                              trade_date=e.time.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"),
                              source=_SRC, note="IBKR commission", idempotency_key=f"ibkr-fee-{e.execId}")
            except Exception as ex:  # noqa: BLE001
                problems.append(f"{sym} 佣金记账失败: {ex}")

    broker = _broker_positions(ib, acct)
    ledger = {h["symbol"]: float(h["units"]) for h in pm.holdings if float(h.get("units") or 0)}
    pos_drift = {s: {"ledger": ledger.get(s, 0.0), "broker": broker.get(s, 0.0)}
                 for s in set(broker) | set(ledger) if abs(ledger.get(s, 0.0) - broker.get(s, 0.0)) > 1e-4}
    cash_drift = round(pm.cash_amount("USD") - _broker_cash(ib, acct), 2)
    if pos_drift or problems:
        _halt(f"对账不一致：持仓 {pos_drift} / 问题 {problems}")
    elif abs(cash_drift) >= 1.0:   # 分红 / 利息 / 换汇会让现金对不上：只提醒，不停手（下单额度按券商现金算）
        _notify("💵 盈透现金和账本对不上", [f"账本 − 券商 = USD {cash_drift}（分红 / 换汇 / 入金要手记一笔）"])
    if booked:
        _notify("✅ 盈透成交", [f"{b['side']} {b['symbol']} {b['qty']:g} @ {b['price']}" for b in booked])
    return {"status": "halted" if (pos_drift or problems) else "ok", "booked": booked,
            "position_drift": pos_drift, "cash_drift_usd": cash_drift, "problems": problems}


def run(phase: str = "submit", **kw) -> Dict[str, Any]:
    return {"pick": pick, "submit": submit, "reconcile": reconcile}[phase](**kw)


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else "submit"), ensure_ascii=False, default=str))
