"""services/holdings_digest.py —— 日报"今日持仓归因"段。临时 memory + 假行情库 + 临时 events.db，不联网。

守的契约：
- 每持仓用自己最近两根收盘；外币持仓按行情库汇率折算并拆出汇率影响；组合不含现金
- 休市日 / 长假用最近两根并标注日期；行情太旧或不足 2 根 → 跳过并说明，不阻断
- 基准同口径对比；没有可归因持仓时不去碰基准（网络）
- 事件：symbols 命中持仓（含 ETF→指数代理）、近 7 天、severity≥mid、每持仓最多 2 条、转述去重
- 日历：只查个股；窗口内列出，取不到写"无数据源"，超时 n/a；任何子步骤失败降级，不抛
- email = markdown 表格（过 notifier 白名单后是 <table>）；chat = 列表；不可信文本转义
- assemble_full_report 新 kwarg 默认空 = 旧行为；entry 失败不阻断日报
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import MagicMock

import pandas as pd
import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.jobs.daily_report_builder import assemble_full_report
from openinvest.services import holdings_digest as hd

D8, D9 = "2026-01-08", "2026-01-09"          # 周四、周五
BENCH = "BENCH"


def _df(closes: Dict[str, float]) -> pd.DataFrame:
    df = pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))
    df.index.name = "Date"
    return df


class _FakeStore:
    def __init__(self, table: Dict[str, Dict[str, float]]):
        self.table = table

    def get_history_df(self, symbol, days=730):
        return _df(self.table[symbol]) if symbol in self.table else pd.DataFrame()


def _pm(tmp_path, cash, holdings, targets=None) -> PortfolioManager:
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": targets or []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": cash, "holdings": holdings}, "")
    return PortfolioManager(store)


# 基线组合（base=CNY）：
#   AAA   CNY 100 股 10.0 → 9.5                      V0 1000    V1 950
#   NDQ.AX AUD 100 股 50 → 51，AUDCNY 4.00 → 4.04    V0 20000   V1 20604（本币 +2%，基准币种 +3.02%，汇率 +1.02%）
#   GC=F  黄金 10 克，GC=F 3110.35 USD/oz 平盘 × USDCNY 7.0 = 700 CNY/g   V0 = V1 = 7000
# 合计 28000 → 28554 = +1.9786%；贡献 NDQ +2.1571pp、AAA −0.1786pp、黄金 0；组合汇率 20000/28000×1.02 = +0.7286%
TABLE = {
    "AAA": {D8: 10.0, D9: 9.5},
    "NDQ.AX": {D8: 50.0, D9: 51.0},
    "AUDCNY=X": {D8: 4.0, D9: 4.04},
    "GC=F": {D8: 3110.35, D9: 3110.35},
    "USDCNY=X": {D8: 7.0, D9: 7.0},
}
HOLDINGS = [
    {"symbol": "AAA", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY", "display_name": "阿尔法"},
    {"symbol": "NDQ.AX", "units": 100.0, "avg_cost": 50.0, "cost_currency": "AUD"},
    {"symbol": "GC=F", "units": 10.0, "avg_cost": 650.0, "cost_currency": "CNY",
     "proxy_kind": "gold_cny_per_gram", "yfinance_proxy": "GC=F"},
]
TARGETS = [{"symbol": "NDQ.AX", "display_name": "纳指ETF"}]


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """行情库 / 基准 / 事件库全部指到测试自己的东西；日历默认空（需要的测试显式传 calendar_fn）。"""
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(TABLE))
    monkeypatch.setattr(hd, "_benchmark_symbol", lambda: BENCH)
    monkeypatch.setattr(hd, "_benchmark_closes", lambda store, sym, today: {D8: 100.0, D9: 101.0})
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.db"))


def _build(tmp_path, *, today=D9, holdings=None, calendar_fn=lambda s: {}, **kw):
    pm = _pm(tmp_path, {"CNY": 100000.0}, holdings if holdings is not None else HOLDINGS, TARGETS)
    return hd.build_holdings_digest(pm, today=today, base="CNY", calendar_fn=calendar_fn, **kw)


def _row(d, symbol):
    return next(r for r in d["attribution"]["holdings"] if r["symbol"] == symbol)


def _add_event(claim, syms, *, sev="mid", src="Reuters", age_days=1, url=None):
    from openinvest.db.event_store import EventStore
    store = EventStore(embedding_dim=4)
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(timespec="seconds")
    _, eid = store.upsert_event({"one_line_claim": claim, "event_type": "other", "stance": "risk", "severity": sev,
                                 "ts": ts, "affected_symbols": syms})
    store.conn.execute("UPDATE events SET created_at = ? WHERE event_id = ?", (ts, eid))
    store.conn.commit()
    if src:
        store.add_source(eid, src_name=src, url=url or f"https://example.com/{eid}", title=claim)
    store.conn.close()


# ============ 数字 ============

def test_digest_numbers_hand_computed(tmp_path):
    d = _build(tmp_path)
    assert d["status"] == "ok" and d["as_of"] == D9 and d["prev_as_of"] == D8 and d["base_currency"] == "CNY"
    a = d["attribution"]
    assert a["portfolio"]["return_pct"] == pytest.approx(554 / 28000 * 100, abs=1e-3)       # +1.9786%
    ndq, aaa, gold = _row(d, "NDQ.AX"), _row(d, "AAA"), _row(d, "GC=F")
    assert ndq["local_return_pct"] == pytest.approx(2.0) and ndq["day_return_pct"] == pytest.approx(3.02)
    assert ndq["fx_contribution_pct"] == pytest.approx(1.02) and ndq["currency"] == "AUD"
    assert ndq["contribution_pct"] == pytest.approx(604 / 28000 * 100, abs=1e-3)
    assert aaa["contribution_pct"] == pytest.approx(-50 / 28000 * 100, abs=1e-3)
    assert gold["day_return_pct"] == 0 and gold["currency"] == "CNY"                         # 黄金克价 = GC=F/31.1035×USDCNY
    assert a["portfolio"]["fx_contribution_pct"] == pytest.approx(20000 / 28000 * 1.02, abs=1e-3)
    assert a["portfolio"]["value_prev_base"] == pytest.approx(28000)                         # 不含现金
    assert a["top_contributors"] == ["NDQ.AX"] and a["top_detractors"] == ["AAA"]
    assert (_row(d, "AAA")["name"], ndq["name"], gold["name"]) == ("阿尔法", "纳指ETF", "GC=F")   # display_name → target → symbol
    # 基准 100 → 101 = +1%
    assert a["benchmark_return_pct"] == pytest.approx(1.0)
    assert a["excess_pct"] == pytest.approx(554 / 28000 * 100 - 1.0, abs=1e-3)
    assert d["benchmark"]["symbol"] == BENCH and d["benchmark"]["date"] == D9


def test_headline_lead_depends_on_vs_benchmark(tmp_path, monkeypatch):
    # 跑赢基准 → 先说谁拉动
    md = hd.render_digest_md(_build(tmp_path), "email")
    assert "今天持仓合计 +1.98%，基准 BENCH +1.00%（跑赢 0.98 个百分点），主要靠 纳指ETF 拉动（+2.16pp），阿尔法 拖累 -0.18pp，其中汇率影响 +0.73%。" in md
    # 基准 +2.5% → 虽然涨了也是跑输，按 spec 说主要被谁拖累
    monkeypatch.setattr(hd, "_benchmark_closes", lambda store, sym, today: {D8: 100.0, D9: 102.5})
    md = hd.render_digest_md(_build(tmp_path), "email")
    assert "基准 BENCH +2.50%（跑输 0.52 个百分点），主要被 阿尔法 拖累（-0.18pp），纳指ETF 抵消了 +2.16pp" in md


def test_headline_skips_negligible_movers(tmp_path, monkeypatch):
    """AAA 只跌 0.02%（贡献 ≈ -0.0007pp）：不值得说"拖累"；NDQ 仍点名。"""
    table = {**TABLE, "AAA": {D8: 10.0, D9: 9.998}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    md = hd.render_digest_md(_build(tmp_path), "chat")
    assert "主要靠 纳指ETF 拉动（+2.16pp）" in md and "拖累" not in md.split("一句话")[1].split("\n")[0]


def test_long_event_title_gets_ellipsis(tmp_path):
    _add_event("Alpha " + "very long headline " * 10, ["AAA"], sev="high")
    md = hd.render_digest_md(_build(tmp_path), "email")
    line = next(ln for ln in md.splitlines() if ln.startswith("- **阿尔法**"))
    assert "…（Reuters" in line and len(line) < 200


def test_stale_fx_series_is_flagged_not_shown_as_flat(tmp_path, monkeypatch):
    """评审 #1：AUDCNY 行情停在 D8（D9 没有 bar、前值填充）→ 汇率影响不是真实 0.00%，要标 n/a + 备注。"""
    table = {**TABLE, "AUDCNY=X": {"2026-01-07": 4.0, D8: 4.0}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    d = _build(tmp_path)
    ndq, aaa = _row(d, "NDQ.AX"), _row(d, "AAA")
    assert ndq["fx_stale"] is True and ndq["fx_contribution_pct"] == 0        # 数字按 0 算
    assert aaa["fx_stale"] is False and _row(d, "GC=F")["fx_stale"] is False   # 本币持仓不受影响
    email, chat = hd.render_digest_md(d, "email"), hd.render_digest_md(d, "chat")
    assert "| 纳指ETF | 71.4% | +2.00% | +1.43pp | n/a |" in email
    assert "汇率 n/a" in chat
    for md in (email, chat):
        assert "纳指ETF 的汇率行情今天没更新，汇率影响未计入（按 0，不是真实值）" in md
    # 汇率有当天 bar（即使数值没变）= 真实平盘，不标
    table = {**TABLE, "AUDCNY=X": {D8: 4.0, D9: 4.0}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    d = _build(tmp_path)
    assert _row(d, "NDQ.AX")["fx_stale"] is False
    assert "汇率行情今天没更新" not in hd.render_digest_md(d, "email")


def test_no_benchmark_still_renders_without_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "_benchmark_symbol", lambda: "")
    d = _build(tmp_path)
    assert d["benchmark"] is None and d["attribution"]["excess_pct"] is None
    md = hd.render_digest_md(d, "email")
    assert "基准：未配置或行情库里不足 2 根收盘价，不比较" in md and "跑赢" not in md and "n/a" in md


def test_stale_benchmark_is_not_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "_benchmark_closes", lambda store, sym, today: {"2025-12-01": 90.0, "2025-12-02": 91.0})
    d = _build(tmp_path)
    assert d["benchmark"] is None and d["attribution"]["excess_pct"] is None and d["status"] == "ok"


def test_benchmark_fetch_failure_degrades(tmp_path, monkeypatch):
    def boom(store, sym, today):
        raise RuntimeError("network down")
    monkeypatch.setattr(hd, "_benchmark_closes", boom)
    d = _build(tmp_path)
    assert d["status"] == "ok" and d["benchmark"] is None


# ============ 休市 / 行情缺失 ============

def test_closed_market_uses_last_two_bars_and_annotates_dates(tmp_path, monkeypatch):
    """周日跑：Fri/Thu 两根；港股那类周四才有最新一根的持仓，标出自己的日期。"""
    table = {**TABLE, "HK1": {"2026-01-07": 40.0, D8: 42.0}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    hold = HOLDINGS + [{"symbol": "HK1", "units": 100.0, "avg_cost": 40.0, "cost_currency": "CNY"}]
    d = _build(tmp_path, today="2026-01-11", holdings=hold)
    assert d["as_of"] == D9 and d["prev_as_of"] == D8                      # 多数持仓（3/4）的最近两根
    hk = _row(d, "HK1")
    assert hk["date"] == D8 and hk["prev_date"] == "2026-01-07" and hk["day_return_pct"] == pytest.approx(5.0)
    md = hd.render_digest_md(d, "email")
    assert "01-08 → 01-09 收盘" in md
    assert "最近收盘是 01-09（今天 01-11 休市或尚无新收盘价）" in md
    assert "HK1（01-08）" in md and "阿尔法（01-09）" not in md             # 只标和多数不同的


def test_stale_and_short_history_are_skipped_with_reason(tmp_path, monkeypatch):
    table = {**TABLE, "OLD": {"2025-11-03": 5.0, "2025-11-04": 6.0}, "ONE": {D9: 3.0}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    extra = [{"symbol": s, "units": 10.0, "avg_cost": 1.0, "cost_currency": "CNY"} for s in ("OLD", "ONE", "NODATA")]
    d = _build(tmp_path, holdings=HOLDINGS + extra)
    skipped = {s["symbol"]: s["reason"] for s in d["attribution"]["skipped"]}
    assert skipped == {"OLD": "stale:2025-11-04", "ONE": "no_history", "NODATA": "no_history"}
    assert d["attribution"]["portfolio"]["value_prev_base"] == pytest.approx(28000)   # 跳过的不进分母
    md = hd.render_digest_md(d, "email")
    assert "OLD 未计入归因（行情已停在 2025-11-04，太旧）" in md and "ONE 未计入归因（行情库里不足 2 根收盘价）" in md


def test_missing_fx_is_skipped_not_zeroed(tmp_path, monkeypatch):
    table = {k: v for k, v in TABLE.items() if k != "AUDCNY=X"}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore(table))
    d = _build(tmp_path)
    assert d["attribution"]["skipped"] == [{"symbol": "NDQ.AX", "reason": "missing_fx"}]
    assert {r["symbol"] for r in d["attribution"]["holdings"]} == {"AAA", "GC=F"}
    assert "NDQ.AX 未计入归因（缺汇率）" in hd.render_digest_md(d, "chat")


def test_no_usable_prices_is_insufficient_and_never_touches_benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore({}))
    called = []
    monkeypatch.setattr(hd, "_benchmark_closes", lambda *a: called.append(a) or {})
    d = _build(tmp_path)
    assert d["status"] == "insufficient_data" and called == []            # 没东西可归因 → 不去刷基准（联网）
    md = hd.render_digest_md(d, "email")
    assert "暂无法归因：" in md and "AAA（行情库里不足 2 根收盘价）" in md and "GC=F（行情库里不足 2 根收盘价）" in md


def test_all_holdings_stale_says_stale_not_missing(tmp_path, monkeypatch):
    """评审 #2：全部因数据过旧被跳过时，别说成"不足 2 根"。"""
    old = {"2025-11-03": 5.0, "2025-11-04": 6.0}
    monkeypatch.setattr(hd, "MarketStore", lambda: _FakeStore({"AAA": old, "NDQ.AX": old, "AUDCNY=X": old}))
    d = _build(tmp_path, holdings=HOLDINGS[:2])
    assert d["status"] == "insufficient_data"
    for target in ("email", "chat"):
        md = hd.render_digest_md(d, target)
        assert "AAA（行情已停在 2025-11-04，太旧）" in md and "NDQ.AX（行情已停在 2025-11-04，太旧）" in md
        assert "不足 2 根" not in md


def test_no_holdings_renders_nothing(tmp_path):
    d = _build(tmp_path, holdings=[{"symbol": "T", "units": 5.0, "avg_cost": 1.0, "cost_currency": "CNY",
                                    "is_tracking_only": True}])
    assert d["status"] == "no_holdings" and hd.render_digest_md(d, "email") == ""
    assert hd.render_digest_md({"status": "no_holdings"}, "chat") == ""


# ============ 事件 ============

def test_events_top2_by_severity_dedupe_alias_and_window(tmp_path):
    _add_event("Alpha Co cuts full-year guidance on weak demand", ["AAA"], sev="high", src="Bloomberg")
    _add_event("Alpha Co cuts full-year guidance on weak demand, shares fall", ["AAA"], sev="mid", src="CNBC")   # 转述 → 去重
    _add_event("Alpha Co announces new CFO", ["AAA"], sev="mid", src="WSJ")
    _add_event("Alpha Co minor product tweak", ["AAA"], sev="low")                                              # 低于 mid
    _add_event("Alpha Co old story from last month", ["AAA"], sev="high", age_days=9)                            # 窗口外
    _add_event("Nasdaq-100 rebalance announced", ["^NDX"], sev="mid", src="Nasdaq")                              # NDQ.AX 经代理命中
    _add_event("Unrelated company news", ["ZZZ"], sev="high")
    d = _build(tmp_path)
    ev = d["events"]
    assert [e["title"] for e in ev["AAA"]] == ["Alpha Co cuts full-year guidance on weak demand", "Alpha Co announces new CFO"]
    assert [e["source"] for e in ev["AAA"]] == ["Bloomberg", "WSJ"]
    assert ev["NDQ.AX"][0]["title"] == "Nasdaq-100 rebalance announced" and ev["NDQ.AX"][0]["source"] == "Nasdaq"
    assert "GC=F" not in ev and "ZZZ" not in ev
    md = hd.render_digest_md(d, "email")
    assert "- **阿尔法**：Alpha Co cuts full-year guidance on weak demand（Bloomberg ·" in md
    assert "近 7 天相关事件" in md and "不是前瞻日历" in md


def test_events_empty_and_failure_degrade(tmp_path, monkeypatch):
    d = _build(tmp_path)
    assert d["events"] == {} and "- 持仓无已入库事件" in hd.render_digest_md(d, "email")

    def boom(*a, **k):
        raise RuntimeError("events.db locked")
    monkeypatch.setattr(hd, "_recent_events", boom)
    d = _build(tmp_path)
    assert d["status"] == "ok" and d["events"] is None                     # 归因不受影响
    md = hd.render_digest_md(d, "chat")
    assert "- n/a（事件库读取失败）" in md and "今天持仓合计" in md


# ============ 日历 ============

def test_calendar_only_stocks_window_and_no_source(tmp_path):
    asked: List[str] = []

    def cal(sym):
        asked.append(sym)
        return {"Earnings Date": [datetime(2026, 1, 16).date(), datetime(2026, 1, 17).date()],     # 窗口 [01-09, 01-16] 含端点
                "Ex-Dividend Date": datetime(2026, 1, 20).date()}                                   # 窗口外

    d = _build(tmp_path, calendar_fn=cal)
    assert asked == ["AAA"]                                                # ETF（NDQ.AX 白名单）/ 黄金不查
    assert d["calendar"] == {"AAA": {"status": "ok", "items": [{"kind": "财报", "date": "2026-01-16"}]}}
    assert "- **阿尔法**：财报 01-16" in hd.render_digest_md(d, "email")

    # 取不到：空 dict / 无已知 key / 抛异常 → 无数据源
    for fn in (lambda s: {}, lambda s: {"Foo": 1}, MagicMock(side_effect=RuntimeError("yahoo 404"))):
        d = _build(tmp_path, calendar_fn=fn)
        assert d["calendar"]["AAA"]["status"] == "no_source"
        assert "- 无数据源：阿尔法" in hd.render_digest_md(d, "email")

    # 有日历但窗口内没有 → 明说没有，而不是"无数据源"
    d = _build(tmp_path, calendar_fn=lambda s: {"Earnings Date": [datetime(2026, 3, 1).date()]})
    assert d["calendar"]["AAA"]["status"] == "none"
    md = hd.render_digest_md(d, "email")
    assert "没有已知财报 / 除息日" in md and "无数据源" not in md


def test_calendar_timeout_marks_na_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "CALENDAR_TIMEOUT_SEC", 0.05)
    release = threading.Event()
    started = time.monotonic()
    d = _build(tmp_path, calendar_fn=lambda s: release.wait(5) or {})
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 2 and d["status"] == "ok" and d["calendar"]["AAA"]["status"] == "n/a"
    assert "- 无数据源：阿尔法" in hd.render_digest_md(d, "email")


def test_calendar_workers_are_daemon_threads(tmp_path):
    """评审 #3：卡住的 yfinance 请求不能拖住进程退出——工作线程必须是守护线程（ThreadPoolExecutor 的会在退出时被 join）。"""
    seen = []
    _build(tmp_path, calendar_fn=lambda s: seen.append(threading.current_thread().daemon) or {})
    assert seen == [True]


def test_calendar_section_when_no_single_stocks(tmp_path):
    only_etf = [h for h in HOLDINGS if h["symbol"] != "AAA"]
    d = _build(tmp_path, holdings=only_etf)
    assert d["calendar"] == {} and "持仓里没有个股" in hd.render_digest_md(d, "chat")


def test_calendar_total_failure_renders_na(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "_fetch_calendars", MagicMock(side_effect=RuntimeError("boom")))
    d = _build(tmp_path)
    assert d["status"] == "ok" and d["calendar"] is None
    assert "- n/a（日历读取失败）" in hd.render_digest_md(d, "email")


# ============ 渲染两变体 ============

def test_email_variant_is_markdown_table_that_survives_html_sanitizer(tmp_path):
    from openinvest.services.notifier import md_to_safe_html
    md = hd.render_digest_md(_build(tmp_path), "email")
    assert "| 持仓 | 权重 | 今日涨跌 | 对组合贡献 | 汇率影响 | 对比基准 |" in md
    assert "| 纳指ETF | 71.4% | +3.02% | +2.16pp | +1.02% | +2.02pp |" in md        # 外币行有汇率影响
    assert "| 阿尔法 | 3.6% | -5.00% | -0.18pp | — | -6.00pp |" in md               # 同币种汇率列是 —
    assert "| **持仓合计（不含现金）** | 100% | +1.98% | +1.98pp | +0.73% | +0.98pp |" in md
    assert "| 基准 BENCH | | +1.00% | | | |" in md
    html = md_to_safe_html(md)
    assert "<table>" in html and html.count("<th>") + html.count("<th ") == 6 and "<td" in html and "纳指ETF" in html


def test_chat_variant_has_no_table_and_same_numbers(tmp_path):
    md = hd.render_digest_md(_build(tmp_path), "chat")
    assert "|:--" not in md and "| 持仓 |" not in md and "<" not in md
    assert "- **纳指ETF**｜权重 71.4%｜今日 +3.02%｜贡献 +2.16pp｜汇率 +1.02%｜对基准 +2.02pp" in md
    assert "- **阿尔法**｜权重 3.6%｜今日 -5.00%｜贡献 -0.18pp｜汇率 —｜对基准 -6.00pp" in md
    assert "- **持仓合计（不含现金）**｜今日 +1.98%｜汇率 +0.73%｜对基准 +0.98pp" in md
    assert "- **基准 BENCH**｜+1.00%" in md


def test_untrusted_text_is_escaped_per_variant(tmp_path):
    claim = "Alpha <img src=x onerror=alert(1)> see [click](http://evil.example) `x` a|b"
    _add_event(claim, ["AAA"], sev="high", src="Wire<b>")
    d = _build(tmp_path)
    email, chat = hd.render_digest_md(d, "email"), hd.render_digest_md(d, "chat")
    for md in (email, chat):
        assert "\\[click\\](http://evil.example)" in md and "\\`x\\`" in md and "a/b" in md
    assert "<img" not in email and "&lt;img" in email and "Wire&lt;b>" in email
    assert "<img" in chat                                                  # 聊天平台不解析 HTML，不做 &lt; 转义
    from openinvest.services.notifier import md_to_safe_html
    assert "<img" not in md_to_safe_html(email) and "href" not in md_to_safe_html(email)


# ============ 接入日报 ============

def _assemble(**kw):
    return assemble_full_report(
        today="2026-01-09", macro_view="MACRO", gold_snapshot_text="", friction_report="",
        target_assets=[], asset_committees={}, skipped_assets=set(), total_assets_cny=0.0,
        final_decision_gemini="", **kw)


def test_assemble_places_digest_on_top_in_both_variants_and_default_is_unchanged():
    section = "## 今日持仓归因（01-08 → 01-09 收盘，CNY 计）\n\n**一句话**: x"
    for target in ("email", "chat"):
        rep = _assemble(holdings_digest_md=section, render_target=target)
        assert rep.count("## 今日持仓归因") == 1
        assert rep.index("# 投资委员会日报") < rep.index("## 今日持仓归因") < rep.index("## 1. 宏观环境")
    # 不传 = 旧行为（别的调用方不受影响）
    assert "## 今日持仓归因" not in _assemble() and "## 今日持仓归因" not in _assemble(holdings_digest_md="  \n")
    # chat 的 TL;DR 仍在最前
    committees = {"AAA": {"verdict": {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "risk", "alloc_cny": 0},
                          "report": SimpleNamespace(cio_memo="m", quant_view="q", risk_view="r")}}
    rep = assemble_full_report(
        today="2026-01-09", macro_view="", gold_snapshot_text="", friction_report="",
        target_assets=[{"symbol": "AAA"}], asset_committees=committees, skipped_assets=set(), total_assets_cny=0.0,
        final_decision_gemini="", render_target="chat", holdings_digest_md=section)
    assert rep.index("今日速览") < rep.index("## 今日持仓归因") < rep.index("## 1. 宏观环境")


def _run_daily_report(monkeypatch, digest_fn, send_email=False):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.jobs.daily_report as dr
    import openinvest.services.discipline as disc
    import openinvest.utils.quotes as quotes

    def session(**kw):
        verdict = {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "neutral", "alloc_cny": 0}
        report = SimpleNamespace(cio_memo="memo", quant_view="q", risk_view="r")
        return {"asset_committees": {"TEST.AX": {"verdict": verdict, "report": report}},
                "macro_view": "MV", "event_brief": "", "errors": {}}

    class Translator:
        def __init__(self, **kw):
            pass

        def run(self, prompt):
            return ""

    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": "TEST.AX"}]}
    pm.cash = {}
    pm.holdings = []
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "get_gold_snapshot", lambda **k: None)
    monkeypatch.setattr(dr, "_load_asset_events", lambda *a, **k: {})
    monkeypatch.setattr(dr, "_portfolio_summary", lambda pm_, total, prices, **kw: "PS")
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: "G")
    monkeypatch.setattr(dr, "send_gmail_notification", lambda c: "me@x")
    monkeypatch.setattr(cr, "run_committee_session", session)
    monkeypatch.setattr(sdk, "SDKAgent", Translator)
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")
    monkeypatch.setattr(quotes, "fetch_fund_nav", lambda s: None)
    monkeypatch.setattr(hd, "build_holdings_digest", digest_fn)
    return dr.run(send_email=send_email, include_report=True)


def test_daily_report_run_embeds_digest_with_matching_variant(monkeypatch):
    seen = []
    monkeypatch.setattr(hd, "render_digest_md", lambda d, target="email": seen.append(target) or f"## 今日持仓归因 [{target}]")
    chat = _run_daily_report(monkeypatch, lambda pm, today=None: {"status": "ok"}, send_email=False)
    mail = _run_daily_report(monkeypatch, lambda pm, today=None: {"status": "ok"}, send_email=True)
    assert "## 今日持仓归因 [chat]" in chat["full_report"] and "## 今日持仓归因 [email]" in mail["full_report"]
    assert seen == ["chat", "email"]


def test_daily_report_survives_digest_failure(monkeypatch):
    def boom(pm, today=None):
        raise RuntimeError("market_data.db gone")
    res = _run_daily_report(monkeypatch, boom)
    assert res["status"] == "success" and "## 今日持仓归因" not in res["full_report"]
    assert "## 1. 宏观环境" in res["full_report"]


def test_digest_reports_price_basis_and_reads_raw_when_complete(tmp_path, monkeypatch):
    """价格口径同账户业绩：最近一段 close_raw 全有 → 原始价（除息日的价差不被复权抹平）；缺 → 复权价。"""
    class _Pair(_FakeStore):
        def __init__(self, table, raw):
            super().__init__(table)
            self.raw = raw

        def get_close_pair_df(self, symbol):
            df = _df(self.table[symbol])
            df["CloseRaw"] = pd.to_numeric([self.raw.get(symbol, {}).get(d) for d in self.table[symbol]])
            return df

    table = {**TABLE, "AAA": {D8: 10.0, D9: 9.5}}
    raw = {"AAA": {D8: 10.2, D9: 9.7}, "NDQ.AX": {D8: 50.0, D9: None}}
    monkeypatch.setattr(hd, "MarketStore", lambda: _Pair(table, raw))
    d = _build(tmp_path)
    assert d["price_basis"] == {"AAA": "raw", "NDQ.AX": "adjusted", "GC=F": "adjusted"}
    assert _row(d, "AAA")["prev_close"] == 10.2 and _row(d, "AAA")["close"] == 9.7
    assert _row(d, "NDQ.AX")["close"] == 51.0   # 缺 D9 的 raw → 整只回退复权价
