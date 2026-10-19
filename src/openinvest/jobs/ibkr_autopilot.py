"""盈透（IBKR）实盘 autopilot —— LLM 选标的、LLM 定买卖（委员会裁决只当参考输入）、本模块下**真单**。
opt-in，只应在独立 INVEST_HOME 开。券商走盈透 Client Portal Web API + First Party OAuth 1.0a（ibind）：
一次性在盈透 OAuth 页登记密钥 / 生成令牌后无人值守，不需要网关进程、不需要每周 IB Key。

四段（cron 按美股时段跑；日期一律北京日期，与 .committee/<date>/ 同口径）：
  pick       每周一（或 target_assets 为空）：LLM 在硬限制内选 ≤5 只美股 / 美股 ETF，整表替换本 home 的
             target_assets。LLM 输出必须过 _check_tradable（美股上市、USD、股价、流动性、非杠杆 / 反向），不过的丢掉。
  decide     美股开盘前、委员会日报之后：LLM 看账户 + 本周名单 + 当日委员会裁决（参考，不必照做），写当日订单
             memory/.ibkr/decisions/<date>.json。文件已存在就不再问 LLM（重跑不会换一套单）。
  submit     ①USD 不够下最小单而 AUD 够 → 账户内 AUD→USD 换汇（每天最多一次，钱不出账户）；②持有但已不在
             target_assets 的 → 清仓；③当日订单 → DAY 限价单（参考价 ±1%，开盘跳空超过就不成交、收盘作废）。
             委员会裁决的 execution 一概不写——执行的是 LLM 的单，不是委员会的裁决。
  reconcile  美股收盘后：拉近 7 天成交，按 execution_id 幂等记账（股票 buy/sell、换汇两腿、佣金 fee）；
             持仓和券商对不上 → 写 STOP 停手。
另有 status：只读快照（券商现金 / 持仓 / 挂单 / 当日成交 + 账本 + 名单 + 当日订单 + STOP），不下单、不记账。

硬闸（代码层，LLM 给什么都先过这里）——金额全按 USD：
  ibkr.enabled 默认关；INVEST_HOME/STOP 存在就不下单；OAuth 会话的账户里没有 ibkr.account 就什么都不做；
  只交易 target_assets 里的标的（清仓例外：卖的是券商里已有的持仓）；单笔 ≤ max_order_usd；当日累计
  （已挂未成交 + 当日已成交）≤ max_day_usd；< min_order_usd 不下（整笔清仓例外）；只挂限价单；
  卖出量 ≤ 券商持仓（现金账户本身不能融资 / 做空）；下单时盈透弹的确认问题只认白名单（_ANSWERS），其余一律不下。
ibkr.dry_run=True（默认）：whatif 试算——盈透校验合约 / 权限 / 碎股 / 佣金，不下单、不换汇、不占 claim。

幂等（ADR-016）：submit 每个 (date, symbol, side) 一把 state_claim，下单后不 unclaim；订单 cOID = 我们的 orderRef
（盈透侧对重复 cOID 拒单），已有同 ref 的挂单 / 成交也跳过。reconcile 每笔成交带 idempotency_key=ibkr-<execution_id>。
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
from openinvest.core.decision_ledger import parse_committee_file
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from openinvest.jobs.paper_autopilot import _holding_for, round_down_units
from openinvest.paths import INVEST_ROOT
from openinvest.utils.quotes import get_quote
from openinvest.utils.symbols import safe_symbol

log = logging.getLogger(__name__)

_SRC = "ibkr_autopilot"
_REF = "oi:"            # orderRef / cOID 前缀：只把带它的挂单 / 成交算进本程序的当日额度
_BAND = 0.01            # 限价 = 参考价 ±1%
_MAX_PICKS = 5
_MIN_PRICE = 5.0
_MIN_DOLLAR_VOL = 20e6  # 3 个月日均成交额（USD）下限：挡小盘 / 易操纵的票
_US_EXCH = {"NMS", "NGM", "NCM", "NYQ", "PCX", "ASE", "BTS"}
# ponytail: 名字关键词挡杠杆 / 反向 ETF，够挡 ProShares / Direxion / 2x-3x 系列；漏网的靠单笔 / 单日上限兜底
_LEVERAGED = re.compile(r"(?i)\b(\d(\.\d+)?x|-\d+x|ultra\w*|inverse|bear|short(?!-term)|leveraged|daily)\b")
_QUOTE_MAX_AGE_DAYS = 5  # 参考价最晚是 5 个自然日前的收盘（周末 + 长假）
_FX_FROM = "AUD"         # 账户入金币种：USD 不够时把它换成 USD
_FX_MIN = 50.0           # 少于这么多 AUD 不换（换汇每笔最低佣金约 USD 2）
_FX_KEEP = 5.0           # 换汇后留在 AUD 的零头（防佣金 / 汇率波动把 AUD 扣成负数）
_FX_MAX = 1000.0         # 单日换汇上限（AUD）


class _Skip(Exception):
    """terminal=True：判定已定（太小 / 没仓位 / 没钱 / 被拒），留 claim；False：什么都没下、条件可能自愈 → unclaim。"""
    def __init__(self, reason: str, terminal: bool = False):
        super().__init__(reason)
        self.reason, self.terminal = reason, terminal


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


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


def _num(v: Any) -> float:
    """Web API 的数字常是字符串（"1,234.5"）或缺失；转不了按 0。"""
    try:
        f = float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0
    return f if math.isfinite(f) else 0.0


# ---------------------------------------------------------------- broker（盈透 Web API）


def _answers(conversion: bool = False) -> dict:
    """下单时盈透弹的确认问题：只认这几个（我们自己的参考价 + 限价兜底），其余 → ibind 抛错 → 不下单。"""
    from ibind import QuestionType as Q
    a = {Q.MISSING_MARKET_DATA: True, Q.TRIGGER_AND_FILL: True, Q.MANDATORY_CAP_PRICE: True,
         Q.DISRUPTIVE_ORDERS: True, Q.PRICE_PERCENTAGE_CONSTRAINT: False, Q.ORDER_SIZE_LIMIT: False,
         Q.ORDER_VALUE_LIMIT: False, Q.TICK_SIZE_LIMIT: False, Q.CLOSE_POSITION: False,
         Q.MULTIPLE_ACCOUNTS: False, Q.CASH_QUANTITY: False, Q.CASH_QUANTITY_ORDER: False,
         Q.MARKET_ORDER_CONFIRMATION: conversion}   # 只有换汇用市价单
    return a


class _WebApi:
    """盈透 Client Portal Web API（OAuth 1.0a，ibind）的最小适配层——本模块只用这几个调用，字段映射集中在这里。
    字段名照 IBKR Web API v1 文档；首次连上先 dry_run（whatif）核对。测试用同名方法的 Fake 替换。"""

    def __init__(self, client, acct: str):
        self.c, self.acct = client, acct

    @classmethod
    def connect(cls, acct: str) -> "_WebApi":
        from ibind import IbkrClient
        # OAuth 参数全从 IBIND_OAUTH1A_* 环境变量读（运营者的 oauth.env）；cron 短跑，不起保活线程
        c = IbkrClient(account_id=acct, use_oauth=True)
        c.receive_brokerage_accounts()   # /iserver/accounts 必须先调，/iserver/* 端点才可用
        return cls(c, acct)

    def close(self) -> None:
        try:
            self.c.close()
        except Exception as e:  # noqa: BLE001
            log.warning(f"[ibkr] 关闭会话失败: {e}")

    def accounts(self) -> List[str]:
        return [str(a.get("accountId") or a.get("id") or "") for a in (self.c.portfolio_accounts().data or [])]

    def cash(self) -> Dict[str, float]:
        led = self.c.get_ledger(self.acct).data or {}
        return {k.upper(): _num(v.get("cashbalance")) for k, v in led.items()
                if isinstance(v, dict) and k.upper() != "BASE"}

    def positions(self) -> Dict[str, float]:
        self.c.invalidate_backend_portfolio_cache(self.acct)   # portfolio 端点有缓存，对账前先刷
        out: Dict[str, float] = {}
        page = 0
        while True:
            rows = self.c.positions(self.acct, page=page).data or []
            for r in rows:
                qty = _num(r.get("position"))
                if str(r.get("assetClass") or "").upper() == "STK" and qty:
                    out[str(r.get("ticker") or r.get("contractDesc") or "").split()[0]] = qty
            if len(rows) < 100:   # 每页 100 条
                return out
            page += 1

    def open_orders(self) -> List[Dict[str, Any]]:
        rows = (self.c.live_orders(account_id=self.acct).data or {}).get("orders") or []
        return [{"ref": str(r.get("order_ref") or ""), "side": str(r.get("side") or "").upper(),
                 "symbol": str(r.get("ticker") or ""), "left": _num(r.get("remainingQuantity")),
                 "lmt": _num(r.get("price")), "status": str(r.get("status") or "")}
                for r in rows if str(r.get("status") or "") not in ("Filled", "Cancelled", "Inactive")]

    def fills(self, days: int = 1) -> List[Dict[str, Any]]:
        rows = self.c.trades(days=str(days), account_id=self.acct).data or []
        out = []
        for r in rows:
            acct = r.get("account") or r.get("accountCode")
            if acct and acct != self.acct:
                continue
            ms = r.get("trade_time_r")
            out.append({"exec_id": str(r.get("execution_id") or ""), "symbol": str(r.get("symbol") or ""),
                        "side": "BOT" if str(r.get("side") or "").upper() in ("B", "BUY", "BOT") else "SLD",
                        "qty": _num(r.get("size")), "price": _num(r.get("price")),
                        "commission": _num(r.get("commission")), "ref": str(r.get("order_ref") or ""),
                        "sec_type": str(r.get("sec_type") or "STK").upper(),
                        "date": (datetime.fromtimestamp(int(ms) / 1000, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
                                 if ms else _today())})
        return out

    def conid(self, symbol: str) -> Optional[int]:
        try:
            data = self.c.stock_conid_by_symbol(symbol).data
        except Exception as e:  # noqa: BLE001 — 查不到 / 有歧义都当不可交易
            log.warning(f"[ibkr] {symbol} conid 查询失败: {e}")
            return None
        v = data.get(symbol) if isinstance(data, dict) else None
        return int(v) if isinstance(v, (int, str)) and str(v).isdigit() else None

    def _order(self, conid: int, side: str, qty: float, lmt: float, ref: str):
        from ibind import OrderRequest
        return OrderRequest(conid=conid, side=side, quantity=qty, order_type="LMT", price=lmt, acct_id=self.acct,
                            coid=ref, tif="DAY", outside_rth=False, manual_indicator=False)

    def whatif(self, conid: int, side: str, qty: float, lmt: float, ref: str) -> Dict[str, Any]:
        d = self.c.whatif_order(self._order(conid, side, qty, lmt, ref), self.acct).data or {}
        return {"commission": (d.get("amount") or {}).get("commission"), "warning": d.get("warn") or None,
                "error": d.get("error") or None}

    def place(self, conid: int, side: str, qty: float, lmt: float, ref: str) -> Dict[str, Any]:
        data = self.c.place_order(self._order(conid, side, qty, lmt, ref), _answers(), self.acct).data
        r = data[0] if isinstance(data, list) and data else (data or {})
        return {"order_id": r.get("order_id"), "status": r.get("order_status") or "Submitted"}

    def fx_pair(self, src: str, dst: str) -> Optional[Dict[str, Any]]:
        """(conid, base) of the src/dst 货币对，如 AUD.USD → base=AUD。"""
        data = self.c.currency_pairs(dst).data or {}
        for p in data.get(dst) or []:
            sym = str(p.get("symbol") or "")
            if sym in (f"{src}.{dst}", f"{dst}.{src}") and str(p.get("conid") or "").isdigit():
                return {"conid": int(p["conid"]), "base": sym.split(".")[0]}
        return None

    def convert(self, src: str, dst: str, amount_src: float, ref: str, dry_run: bool) -> Dict[str, Any]:
        """账户内换汇（钱不出账户）：卖 amount_src 的 src 换 dst。市价，isCcyConv。"""
        from ibind import OrderRequest
        pair = self.fx_pair(src, dst)
        if pair is None:
            raise _Skip(f"no_fx_pair_{src}_{dst}", terminal=True)
        side = "SELL" if pair["base"] == src else "BUY"   # 以 base 计：卖 src / 买 dst
        req = OrderRequest(conid=pair["conid"], side=side, quantity=0, order_type="MKT", acct_id=self.acct,
                           coid=ref, tif="DAY", is_ccy_conv=True, fx_qty=round(amount_src, 2),
                           manual_indicator=False)
        if dry_run:
            d = self.c.whatif_order(req, self.acct).data or {}
            return {"dry_run": True, "warning": d.get("warn") or None, "error": d.get("error") or None}
        data = self.c.place_order(req, _answers(conversion=True), self.acct).data
        r = data[0] if isinstance(data, list) and data else (data or {})
        return {"order_id": r.get("order_id"), "status": r.get("order_status") or "Submitted"}


def _connect(acct: str):
    return _WebApi.connect(acct)


def _used_today(b, fills: list) -> tuple[float, float, set]:
    """(当日已用额度, 挂着的买单占用现金, 已存在的 orderRef)。只算本程序的单（orderRef 带前缀）。"""
    used = reserved = 0.0
    refs = set()
    for o in b.open_orders():
        if not o["ref"].startswith(_REF):
            continue
        used += o["left"] * o["lmt"]
        if o["side"] == "BUY":
            reserved += o["left"] * o["lmt"]
        refs.add(o["ref"])
    for f in fills:
        if f["ref"].startswith(_REF):
            if f["sec_type"] == "STK":
                used += f["qty"] * f["price"]
            refs.add(f["ref"])
    return used, reserved, refs


# ---------------------------------------------------------------- LLM（pick / decide 共用）


def _agent(system_prompt: str):
    from openinvest.capabilities.sdk_agent import SDKAgent
    from openinvest.utils.llm import get_llm_config_safe
    api_key, base_url, model, provider = get_llm_config_safe()
    if not api_key:
        raise RuntimeError("LLM_API_KEY 缺失")
    return SDKAgent(system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url,
                    temperature=0.3, enable_tools=True, max_tool_iterations=8, provider=provider)


def _parse_json(raw: str, key: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("LLM 没输出 JSON")
    data = json.loads(m.group(0))
    if not isinstance(data.get(key), list):
        raise ValueError(f"JSON 缺 {key} 列表")
    return data


def _account_lines(pm: PortfolioManager, today: str) -> List[str]:
    held = []
    for h in pm.holdings:
        if float(h.get("units") or 0) <= 0:
            continue
        snap = get_quote(dict(h))
        cost = float(h.get("avg_cost") or 0)
        if snap and snap.price:
            pnl = f"，浮盈 {(snap.price / cost - 1) * 100:+.1f}%" if cost else ""
            px = f"{snap.price:.2f}{pnl}"
        else:
            px = "?"
        held.append(f"- {h['symbol']}: {float(h['units']):g} 股，成本 {cost:.2f}，现价 {px}")
    others = {k: v for k, v in pm.cash.items() if k != "USD" and v}
    note = f"；另有 {others}，不是 USD 不能直接下单（换汇要人手做）" if others else ""
    return [f"今天 {today}。可下单现金 USD {pm.cash_amount('USD'):.2f}{note}。",
            "当前持仓：", *(held or ["（空仓）"])]


def _store_dir(name: str):
    d = MemoryStore().root / ".ibkr" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- pick


def _check_tradable(symbol: str, info: Dict[str, Any]) -> Optional[str]:
    """None = 可以；否则返回拒绝原因。info = yfinance Ticker.info。"""
    if not re.fullmatch(r"[A-Z]{1,5}", symbol):
        # 分级股 BRK.B：盈透侧代码写作 "BRK B"，持仓 / 对账 key 对不上会被当成名单外持仓清仓 → 直接不收
        return "dotted_ticker_unsupported" if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", symbol) else "not_us_ticker"
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


_PICK_SYSTEM = """你在管理一个盈透（Interactive Brokers）实盘现金账户，钱是用户的真钱。每周一由你决定本周要持有的标的。
选中的标的之后每个交易日由你决定买卖（投资委员会的裁决只当参考）；**不在你名单里的现有持仓会被清仓**。

硬限制（代码会逐条校验，不满足的直接丢掉，不要浪费名额）：
- 最多 5 只；美股上市的普通股或 ETF，USD 计价，股价 ≥ $5，3 个月日均成交额 ≥ $2000 万
- 不要杠杆 / 反向 / 做空类 ETF
- 现金账户：不能融资、不能做空；单笔 ≤ USD {max_order}，单日 ≤ USD {max_day}；支持碎股；每单佣金最低约 USD 0.35

你可以调用工具查历史行情、多周期技术面和宏观快照。只输出一个 JSON，不要别的文字：
{{"picks": [{{"symbol": "代码", "why": "一句话理由"}}], "note": "本周整体思路，一两句"}}"""


def _background() -> List[str]:
    body = (MemoryStore().read("strategy").body or "").strip()
    return ["", "账户 / 用户背景（strategy.md 正文）：", body or "（无）"]


def _pick_prompt(pm: PortfolioManager, today: str) -> str:
    targets = ", ".join(t["symbol"] for t in pm.strategy.get("target_assets") or []) or "（无）"
    return "\n".join([*_account_lines(pm, today), f"上周名单：{targets}", *_background()])


def pick(today: Optional[str] = None, *, force: bool = False, agent=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    today = today or _today()
    pm = PortfolioManager()
    current = pm.strategy.get("target_assets") or []
    if not force and current and datetime.strptime(today, "%Y-%m-%d").weekday() != 0:
        return {"status": "skipped", "reason": "not_pick_day"}

    agent = agent or _agent(_PICK_SYSTEM.format(max_order=f"{cfg.max_order_usd:g}", max_day=f"{cfg.max_day_usd:g}"))
    data = _parse_json(agent.run(_pick_prompt(pm, today)), "picks")

    ok: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for p in data["picks"]:
        p = {"symbol": p} if isinstance(p, str) else p   # LLM 偶尔只给代码列表
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
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

    record = {"date": today, "picks": ok, "rejected": rejected, "note": data.get("note"),
              "previous": [t["symbol"] for t in current]}
    (_store_dir("picks") / f"{today}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
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


# ---------------------------------------------------------------- decide


_DECIDE_SYSTEM = """你在管理一个盈透（Interactive Brokers）实盘现金账户，钱是用户的真钱。现在是美股开盘前，由你决定今天的买卖。

- 只能交易本周名单里的标的（名单外的持仓程序会自动清仓，不用你管）
- 投资委员会（同一个模型按宏观 / 量化 / 风控 / CIO 四个角色出裁决，规则偏保守）给了今天的意见，**只是参考**，你可以不同意
- 硬限制（代码会再钳一遍）：单笔 ≤ USD {max_order}，当天合计 ≤ USD {max_day}，单笔 < USD {min_order} 不下；
  现金账户不能融资 / 做空，卖出不超过持仓
- 每单佣金最低约 USD 0.35，别为了动而动；没有把握就不下单（orders 留空）
- 订单是当日限价单（买 = 昨收 +1%，卖 = 昨收 −1%），开盘跳空超过就不成交

你可以调用工具查历史行情、多周期技术面和宏观快照。只输出一个 JSON，不要别的文字：
{{"orders": [{{"symbol": "代码", "side": "BUY 或 SELL", "usd": 金额数字（SELL 全部卖出写 "all"）, "why": "一句话理由"}}], "note": "今天整体判断，一两句"}}"""


def _latest_pick_reasons() -> Dict[str, str]:
    files = sorted(_store_dir("picks").glob("*.json"))
    if not files:
        return {}
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    return {p["symbol"]: p.get("_why") or "" for p in data.get("picks") or []}


def _committee_lines(targets: List[str], today: str) -> List[str]:
    out = []
    for s in targets:
        v = parse_committee_file(MemoryStore().root / ".committee" / today / f"{safe_symbol(s)}.md")
        if not v:
            out.append(f"- {s}：今天没有裁决")
            continue
        alloc = f"，建议 {v['alloc_currency'] or ''} {v['alloc_cny']:g}" if v.get("alloc_cny") else ""
        memo = " ".join((v.get("cio_memo") or "").split())[:400]
        out.append(f"- {s}：{v['verdict']}（置信度 {v['confidence']:.2f}{alloc}）{memo}")
    return out


def _decide_prompt(pm: PortfolioManager, targets: List[str], today: str) -> str:
    why = _latest_pick_reasons()
    return "\n".join([*_account_lines(pm, today),
                      "本周名单：", *(f"- {s}：{why.get(s, '')}" for s in targets),
                      "", "今天委员会的裁决（参考）：", *_committee_lines(targets, today), *_background()])


def _validate_orders(orders: list, targets: set) -> tuple[list, list]:
    ok: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for o in orders:
        o = o if isinstance(o, dict) else {}
        sym = str(o.get("symbol") or "").strip().upper()
        side = str(o.get("side") or "").strip().upper()
        usd = o.get("usd")
        full = side == "SELL" and str(usd).strip().lower() == "all"
        reason = None
        if sym not in targets:
            reason = "not_in_list"
        elif side not in ("BUY", "SELL"):
            reason = "bad_side"
        elif any(a["symbol"] == sym for a in ok):
            reason = "one_order_per_symbol"
        elif not full:
            try:
                usd = float(usd)
            except (TypeError, ValueError):
                usd = float("nan")
            if not math.isfinite(usd) or usd <= 0:
                reason = "bad_amount"
        if reason:
            rejected.append({"symbol": sym, "side": side, "reason": reason})
            continue
        ok.append({"symbol": sym, "side": side, "full": full, "usd": 0.0 if full else usd,
                   "why": str(o.get("why") or "")[:200]})
    return ok, rejected


def decide(today: Optional[str] = None, *, agent=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    today = today or _today()
    path = _store_dir("decisions") / f"{today}.json"
    if path.exists():   # 当天已决定：重跑不再问 LLM，免得同一天换一套单
        return {"status": "skipped", "reason": "already_decided", **json.loads(path.read_text(encoding="utf-8"))}
    pm = PortfolioManager()
    targets = [t["symbol"] for t in pm.strategy.get("target_assets") or []]
    agent = agent or _agent(_DECIDE_SYSTEM.format(max_order=f"{cfg.max_order_usd:g}", max_day=f"{cfg.max_day_usd:g}",
                                                  min_order=f"{cfg.min_order_usd:g}"))
    data = _parse_json(agent.run(_decide_prompt(pm, targets, today)), "orders")
    ok, rejected = _validate_orders(data["orders"], set(targets))
    record = {"date": today, "orders": ok, "rejected": rejected, "note": data.get("note")}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", **record}


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
    """当日要做的事：先清仓（不在名单里的持仓），再 decide 写的当日订单；卖在买前（回款当天可再买）。"""
    targets = {t["symbol"] for t in pm.strategy.get("target_assets") or []}
    out = [{"symbol": s, "side": "SELL", "full": True, "usd": 0.0, "exit": True, "why": "已不在本周名单",
            "ref": f"{_REF}exit:{today}/{s}"} for s in sorted(positions) if s not in targets]
    path = _store_dir("decisions") / f"{today}.json"
    orders = json.loads(path.read_text(encoding="utf-8")).get("orders") or [] if path.exists() else []
    for o in orders:
        if o["symbol"] not in targets:
            continue   # 只交易名单里的标的（白名单；decide 之后名单又被改过也挡得住）
        out.append({**o, "exit": False, "ref": f"{_REF}{today}/{o['symbol']}/{o['side']}"})
    return sorted(out, key=lambda a: (a["side"] == "BUY", a["symbol"]))


def _caps_error(cfg) -> Optional[str]:
    """金额闸本身不合法（NaN / inf / ≤0 / 最小单大于单笔上限）→ 一单都不下（fail closed）。"""
    caps = (cfg.max_order_usd, cfg.max_day_usd, cfg.min_order_usd)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in caps):
        return f"caps_not_finite: {caps}"
    if cfg.max_order_usd <= 0 or cfg.max_day_usd <= 0 or cfg.min_order_usd < 0 or cfg.min_order_usd > cfg.max_order_usd:
        return f"caps_invalid: {caps}"
    return None


def _maybe_convert(b, pm: PortfolioManager, cash: Dict[str, float], refs: set, today: str, cfg) -> Optional[Dict]:
    """USD 不够下最小单、而 AUD 够 → 把 AUD（留零头）换成 USD。每天最多一次（claim + cOID 双闸）。"""
    usd, src = cash.get("USD", 0.0), cash.get(_FX_FROM, 0.0)
    amount = min(src - _FX_KEEP, _FX_MAX)
    if usd >= cfg.min_order_usd or amount < _FX_MIN:
        return None
    ref = f"{_REF}fx:{today}/{_FX_FROM}USD"
    if ref in refs:
        return {"skipped": "already_at_broker"}
    key = f"{today}:fx:{_FX_FROM}USD"
    if not cfg.dry_run and not pm.store.state_claim(_SRC, key):
        return {"skipped": "already_done_today"}
    try:
        res = b.convert(_FX_FROM, "USD", amount, ref, cfg.dry_run)
    except _Skip as s:
        return {"skipped": s.reason}
    except Exception as e:  # noqa: BLE001 — 换汇被拒：留 claim（终局），当天不再试
        return {"skipped": f"rejected: {e}"}
    return {"from": _FX_FROM, "to": "USD", "amount": round(amount, 2), **res}


def submit(today: Optional[str] = None, *, broker=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    bad = _caps_error(cfg)
    if bad:
        return {"status": "error", "reason": bad}
    if _stop_file().exists():
        return {"status": "halted", "reason": _stop_file().read_text(encoding="utf-8").strip()}
    today = today or _today()
    acct = cfg.account
    b = broker or _connect(acct)
    try:
        if not acct or acct not in b.accounts():
            return {"status": "error", "reason": f"account_mismatch: 配置 {acct!r}，会话 {b.accounts()}"}
        return _submit(b, today, cfg)
    finally:
        if broker is None:
            b.close()


def _submit(b, today: str, cfg) -> Dict[str, Any]:
    pm = PortfolioManager()
    positions = b.positions()
    cash = b.cash()
    used, reserved, refs = _used_today(b, b.fills(days=1))
    fx = _maybe_convert(b, pm, cash, refs, today, cfg)
    if fx and fx.get("order_id"):
        cash = b.cash()   # 换汇后重读 USD
    usd_cash = cash.get("USD", 0.0) - reserved
    placed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for a in _actions(pm, positions, today):
        sym = a["symbol"]
        if a["ref"] in refs:
            skipped.append({"symbol": sym, "reason": "already_at_broker"})
            continue
        key = f"{today}:{sym}:{a['side'].lower()}"
        if not cfg.dry_run and not pm.store.state_claim(_SRC, key):
            skipped.append({"symbol": sym, "reason": "already_done_today"})
            continue
        sent = False   # 单一旦发出，之后任何异常都不 unclaim（ADR-016：下单后不 unclaim）
        try:
            ref = _ref_price(pm, sym, today)
            qty, lmt = _size(a, ref, usd_cash, positions.get(sym, 0.0),
                             min(cfg.max_order_usd, cfg.max_day_usd - used), cfg)
            conid = b.conid(sym)
            if conid is None:
                raise _Skip("not_tradable_at_ibkr", terminal=True)
            row = {"symbol": sym, "side": a["side"], "qty": qty, "lmt": lmt, "ref_price": ref,
                   "max_usd": round(qty * lmt, 2), "exit": a["exit"], "why": a.get("why") or ""}
            if cfg.dry_run:
                w = b.whatif(conid, a["side"], qty, lmt, a["ref"])
                if w.get("error"):
                    raise _Skip(f"whatif_error: {w['error']}", terminal=True)
                row.update(dry_run=True, commission=w.get("commission"), warning=w.get("warning"))
            else:
                sent = True
                try:
                    r = b.place(conid, a["side"], qty, lmt, a["ref"])
                except Exception as e:  # noqa: BLE001 — 被拒 / 确认问题不在白名单：单没下，终局
                    raise _Skip(f"rejected: {e}", terminal=True) from e
                row.update(status=r.get("status"), order_id=r.get("order_id"))
            used += qty * lmt   # 与 _used_today 对挂单的口径一致（按限价）
            if a["side"] == "BUY":
                usd_cash -= qty * lmt
            placed.append(row)
            log.info(f"📨 [ibkr] {row}")
        except _Skip as s:
            if not s.terminal and not cfg.dry_run:
                pm.store.state_unclaim(_SRC, key)
            skipped.append({"symbol": sym, "reason": s.reason})
        except Exception as e:  # noqa: BLE001 — 下单前失败 → 没下 → unclaim 可重试；已发出 → 留 claim
            if not cfg.dry_run and not sent:
                pm.store.state_unclaim(_SRC, key)
            skipped.append({"symbol": sym, "reason": f"{'error_after_send' if sent else 'error'}: {e}"})
            log.warning(f"[ibkr] {sym} 失败（{'已发出，保留 claim' if sent else '未发出'}）: {e}")

    out = {"status": "ok", "date": today, "dry_run": cfg.dry_run, "fx": fx, "placed": placed, "skipped": skipped,
           "used_usd": round(used, 2)}
    if placed or (fx and not fx.get("skipped")):
        _notify(f"{'🧪 盈透试算' if cfg.dry_run else '📨 盈透下单'} {today}",
                ([f"换汇 {fx['amount']} {fx['from']} → {fx['to']}"] if fx and not fx.get("skipped") else [])
                + [f"{r['side']} {r['symbol']} {r['qty']:g} @ {'≤' if r['side'] == 'BUY' else '≥'}{r['lmt']}"
                   f"（≈USD {r['max_usd']}）— {r['why']}" for r in placed]
                + [f"跳过 {s['symbol']}: {s['reason']}" for s in skipped])
    return out


# ---------------------------------------------------------------- reconcile


def _book_fx(pm: PortfolioManager, f: Dict[str, Any]) -> Optional[Dict]:
    """换汇成交 X.Y：BOT = +qty X / −qty×price Y；SLD 反过来。两腿各带幂等键。"""
    base, _, quote = f["symbol"].partition(".")
    if not quote:
        return None
    gain, lose = (base, quote) if f["side"] == "BOT" else (quote, base)
    amt_gain = f["qty"] if f["side"] == "BOT" else f["qty"] * f["price"]
    amt_lose = f["qty"] * f["price"] if f["side"] == "BOT" else f["qty"]
    key = f"ibkr-{f['exec_id']}"
    r1 = pm.withdraw_cash(lose, round(amt_lose, 2), source=_SRC, idempotency_key=f"{key}-out")
    pm.deposit_cash(gain, round(amt_gain, 2), source=_SRC, idempotency_key=f"{key}-in")
    return None if r1.get("status") == "duplicate" else {"fx": f["symbol"], "side": f["side"], "qty": f["qty"],
                                                         "price": f["price"], "exec_id": f["exec_id"]}


def reconcile(*, broker=None) -> Dict[str, Any]:
    cfg = load_config(_force_reload=True).ibkr
    if not cfg.enabled:
        return {"status": "skipped", "reason": "ibkr_disabled"}
    acct = cfg.account
    b = broker or _connect(acct)
    try:
        if not acct or acct not in b.accounts():
            return {"status": "error", "reason": f"account_mismatch: 配置 {acct!r}，会话 {b.accounts()}"}
        return _reconcile(b)
    finally:
        if broker is None:
            b.close()


def _reconcile(b) -> Dict[str, Any]:
    pm = PortfolioManager()
    booked: List[Dict[str, Any]] = []
    problems: List[str] = []
    for f in b.fills(days=7):   # 近 7 天：哪天对账没跑，下次补上（幂等键挡重复）
        sym, qty, px = f["symbol"], f["qty"], f["price"]
        key = f"ibkr-{f['exec_id']}"
        try:
            if f["sec_type"] == "CASH":
                row = _book_fx(pm, f)
                if row:
                    booked.append(row)
            elif f["sec_type"] == "STK":
                if f["side"] == "BOT":
                    h = pm.find_holding(sym) or {}
                    res = pm.buy(sym, qty, px, currency="USD", kind=h.get("kind") or _guess_kind_from_symbol(sym),
                                 unit_label="股", source=_SRC, idempotency_key=key)
                else:
                    res = pm.sell(sym, qty, px, source=_SRC, idempotency_key=key)
                if res.get("status") != "duplicate":
                    booked.append({"symbol": sym, "side": f["side"], "qty": qty, "price": px, "exec_id": f["exec_id"]})
            else:
                continue
        except ValueError as ex:
            problems.append(f"{sym} {f['side']} {qty:g} 记账失败: {ex}")
            continue
        fee = f["commission"]
        if 0 < fee < 1e6:
            try:
                pm.record_fee(round(fee, 4), "USD", sym, trade_date=f["date"], source=_SRC,
                              note="IBKR commission", idempotency_key=f"ibkr-fee-{f['exec_id']}")
            except Exception as ex:  # noqa: BLE001
                problems.append(f"{sym} 佣金记账失败: {ex}")

    broker_pos = b.positions()
    ledger = {h["symbol"]: float(h["units"]) for h in pm.holdings if float(h.get("units") or 0)}
    pos_drift = {s: {"ledger": ledger.get(s, 0.0), "broker": broker_pos.get(s, 0.0)}
                 for s in set(broker_pos) | set(ledger) if abs(ledger.get(s, 0.0) - broker_pos.get(s, 0.0)) > 1e-4}
    bcash = b.cash()
    cash_drift = {c: round(pm.cash_amount(c) - bcash.get(c, 0.0), 2) for c in set(bcash) | set(pm.cash)
                  if abs(pm.cash_amount(c) - bcash.get(c, 0.0)) >= 1.0}
    if pos_drift or problems:
        _halt(f"对账不一致：持仓 {pos_drift} / 问题 {problems}")
    elif cash_drift:   # 分红 / 利息 / 入金会让现金对不上：只提醒，不停手（下单额度按券商现金算）
        _notify("💵 盈透现金和账本对不上", [f"账本 − 券商 = {cash_drift}（分红 / 入金要手记一笔）"])
    if booked:
        _notify("✅ 盈透成交", [f"{b_.get('side')} {b_.get('symbol') or b_.get('fx')} {b_['qty']:g} @ {b_['price']}"
                               for b_ in booked])
    return {"status": "halted" if (pos_drift or problems) else "ok", "booked": booked,
            "position_drift": pos_drift, "cash_drift": cash_drift, "problems": problems}


# ---------------------------------------------------------------- status（只读）


def status(*, broker=None) -> Dict[str, Any]:
    """随时问「盈透那边现在什么情况」用：不下单、不记账、不看 ibkr.enabled。连不上也照样返回本地部分。"""
    cfg = load_config(_force_reload=True).ibkr
    today = _today()
    pm = PortfolioManager()
    path = _store_dir("decisions") / f"{today}.json"
    out: Dict[str, Any] = {
        "date": today, "enabled": cfg.enabled, "dry_run": cfg.dry_run, "account": cfg.account,
        "halted": _stop_file().read_text(encoding="utf-8").strip() if _stop_file().exists() else None,
        "targets": [t["symbol"] for t in pm.strategy.get("target_assets") or []],
        "today_orders": json.loads(path.read_text(encoding="utf-8")).get("orders") if path.exists() else None,
        "ledger": {"cash": pm.cash, "holdings": {h["symbol"]: float(h["units"]) for h in pm.holdings
                                                 if float(h.get("units") or 0)}},
    }
    try:
        b = broker or _connect(cfg.account)
    except Exception as e:  # noqa: BLE001
        out["broker"] = {"error": f"unreachable: {e}"}
        return out
    try:
        if not cfg.account or cfg.account not in b.accounts():
            out["broker"] = {"error": f"account_mismatch: 配置 {cfg.account!r}，会话 {b.accounts()}"}
            return out
        out["broker"] = {"cash": b.cash(), "positions": b.positions(), "open_orders": b.open_orders(),
                         "fills_today": b.fills(days=1)}
        return out
    finally:
        if broker is None:
            b.close()


def notify_failure(phase: str, problems: Dict[str, Any]) -> None:
    """CLI 用：某段返回 error / halted 或抛异常时发邮件——cron 只看退出码，没人盯日志。"""
    _notify(f"⚠️ 盈透 autopilot {phase} 没跑成",
            [f"{k}: {json.dumps(v, ensure_ascii=False, default=str)[:500]}" for k, v in problems.items()])


def run(phase: str = "submit", **kw) -> Dict[str, Any]:
    return {"pick": pick, "decide": decide, "submit": submit, "reconcile": reconcile, "status": status}[phase](**kw)


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else "submit"), ensure_ascii=False, default=str))
