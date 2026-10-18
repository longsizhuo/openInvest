"""日报"今日持仓归因"段（IO 壳）：行情库 → calc.attribution → markdown

2026 年 Robinhood Digests / Schwab Portfolio Insights / Google Finance 的默认交付物：
今天每个持仓涨跌多少、谁拖累了组合、和基准比如何、接下来 7 天有什么事。

数据源（行情 / 汇率 / 事件只读本地库；联网只有下面两处，都有退路）：
- ``core.portfolio_manager``——当前持仓（跳过 tracking-only）
- ``db/market_data.db``——每个持仓**自己**最近两根收盘（休市 / 不同交易所日历不同，不强行对齐
  到同一天；与基准比较的窗口也是各自最近两根，日期标在表里）、汇率；取价 / 汇率口径复用
  ``services.account_performance``（黄金克价 / 场外基金 / 汇率反向·交叉对），保证和账户业绩同口径
- ``db/events.db``——symbols 命中持仓（含 ETF→指数代理）且 created_at 最近 7 天的事件，每持仓最多 2 条
- yfinance ``Ticker.calendar``——个股未来 7 天财报 / 除息日；取不到（异常 / 超时 / 空）写"无数据源"。
  整体 15 秒硬墙钟（守护线程，超时不 join、不拖进程退出），超时的标的记 n/a
- 基准行情：经 ``account_performance._benchmark_closes``，每进程每天最多刷新一次（日报里"账户业绩"段通常已刷过，
  这里只读库）；失败用库里已有的，库里也没有就不比较。没有可归因的持仓时不碰基准

任何子步骤失败只让对应一列降级为 n/a，不阻断日报。events.db 里是**已发生**的近期事件（入库时刻
近 7 天），不是前瞻日历；前瞻部分只有上面的财报 / 除息。
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import Counter
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from openinvest.calc.attribution import attribute
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.db.market_store import MarketStore
from openinvest.services.account_performance import (
    GOLD_PROXY_KIND,
    _at,
    _benchmark_closes,
    _benchmark_symbol,
    _fx_series,
    _price_series,
)
from openinvest.services.symbol_map import is_single_stock, proxy_symbols_for
from openinvest.utils.base_currency import base_currency

log = logging.getLogger(__name__)

MAX_BAR_AGE_DAYS = 10        # 最新收盘比今天旧超过这么多天（含春节长假）→ 不归因，免得拿几周前的"涨跌"当今天
EVENT_WINDOW_DAYS = 7
EVENT_MIN_SEVERITY = "mid"   # 同 D17 资产专属事件 / 委员会 RAG 默认
EVENTS_PER_HOLDING = 2
EVENT_TITLE_MAX = 90
MIN_MOVER_PP = 0.05          # 一句话结论里"拖累 / 拉动"至少要有这么大贡献（百分点），-0.01pp 不值得点名
CALENDAR_HORIZON_DAYS = 7
CALENDAR_TIMEOUT_SEC = 15
_SEV_RANK = {"low": 1, "mid": 2, "high": 3}
_CAL_KEYS = (("Earnings Date", "财报"), ("Ex-Dividend Date", "除息"), ("Dividend Date", "派息"))
_SKIP_WHY = {
    "no_history": "行情库里不足 2 根收盘价", "missing_price": "价格无效", "missing_fx": "缺汇率",
    "no_units": "持仓量无效",
}


# ---------- 1. 取数 ----------

def _last_two(series: Any, today: str) -> Optional[tuple]:
    """序列里 ≤ today 的最后两根 → (prev_date, prev, date, last)；不足两根 → None。"""
    if series is None or series.empty:
        return None
    s = series[series.index <= today]
    if len(s) < 2:
        return None
    return s.index[-2], float(s.iloc[-2]), s.index[-1], float(s.iloc[-1])


def _yf_calendar(symbol: str) -> dict:
    """yfinance Ticker.calendar（联网）；调用方负责超时和异常。"""
    import yfinance as yf
    return dict(yf.Ticker(symbol).calendar or {})


def _calendar_items(cal: dict, today: str, horizon: str) -> Optional[List[Dict[str, str]]]:
    """日历 dict → 窗口 [today, horizon] 内的 [{kind, date}]；dict 里没有任何已知 key → None（无数据源）。"""
    if not any(k in cal for k, _ in _CAL_KEYS):
        return None
    items = []
    for key, kind in _CAL_KEYS:
        v = cal.get(key)
        for d in (v if isinstance(v, (list, tuple)) else [v]):
            ds = str(d or "")[:10]
            if today <= ds <= horizon:
                items.append({"kind": kind, "date": ds})
    return sorted(items, key=lambda i: (i["date"], i["kind"]))


def _fetch_calendars(symbols: List[str], today: str,
                     calendar_fn: Callable[[str], dict]) -> Dict[str, Dict[str, Any]]:
    """{symbol: {status: ok|none|no_source|n/a, items}}：ok=窗口内有日期，none=有日历但窗口内没有，
    no_source=取不到 / 空，n/a=超时没等到。整体 CALENDAR_TIMEOUT_SEC 硬墙钟：工作线程是**守护线程**，
    超时直接放弃它（不 join；ThreadPoolExecutor 的线程解释器退出时会被 join，卡住的 yfinance 请求会拖住 cron 进程退出）。"""
    horizon = (date.fromisoformat(today) + timedelta(days=CALENDAR_HORIZON_DAYS)).isoformat()
    todo: "queue.Queue[str]" = queue.Queue()
    for s in symbols:
        todo.put(s)
    raw: Dict[str, dict] = {}

    def work() -> None:
        while True:
            try:
                sym = todo.get_nowait()
            except queue.Empty:
                return
            try:
                raw[sym] = calendar_fn(sym) or {}
            except Exception as e:  # noqa: BLE001  单标的失败 = 该标的无数据源
                log.warning("holdings_digest: %s 日历取不到: %s", sym, e)
                raw[sym] = {}

    # ponytail: 最多 4 个并发工作线程，持仓个股数上百才需要换成真线程池
    threads = [threading.Thread(target=work, daemon=True) for _ in range(min(4, len(symbols)))]
    for t in threads:
        t.start()
    deadline = time.monotonic() + CALENDAR_TIMEOUT_SEC
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    got = dict(raw)   # 快照：超时后迟到的线程再写 raw 也不影响已返回的结果
    out: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        if sym not in got:
            out[sym] = {"status": "n/a", "items": []}
            continue
        items = _calendar_items(got[sym], today, horizon)
        out[sym] = ({"status": "no_source", "items": []} if items is None
                    else {"status": "ok" if items else "none", "items": items})
    return out


def _recent_events(symbols: List[str], tracks_by_sym: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """{symbol: [{title, source, date}]}：每持仓 severity 降序、同级新→旧，相似转述去重，最多 2 条。"""
    from openinvest.db.event_store import EventStore
    from openinvest.services.embeddings import DEFAULT_DIM

    store = EventStore(embedding_dim=DEFAULT_DIM)
    try:
        out: Dict[str, List[Dict[str, str]]] = {}
        for sym in symbols:
            evs = store.recall(sym, time_window_days=EVENT_WINDOW_DAYS, min_severity=EVENT_MIN_SEVERITY,
                               top_k=20, aliases=sorted(proxy_symbols_for(sym, tracks=tracks_by_sym.get(sym))))
            picked: List[Dict[str, str]] = []
            # recall 已是新→旧；sorted 稳定，按 severity 降序后同级仍新→旧
            for e in sorted(evs, key=lambda e: -_SEV_RANK.get(e.get("severity"), 0)):
                claim = str(e.get("one_line_claim") or "").strip()
                if not claim or any(SequenceMatcher(None, p["title"].lower(), claim.lower()).ratio() >= 0.6
                                    for p in picked):
                    continue
                srcs = e.get("sources") or []
                picked.append({"title": claim, "source": str((srcs[0] or {}).get("src_name") or "") if srcs else "",
                               "date": str(e.get("ts") or "")[:10]})
                if len(picked) >= EVENTS_PER_HOLDING:
                    break
            if picked:
                out[sym] = picked
        return out
    finally:
        store.conn.close()


# ---------- 2. 主入口 ----------

def build_holdings_digest(pm: Optional[PortfolioManager] = None, *, today: Optional[str] = None,
                          base: Optional[str] = None,
                          calendar_fn: Optional[Callable[[str], dict]] = None) -> Dict[str, Any]:
    """日报"今日持仓归因"的数据块。status: ok | no_holdings | insufficient_data（没有任何持仓有 ≥2 根收盘）。

    events / calendar 子步骤失败 → 对应字段为 None（渲染成 n/a），不影响归因本身。
    """
    pm = pm or PortfolioManager()
    base = (base or base_currency()).upper()
    today = today or date.today().isoformat()
    holdings = [dict(h) for h in pm.holdings
                if not h.get("is_tracking_only") and float(h.get("units") or 0) > 0]
    if not holdings:
        return {"status": "no_holdings", "base_currency": base, "today": today}

    try:
        targets = {str(a.get("symbol")): a for a in pm.strategy.get("target_assets", [])}
    except Exception:  # noqa: BLE001  target_assets 只用来补 display_name / tracks
        targets = {}
    store = MarketStore()
    fx_cache: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    pre_skipped: List[Dict[str, str]] = []
    price_basis: Dict[str, str] = {}
    # 价格口径同账户业绩（raw | adjusted，见 services.account_performance 模块 docstring）：只看最近两根 bar，
    # 所以「所需范围」= 最近 MAX_BAR_AGE_DAYS+5 天；再早的行缺 close_raw 不影响。
    since = (date.fromisoformat(today) - timedelta(days=MAX_BAR_AGE_DAYS + 5)).isoformat()
    for h in holdings:
        sym = str(h.get("symbol"))
        ccy = "CNY" if str(h.get("proxy_kind") or "") == GOLD_PROXY_KIND else str(h.get("cost_currency") or "CNY").upper()
        series, basis = _price_series(store, sym, h, since=since)
        two = _last_two(series, today)
        if two is None:
            pre_skipped.append({"symbol": sym, "reason": "no_history"})
            continue
        d0, p0, d1, p1 = two
        if (date.fromisoformat(today) - date.fromisoformat(d1)).days > MAX_BAR_AGE_DAYS:
            pre_skipped.append({"symbol": sym, "reason": f"stale:{d1}"})
            continue
        price_basis[sym] = basis
        if ccy not in fx_cache:
            fx_cache[ccy] = _fx_series(store, ccy, base)
        fx0, fx1, fx_stale = 1.0, 1.0, False
        if ccy != base:
            fx0 = _at(fx_cache[ccy], d0)[0]
            fx1, filled = _at(fx_cache[ccy], d1)
            # d1 当天没有汇率 bar（前值填充）且和 d0 同值 = 汇率行情没更新；此时"汇率影响 0.00%"是缺数据，不是真实平盘
            fx_stale = bool(filled and fx1 is not None and fx1 == fx0)
        rows.append({"symbol": sym, "name": h.get("display_name") or targets.get(sym, {}).get("display_name") or sym,
                     "units": float(h["units"]), "currency": ccy, "prev_close": p0, "close": p1,
                     "fx_prev": fx0, "fx": fx1, "fx_stale": fx_stale, "prev_date": d0, "date": d1})

    out: Dict[str, Any] = {"base_currency": base, "today": today}
    benchmark = _benchmark_window(store, today) if rows else None   # 没有可归因的持仓就不去碰基准（网络）
    attr = attribute(rows, benchmark)
    attr["skipped"] = pre_skipped + attr["skipped"]
    if attr["portfolio"] is None:
        return {**out, "status": "insufficient_data", "attribution": attr}

    dates = Counter(r["date"] for r in attr["holdings"])
    as_of = max(dates, key=lambda d: (dates[d], d))     # 多数持仓的最新收盘日（并列取较新）
    prevs = Counter(r["prev_date"] for r in attr["holdings"] if r["date"] == as_of)
    out.update(status="ok", attribution=attr, as_of=as_of,
               prev_as_of=max(prevs, key=lambda d: (prevs[d], d)), benchmark=benchmark,
               price_basis=price_basis)

    symbols = [h["symbol"] for h in holdings]
    tracks = {s: a.get("tracks") for s, a in targets.items()}
    try:
        out["events"] = _recent_events(symbols, tracks)
    except Exception as e:  # noqa: BLE001  事件库读不到 → 该列 n/a
        log.warning("holdings_digest: 事件读取失败 graceful: %s", e)
        out["events"] = None
    try:
        stocks = [s for s in symbols if is_single_stock(s, tracks=tracks.get(s))]
        out["calendar"] = _fetch_calendars(stocks, today, calendar_fn or _yf_calendar)
    except Exception as e:  # noqa: BLE001
        log.warning("holdings_digest: 日历读取失败 graceful: %s", e)
        out["calendar"] = None
    return out


def _benchmark_window(store: MarketStore, today: str) -> Optional[Dict[str, Any]]:
    """配置基准最近两根收盘 → {symbol, prev_close, close, prev_date, date}；没配 / 行情不足 / 失败 → None。"""
    try:
        sym = _benchmark_symbol()
        if not sym:
            return None
        closes = pd.Series(_benchmark_closes(store, sym, today), dtype=float).sort_index()
        two = _last_two(closes, today)
    except Exception as e:  # noqa: BLE001
        log.warning("holdings_digest: 基准读取失败 graceful: %s", e)
        return None
    if two is None or (date.fromisoformat(today) - date.fromisoformat(two[2])).days > MAX_BAR_AGE_DAYS:
        return None   # 不足两根 / 基准行情停更：不和几周前的涨跌比
    d0, p0, d1, p1 = two
    return {"symbol": sym, "prev_close": p0, "close": p1, "prev_date": d0, "date": d1}


# ---------- 3. 渲染 ----------

def _esc(text: Any, render_target: str) -> str:
    """不可信文本（新闻 claim / 来源 / 持仓名）进 markdown 前：email 里"像标签的 <"变文字，
    两个变体都把 \\ [ ] ` 转义（chat 平台会把 [x](url) 渲染成可点链接），表格里的 | 换成 /。"""
    s = str(text)
    if render_target == "email":
        s = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", s)
    return re.sub(r"[\\\[\]`]", r"\\\g<0>", s).replace("|", "/")


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _pp(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.2f}pp"


def _md(d: str) -> str:
    return d[5:] if len(d) == 10 else d


def _skip_why(reason: str) -> str:
    return f"行情已停在 {reason[6:]}，太旧" if reason.startswith("stale:") else _SKIP_WHY.get(reason, reason)


def _headline(attr: Dict[str, Any], bench_sym: str, label: Dict[str, str]) -> str:
    """一行人话结论，确定性生成：组合涨跌 → 对比基准 → 主要拖累 / 拉动。"""
    port = attr["portfolio"]["return_pct"]
    bench, ex = attr["benchmark_return_pct"], attr["excess_pct"]
    parts = [f"今天持仓合计 {_pct(port)}"]
    if bench is not None:
        verdict = "与基准基本持平" if abs(ex) < 0.005 else f"{'跑赢' if ex > 0 else '跑输'} {abs(ex):.2f} 个百分点"
        parts.append(f"基准 {bench_sym} {_pct(bench)}（{verdict}）")
    by_sym = {r["symbol"]: r for r in attr["holdings"]}

    def mover(syms: List[str]) -> Optional[Dict[str, Any]]:
        r = by_sym.get(syms[0]) if syms else None
        return r if r and abs(r["contribution_pct"]) >= MIN_MOVER_PP else None

    down, up = mover(attr["top_detractors"]), mover(attr["top_contributors"])
    if down and (port < 0 or (ex is not None and ex < 0) or not up):
        parts.append(f"主要被 {label[down['symbol']]} 拖累（{_pp(down['contribution_pct'])}）"
                     + (f"，{label[up['symbol']]} 抵消了 {_pp(up['contribution_pct'])}" if up else ""))
    elif up:
        parts.append(f"主要靠 {label[up['symbol']]} 拉动（{_pp(up['contribution_pct'])}）"
                     + (f"，{label[down['symbol']]} 拖累 {_pp(down['contribution_pct'])}" if down else ""))
    fx = attr["portfolio"]["fx_contribution_pct"]
    if abs(fx) >= 0.01:
        parts.append(f"其中汇率影响 {_pct(fx)}")
    return "，".join(parts) + "。"


def render_digest_md(d: Dict[str, Any], render_target: str = "email") -> str:
    """日报"今日持仓归因"段（含 ## 标题）。email = markdown 表格；chat = 逐行列表（Discord/微信/QQ
    不渲染表格）。无持仓 → ""；有持仓但行情不足 → 一行说明。"""
    if d.get("status") == "no_holdings":
        return ""
    if d.get("status") != "ok":
        why = "；".join(f"{_esc(s['symbol'], render_target)}（{_skip_why(s['reason'])}）"
                       for s in d.get("attribution", {}).get("skipped", []))
        return "## 今日持仓归因\n- 暂无法归因：" + (why or "没有可用的持仓行情")
    chat = render_target == "chat"
    e = lambda x: _esc(x, render_target)  # noqa: E731
    attr, base = d["attribution"], d["base_currency"]
    rows, port = attr["holdings"], attr["portfolio"]
    label = {r["symbol"]: e(r.get("name") or r["symbol"]) for r in rows}
    nm = lambda sym: label.get(sym) or e(sym)  # noqa: E731  跳过归因的持仓没有 label
    bench = d.get("benchmark")
    bench_sym = e(bench["symbol"]) if bench else ""
    as_of = d["as_of"]

    out = [f"## 今日持仓归因（{_md(d['prev_as_of'])} → {_md(as_of)} 收盘，{base} 计）", "",
           f"**一句话**: {_headline(attr, bench_sym, label)}", ""]
    if (date.fromisoformat(d["today"]) - date.fromisoformat(as_of)).days >= 2:   # 周末 / 长假：标出，别让人以为是今天的
        out += [f"_最近收盘是 {_md(as_of)}（今天 {_md(d['today'])} 休市或尚无新收盘价），用最近两个交易日。_", ""]

    def name(r: Dict[str, Any]) -> str:   # 收盘日和多数持仓不同的，标出自己的日期
        return label[r["symbol"]] + (f"（{_md(r['date'])}）" if r["date"] != as_of else "")

    def fx_cell(r: Dict[str, Any]) -> str:
        if r.get("fx_stale"):
            return "n/a"
        return "—" if r["currency"] == base else _pct(r["fx_contribution_pct"])

    if chat:
        for r in rows:
            out.append(f"- **{name(r)}**｜权重 {r['weight_pct']:.1f}%｜今日 {_pct(r['day_return_pct'])}"
                       f"｜贡献 {_pp(r['contribution_pct'])}｜汇率 {fx_cell(r)}｜对基准 {_pp(r['excess_vs_benchmark_pct'])}")
        out.append(f"- **持仓合计（不含现金）**｜今日 {_pct(port['return_pct'])}｜汇率 {_pct(port['fx_contribution_pct'])}"
                   f"｜对基准 {_pp(attr['excess_pct'])}")
        if bench:
            out.append(f"- **基准 {bench_sym}**｜{_pct(attr['benchmark_return_pct'])}"
                       + (f"（{_md(bench['date'])}）" if bench["date"] != as_of else ""))
    else:
        out += ["| 持仓 | 权重 | 今日涨跌 | 对组合贡献 | 汇率影响 | 对比基准 |", "|:--|--:|--:|--:|--:|--:|"]
        for r in rows:
            out.append(f"| {name(r)} | {r['weight_pct']:.1f}% | {_pct(r['day_return_pct'])} | "
                       f"{_pp(r['contribution_pct'])} | {fx_cell(r)} | {_pp(r['excess_vs_benchmark_pct'])} |")
        out.append(f"| **持仓合计（不含现金）** | 100% | {_pct(port['return_pct'])} | {_pp(port['return_pct'])} | "
                   f"{_pct(port['fx_contribution_pct'])} | {_pp(attr['excess_pct'])} |")
        if bench:
            out.append(f"| 基准 {bench_sym}"
                       + (f"（{_md(bench['date'])}）" if bench["date"] != as_of else "")
                       + f" | | {_pct(attr['benchmark_return_pct'])} | | | |")
    out.append("")
    notes = []
    if not bench:
        notes.append("基准：未配置或行情库里不足 2 根收盘价，不比较")
    for s in attr["skipped"]:
        notes.append(f"{e(s['symbol'])} 未计入归因（{_skip_why(s['reason'])}）")
    stale_fx = [label[r["symbol"]] for r in rows if r.get("fx_stale")]
    if stale_fx:
        notes.append(f"{'、'.join(stale_fx)} 的汇率行情今天没更新，汇率影响未计入（按 0，不是真实值）")
    if notes:
        out += ["_" + "；".join(notes) + "_", ""]

    # 近 7 天相关事件
    out.append(f"**近 {EVENT_WINDOW_DAYS} 天相关事件**（已入库、severity≥{EVENT_MIN_SEVERITY}，每个持仓最多 {EVENTS_PER_HOLDING} 条；"
               "是已发生的新闻，不是前瞻日历）:")
    out.append("")
    events = d.get("events")
    if events is None:
        out.append("- n/a（事件库读取失败）")
    elif not events:
        out.append("- 持仓无已入库事件")
    else:
        for sym, evs in events.items():
            for ev in evs:
                title = ev["title"] if len(ev["title"]) <= EVENT_TITLE_MAX else ev["title"][:EVENT_TITLE_MAX].rstrip() + "…"
                out.append(f"- **{nm(sym)}**：{e(title)}"
                           f"（{e(ev['source']) or '来源未知'}{' · ' + _md(ev['date']) if ev['date'] else ''}）")
    out.append("")

    # 未来 7 天日历
    out.append(f"**未来 {CALENDAR_HORIZON_DAYS} 天日历**（个股财报 / 除息，yfinance）:")
    out.append("")
    cal = d.get("calendar")
    if cal is None:
        out.append("- n/a（日历读取失败）")
    elif not cal:
        out.append("- 持仓里没有个股（ETF / 商品 / 场外基金无财报日历）")
    else:
        for sym, c in cal.items():
            if c["items"]:
                out.append(f"- **{nm(sym)}**：" + "、".join(f"{i['kind']} {_md(i['date'])}" for i in c["items"]))
        gaps = [nm(s) for s, c in cal.items() if c["status"] in ("no_source", "n/a")]
        if gaps:
            out.append("- 无数据源：" + "、".join(gaps))
        if not any(c["items"] for c in cal.values()) and not gaps:
            out.append(f"- 持仓个股未来 {CALENDAR_HORIZON_DAYS} 天内没有已知财报 / 除息日")
    return "\n".join(out)


__all__ = ["build_holdings_digest", "render_digest_md"]
