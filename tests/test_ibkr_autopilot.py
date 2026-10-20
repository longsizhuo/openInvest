"""jobs/ibkr_autopilot.py 测试：盈透实盘 autopilot（默认关 / 试算 / 金额硬闸 / 清仓 / 幂等 / STOP / 对账 /
换汇 / 选股校验 / LLM 当日订单 decide——委员会裁决只当参考，不触发下单、不被记执行）+ IB Gateway 适配层的字段映射。

tmp INVEST_HOME 里造持仓 + 当日裁决 md + 当日订单 json；券商用 FakeBroker（_WebApi 的同名方法），
适配层本身用 FakeIB（ib_async IB 的同名方法）测；行情 / 汇率 / yfinance info / LLM 全 monkeypatch，
不联网、不连盈透。
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import openinvest.cli as cli
import openinvest.db.trades_db as trades_db_mod
import openinvest.jobs.ibkr_autopilot as ia
from openinvest.core import memory_store as ms
from openinvest.core.config import API_SETTABLE, IbkrConfig, load_config, reset_config, set_persisted_override
from openinvest.core.decision_ledger import list_decisions
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.quotes import QuoteSnapshot

TODAY = "2026-10-19"   # 周一
ACCT = "U1"


class _FakeTradesDB:
    def __init__(self, *a, **kw):
        pass

    def list_trades(self, limit=10000):
        return []


class FakeBroker:
    """_IbGateway 的同名方法（没有 convert：submit 一调就 AttributeError）。placed / whatifs 记录调用；reject=True 时 place 抛错（=盈透拒单 / 确认问题不在白名单）。"""

    def __init__(self, cash=300.0, positions=None, fills=None, reject=False, unknown=(), extra_cash=None):
        self._cash = {"USD": cash, **(extra_cash or {})}
        self.pos, self._fills, self.reject = dict(positions or {}), list(fills or []), reject
        self.unknown, self.placed, self.whatifs, self.open, self.closed = set(unknown), [], [], [], 0

    def accounts(self):
        return [ACCT]

    def cash(self):
        return dict(self._cash)

    def buying_power_usd(self):
        return getattr(self, "bp", None) if getattr(self, "bp", None) is not None else self._cash.get("USD", 0.0)

    def positions(self):
        return dict(self.pos)

    def open_orders(self):
        return list(self.open)

    def fills(self, days=1):
        return list(self._fills)

    def conid(self, symbol):
        return None if symbol in self.unknown else 1000 + len(symbol)

    def whatif(self, conid, side, qty, lmt, ref):
        self.whatifs.append({"symbol": ref.split("/")[-2] if ref.count("/") >= 2 else ref, "side": side, "qty": qty,
                             "lmt": lmt, "ref": ref})
        return {"commission": 0.35, "warning": None, "error": None}

    def place(self, conid, side, qty, lmt, ref):
        if self.reject:
            raise RuntimeError("A question was not given a positive reply: No trading permissions")
        sym = ref.split("/")[-2] if ref.count("/") >= 2 else ref.split("/")[-1]
        self.placed.append({"symbol": sym, "side": side, "qty": qty, "lmt": lmt, "ref": ref})
        self.open.append({"ref": ref, "side": side, "symbol": sym, "left": qty, "lmt": lmt, "status": "PreSubmitted"})
        return {"order_id": str(100 + len(self.placed)), "status": "PreSubmitted"}

    def news(self, symbol, days=2, limit=8):
        return [dict(h) for h in getattr(self, "headlines", {}).get(symbol, [])]

    def article(self, provider, article_id):
        return f"full text of {provider}:{article_id}"

    def close(self):
        self.closed += 1


def _fill(sym, side, qty, px, exec_id, fee=0.35, ref="oi:x", sec="STK"):
    return {"exec_id": exec_id, "symbol": sym, "side": side, "qty": qty, "price": px, "commission": fee,
            "ref": ref, "sec_type": sec, "date": TODAY}


@pytest.fixture
def home(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)
    monkeypatch.setattr(trades_db_mod, "TradesDB", _FakeTradesDB)
    monkeypatch.setattr(ia, "INVEST_ROOT", tmp_path)
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate",
                        lambda quote, base="CNY", as_of_date=None: {("USD", "CNY"): 7.1}.get((quote, base)))
    sent = []
    monkeypatch.setattr(ia, "_notify", lambda subject, lines: sent.append((subject, lines)))
    reset_config()
    store = MemoryStore(mem)
    store.sent = sent
    return store


def _seed(store, cash=300.0, holdings=None, targets=("GLDM", "QQQM")):
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Aggressive"}, "")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3, "max_single_invest_cny": 700,
        "target_assets": [{"symbol": s, "max_single_invest_cny": 700} for s in targets]}, "账户背景：测试")
    store.write("portfolio", "state", {"cash": {"USD": cash}, "holdings": holdings or [], "schema_version": 2}, "")


def _verdict(store, symbol, verdict, alloc=None):
    d = store.root / ".committee" / TODAY
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# Committee: {symbol}", "", f"**Date**: {TODAY}", f"**Symbol**: {symbol}",
             f"**Verdict**: {verdict} (confidence 0.70)"]
    if alloc is not None:
        lines.append(f"**Suggested allocation USD**: {alloc:,}")
    (d / f"{symbol}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prices(monkeypatch, prices, bar="2026-10-16", stale=False):
    def fake(h):
        p = prices.get(h["symbol"])
        return None if p is None else QuoteSnapshot(symbol=h["symbol"], price=p, currency="USD", unit="股",
                                                    last_updated=bar, is_stale=stale)
    monkeypatch.setattr(ia, "get_quote", fake)


def _enable(monkeypatch, dry_run=False):
    monkeypatch.setenv("INVEST_IBKR_ENABLED", "true")
    monkeypatch.setenv("INVEST_IBKR_DRY_RUN", "true" if dry_run else "false")
    monkeypatch.setenv("INVEST_IBKR_ACCOUNT", ACCT)
    reset_config()


def _orders(store, orders, note="", date=TODAY):
    """直接写 decide 的产物（memory/.ibkr/decisions/<date>.json），submit 读它。"""
    d = store.root / ".ibkr" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    norm = [{"symbol": o[0], "side": o[1], "full": o[2] == "all", "usd": 0.0 if o[2] == "all" else float(o[2]),
             "why": f"why-{o[0]}"} for o in orders]
    (d / f"{date}.json").write_text(json.dumps({"date": date, "orders": norm, "rejected": [], "note": note}),
                                    encoding="utf-8")


def _decision(sym):
    return next(d for d in list_decisions(days=3) if d["decision_id"] == f"{TODAY}/{sym}")


# ---------------- 配置 / 开关 ----------------

def test_config_defaults_and_whitelist():
    c = IbkrConfig()
    assert (c.enabled, c.dry_run, c.account, c.max_order_usd, c.max_day_usd, c.min_order_usd) == \
        (False, True, "", 100.0, 150.0, 20.0)
    assert load_config().ibkr == c
    # 真单开关不许经 MCP / web API / agent 的 config set 改（只认 env / home 自己的 override 文件）
    assert not [k for k in API_SETTABLE if k.startswith("ibkr.")]
    with pytest.raises(ValueError):
        set_persisted_override("ibkr.dry_run", False)
    with pytest.raises(ValueError, match="非负有限数"):
        set_persisted_override("autopilot.min_order_cny", float("nan"))   # NaN 曾能过 f < 0


@pytest.mark.parametrize("env", [{"INVEST_IBKR_MAX_ORDER_USD": "nan"}, {"INVEST_IBKR_MAX_DAY_USD": "inf"},
                                 {"INVEST_IBKR_MAX_ORDER_USD": "0"}, {"INVEST_IBKR_MIN_ORDER_USD": "500"}])
def test_bad_caps_fail_closed(home, monkeypatch, env):
    _enable(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_config()
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    out = ia.submit(TODAY, broker=b)
    assert out["status"] == "error" and out["reason"].startswith("caps_") and b.placed == []


def test_notify_escapes_html(monkeypatch):
    sent = {}
    monkeypatch.setattr("openinvest.services.notifier.send_email_html", lambda **kw: sent.update(kw))
    ia._notify("s", ['<img src=x onerror=alert(1)> & "x"'])
    assert "<img" not in sent["html_body"] and "&lt;img" in sent["html_body"]
    assert sent["plain_body"] == '<img src=x onerror=alert(1)> & "x"'


def test_notify_cmd_gets_stdin_and_falls_back_to_email(monkeypatch, tmp_path):
    sent = {}
    monkeypatch.setattr("openinvest.services.notifier.send_email_html", lambda **kw: sent.update(kw))
    out = tmp_path / "msg"
    monkeypatch.setenv("INVEST_IBKR_NOTIFY_CMD", f"tee {out}")
    reset_config()
    ia._notify("标题", ["$(touch pwned); `id`"])
    assert out.read_text() == "标题\n\n$(touch pwned); `id`" and sent == {}   # stdin 进，不经 shell
    monkeypatch.setenv("INVEST_IBKR_NOTIFY_CMD", "false")
    reset_config()
    ia._notify("停手", ["x"])
    assert sent["subject"] == "停手"                                         # 推送失败退回邮件
    monkeypatch.setenv("INVEST_IBKR_NOTIFY_CMD", f"tee {out}")
    reset_config()
    ia._notify("长", ["x" * 15000])
    assert sent["subject"] == "长"                                           # 超长不走推送（会被截）


def test_connect_uses_configured_port(monkeypatch):
    seen = {}

    class _IB:
        def connect(self, host, port, clientId, timeout):
            seen.update(host=host, port=port)

    monkeypatch.setattr("ib_async.IB", _IB)
    monkeypatch.setenv("INVEST_IBKR_PORT", "4002")
    reset_config()
    ia._IbGateway.connect("DU1")
    assert seen == {"host": "127.0.0.1", "port": 4002}   # 模拟盘网关另一个端口，只连本机


def test_disabled_is_noop(home):
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    b = FakeBroker()
    assert ia.submit(TODAY, broker=b)["reason"] == "ibkr_disabled"
    assert ia.reconcile(broker=b)["reason"] == "ibkr_disabled"
    assert ia.pick(TODAY, agent=object())["reason"] == "ibkr_disabled"
    assert ia.decide(TODAY, agent=object())["reason"] == "ibkr_disabled"
    assert b.placed == [] and b.whatifs == []


def test_stop_file_and_account_mismatch(home, monkeypatch, tmp_path):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    _prices(monkeypatch, {"QQQM": 40.0})
    (tmp_path / "STOP").write_text("manual\n")
    b = FakeBroker()
    assert ia.submit(TODAY, broker=b)["status"] == "halted" and b.placed == []
    (tmp_path / "STOP").unlink()
    monkeypatch.setenv("INVEST_IBKR_ACCOUNT", "U999")
    reset_config()
    assert ia.submit(TODAY, broker=b)["reason"].startswith("account_mismatch") and b.placed == []


def test_own_connection_is_closed(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    b = FakeBroker()
    monkeypatch.setattr(ia, "_connect", lambda acct, client_id=17: b)
    ia.submit(TODAY)
    ia.reconcile()
    ia.status()
    assert b.closed == 3                                     # 自己开的会话自己关
    ia.submit(TODAY, broker=b)
    assert b.closed == 3                                     # 调用方传进来的不归我们关


# ---------------- submit ----------------

def test_committee_verdict_alone_places_nothing_and_is_not_marked_executed(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)                # 委员会说买，但 LLM 当天没下单
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    assert ia.submit(TODAY, broker=b)["placed"] == [] and b.placed == []
    _orders(home, [("QQQM", "BUY", 50)])
    ia.submit(TODAY, broker=b)
    assert len(b.placed) == 1
    assert _decision("QQQM")["execution"] is None            # 执行的是 LLM 的单，委员会裁决不记执行


def test_dry_run_only_whatif(home, monkeypatch):
    _enable(monkeypatch, dry_run=True)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    out = ia.submit(TODAY, broker=b)
    assert b.placed == [] and len(b.whatifs) == 1
    row = out["placed"][0]
    assert (row["qty"], row["lmt"], row["dry_run"], row["commission"]) == (2.0, 40.4, True, 0.35)   # 整股：100 / 40.4 → 2
    ia.submit(TODAY, broker=b)
    assert len(b.whatifs) == 2                               # 试算不占 claim，可反复跑


def test_whatif_error_is_reported_not_placed(home, monkeypatch):
    _enable(monkeypatch, dry_run=True)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    monkeypatch.setattr(b, "whatif", lambda *a: {"error": "Fractional trading not permitted"})
    out = ia.submit(TODAY, broker=b)
    assert out["placed"] == [] and out["skipped"][0]["reason"] == "whatif_error: Fractional trading not permitted"


def test_live_buy_capped_limit_order_and_idempotent(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])                    # LLM 要 500，单笔闸钳到 100
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    out = ia.submit(TODAY, broker=b)
    (o,) = b.placed
    assert (o["symbol"], o["side"], o["qty"], o["lmt"], o["ref"]) == ("QQQM", "BUY", 2.0, 40.4, f"oi:{TODAY}/QQQM/BUY")
    assert out["placed"][0]["max_usd"] == pytest.approx(80.8)
    assert home.sent and "下单" in home.sent[-1][0] and "why-QQQM" in home.sent[-1][1][0]
    ia.submit(TODAY, broker=b)                               # 重跑：盈透已有同 ref 挂单 → 不二次下单
    assert len(b.placed) == 1
    b.open.clear()                                           # 挂单视图丢了：claim 兜底
    assert ia.submit(TODAY, broker=b)["skipped"] == [{"symbol": "QQQM", "reason": "already_done_today"}]
    assert len(b.placed) == 1


def test_exception_after_send_keeps_claim(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker()
    real_place = b.place
    monkeypatch.setattr(b, "place", lambda *a: (real_place(*a), None)[1])   # 单发出去了，回包是 None → 后续 .get 抛错
    out = ia.submit(TODAY, broker=b)
    assert out["skipped"][0]["reason"].startswith("error_after_send") and len(b.placed) == 1
    b.open.clear()
    assert ia.submit(TODAY, broker=b)["skipped"] == [{"symbol": "QQQM", "reason": "already_done_today"}]
    assert len(b.placed) == 1                                # 没有因为 unclaim 二次下单


def test_day_cap_shrinks_later_orders(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500), ("GLDM", "BUY", 500)])
    _prices(monkeypatch, {"GLDM": 30.0, "QQQM": 40.0})
    b = FakeBroker()
    out = ia.submit(TODAY, broker=b)
    o1, o2 = b.placed
    assert (o1["symbol"], o1["qty"]) == ("GLDM", 3.0)        # 100 / 30.3 → 3 股 = 90.9（按代码排序，先 GLDM）
    assert (o2["symbol"], o2["qty"]) == ("QQQM", 1.0)        # 剩 150 − 90.9 = 59.1 → /40.4 → 1 股
    assert out["used_usd"] == pytest.approx(131.3) and out["used_usd"] <= 150.0


def test_open_orders_and_todays_fills_count_toward_day_cap(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker(fills=[_fill("GLDM", "BOT", 2.0, 50.0, "e1", ref=f"oi:{TODAY}/GLDM/BUY")])   # 已成交 100
    ia.submit(TODAY, broker=b)
    (o,) = b.placed
    assert o["qty"] == 1.0                                   # 剩 50 → /40.4 → 1 股


def test_exit_sells_position_not_in_targets(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _prices(monkeypatch, {"XLE": 30.0})
    b = FakeBroker(positions={"XLE": 3})
    out = ia.submit(TODAY, broker=b)
    (o,) = b.placed
    assert (o["side"], o["qty"], o["lmt"], o["ref"]) == ("SELL", 3.0, 29.7, f"oi:exit:{TODAY}/XLE")
    assert out["placed"][0]["exit"] is True


def test_sell_capped_and_no_short(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "SELL", "all"), ("GLDM", "SELL", 50)])
    _prices(monkeypatch, {"QQQM": 120.0, "GLDM": 50.0})
    b = FakeBroker(positions={"QQQM": 3.0})                  # GLDM 没仓位
    out = ia.submit(TODAY, broker=b)
    (o,) = b.placed
    assert (o["symbol"], o["side"], o["qty"]) == ("QQQM", "SELL", 1.0)   # 清仓：一股 120 > 单笔 100 但 ≤ 当日 150 → 卖 1 股
    assert {"symbol": "GLDM", "reason": "no_position"} in out["skipped"]


def test_exit_exception_respects_day_cap_and_partial_sells_stay_capped(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "SELL", "all"), ("GLDM", "SELL", 130)])
    _prices(monkeypatch, {"QQQM": 200.0, "GLDM": 120.0})
    b = FakeBroker(positions={"QQQM": 3.0, "GLDM": 2.0})
    out = ia.submit(TODAY, broker=b)
    reasons = {x["symbol"]: x["reason"] for x in out["skipped"]}
    assert b.placed == []
    assert reasons["QQQM"] == "exit_blocked_by_caps"          # 一股 200 > 当日上限 150：清仓也不卖，报警
    assert reasons["GLDM"] == "share_price_above_cap"         # 部分减仓不享受 1 股例外
    assert home.sent and "清仓卖不出去" in home.sent[-1][1][-1]


def test_sells_stop_when_day_budget_used(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "SELL", "all")])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker(positions={"QQQM": 3.0}, fills=[_fill("GLDM", "BOT", 3.0, 50.0, "e1", ref=f"oi:{TODAY}/GLDM/BUY")])
    assert ia.submit(TODAY, broker=b)["skipped"] == [{"symbol": "QQQM", "reason": "day_cap_reached"}] and b.placed == []


def test_share_price_above_cap_is_reported(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 500)])
    _prices(monkeypatch, {"QQQM": 296.0})                    # 只能整股：一股贵过单笔上限 100
    b = FakeBroker()
    out = ia.submit(TODAY, broker=b)
    assert b.placed == [] and out["skipped"] == [{"symbol": "QQQM", "reason": "share_price_above_cap"}]


def test_skips_orders_outside_list_and_no_cash(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash=15.0)
    _orders(home, [("AAPL", "BUY", 80), ("QQQM", "BUY", 80)])   # 名单在 decide 之后被改过 → AAPL 挡住
    _prices(monkeypatch, {"QQQM": 40.0, "AAPL": 40.0})
    b = FakeBroker(cash=15.0)
    out = ia.submit(TODAY, broker=b)
    assert b.placed == [] and out["skipped"] == [{"symbol": "QQQM", "reason": "no_cash"}]


def test_unknown_contract_and_rejection_are_terminal(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("GLDM", "BUY", 60), ("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"GLDM": 50.0, "QQQM": 40.0})
    b = FakeBroker(unknown={"GLDM"}, reject=True)
    out = ia.submit(TODAY, broker=b)
    reasons = {s["symbol"]: s["reason"] for s in out["skipped"]}
    assert reasons["GLDM"] == "not_tradable_at_ibkr"
    assert reasons["QQQM"].startswith("rejected: A question was not given a positive reply")
    b.reject = False
    ia.submit(TODAY, broker=b)                               # 终局：claim 留着，不重试
    assert b.placed == []


def test_stale_quote_unclaims_for_retry(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"QQQM": 40.0}, bar="2026-10-01")
    b = FakeBroker()
    assert ia.submit(TODAY, broker=b)["skipped"] == [{"symbol": "QQQM", "reason": "stale_quote"}]
    _prices(monkeypatch, {"QQQM": 40.0})
    ia.submit(TODAY, broker=b)
    assert len(b.placed) == 1


# ---------------- 换汇 ----------------

def test_buying_power_counts_base_currency_and_submit_never_pre_converts(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash=0.0)
    _orders(home, [("QQQM", "BUY", 90)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker(cash=0.0, extra_cash={"AUD": 500.0})
    b.bp = 348.0                                             # AvailableFunds AUD 500 折 USD：盈透下单时自动换汇
    out = ia.submit(TODAY, broker=b)
    assert [o["qty"] for o in b.placed] == [2.0]   # 不先自己换汇（10-20 叠加透支的教训）
    assert "fx" not in out


# ---------------- reconcile ----------------

def test_reconcile_books_fill_and_fee_idempotently(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    b = FakeBroker(positions={"QQQM": 0.495}, fills=[_fill("QQQM", "BOT", 0.495, 201.5, "0001.01")])
    b._cash["USD"] = round(300 - 0.495 * 201.5 - 0.35, 2)
    out = ia.reconcile(broker=b)
    assert out["status"] == "ok" and [x["exec_id"] for x in out["booked"]] == ["0001.01"]
    pm = PortfolioManager(home)
    assert pm.find_holding("QQQM")["units"] == pytest.approx(0.495)
    assert pm.cash_amount("USD") == pytest.approx(300 - 0.495 * 201.5 - 0.35, abs=0.01)   # 账本按分取整
    assert out["cash_drift"] == {}
    again = ia.reconcile(broker=b)
    assert again["booked"] == [] and PortfolioManager(home).find_holding("QQQM")["units"] == pytest.approx(0.495)
    assert [h["action"] for h in home.read_history()] == ["buy", "fee"]


def test_reconcile_books_fx_conversion_both_legs(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash=0.0)
    store = home
    store.write("portfolio", "state", {"cash": {"USD": 0.0, "AUD": 500.0}, "holdings": [], "schema_version": 2}, "")
    fx = {**_fill("AUD.USD", "SLD", 495.0, 0.7, "fx-1", fee=2.0, ref=f"oi:fx:{TODAY}/AUDUSD", sec="CASH"), "commission_ccy": "USD"}
    b = FakeBroker(cash=round(495 * 0.7 - 2.0, 2), extra_cash={"AUD": 5.0}, fills=[fx])
    out = ia.reconcile(broker=b)
    assert out["status"] == "ok" and out["booked"][0]["fx"] == "AUD.USD"
    pm = PortfolioManager(home)
    assert pm.cash_amount("AUD") == pytest.approx(5.0) and pm.cash_amount("USD") == pytest.approx(495 * 0.7 - 2.0)
    assert out["cash_drift"] == {}
    assert ia.reconcile(broker=b)["booked"] == []            # 幂等


def test_reconcile_position_drift_halts(home, monkeypatch, tmp_path):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker(positions={"QQQM": 1.0})                  # 券商有、账本没有 → 有人在账户里动了手
    out = ia.reconcile(broker=b)
    assert out["status"] == "halted" and (tmp_path / "STOP").exists()
    assert ia.submit(TODAY, broker=b)["status"] == "halted" and b.placed == []


# ---------------- status ----------------

def test_status_is_readonly_snapshot(home, monkeypatch, tmp_path):
    _enable(monkeypatch)
    _seed(home)
    _orders(home, [("QQQM", "BUY", 60)])
    _prices(monkeypatch, {"QQQM": 40.0})
    b = FakeBroker(positions={"GLDM": 1.5}, fills=[_fill("GLDM", "BOT", 1.5, 50.0, "e9")])
    ia.submit(TODAY, broker=b)                               # 造一张挂单
    monkeypatch.setattr(ia, "_today", lambda: TODAY)
    out = ia.status(broker=b)
    assert len(b.placed) == 1 and b.whatifs == []            # status 本身不下单
    br = out["broker"]
    assert br["cash"]["USD"] == 300.0 and br["positions"] == {"GLDM": 1.5}
    assert br["open_orders"][0]["ref"] == f"oi:{TODAY}/QQQM/BUY"
    assert br["fills_today"][0]["exec_id"] == "e9"
    assert out["targets"] == ["GLDM", "QQQM"] and out["today_orders"][0]["symbol"] == "QQQM"
    assert out["ledger"]["cash"] == {"USD": 300.0} and out["halted"] is None
    (tmp_path / "STOP").write_text("drift\n")
    monkeypatch.setattr(ia, "_connect", lambda acct, client_id=17: (_ for _ in ()).throw(ConnectionRefusedError("down")))
    out = ia.status()
    assert out["halted"].endswith("drift") and out["broker"]["error"].startswith("unreachable")


# ---------------- _IbGateway 字段映射（假的 ib_async IB）----------------

from datetime import datetime, timezone  # noqa: E402

from ib_async import CommissionReport, Execution, Fill, Order, OrderState, OrderStatus, Stock, Trade  # noqa: E402
from ib_async import Forex as _Forex  # noqa: E402

UNSET = 1.7976931348623157e308


class FakeIB:
    def __init__(self, status="PreSubmitted"):
        self.status, self.calls, self.whatif_state = status, [], OrderState(commission=0.35, warningText="")

    def managedAccounts(self):
        return ["U1"]

    def accountValues(self, acct=""):
        av = lambda tag, ccy, v: SimpleNamespace(tag=tag, currency=ccy, value=v, account="U1")  # noqa: E731
        return [av("CashBalance", "USD", "312.5"), av("CashBalance", "BASE", "999"), av("$LEDGER-CashBalance", "AUD", "5"),
                av("$LEDGER-CashBalance", "BASE", "5"), av("TotalCashValue", "AUD", "500"), av("NetLiquidation", "USD", "1"),
                av("AvailableFunds", "AUD", "500")]

    def positions(self, acct=""):
        pos = lambda sym, sec, q: SimpleNamespace(contract=SimpleNamespace(symbol=sym, secType=sec), position=q)  # noqa: E731
        return [pos("QQQM", "STK", 0.495), pos("X", "OPT", 1), pos("ZERO", "STK", 0)]

    def reqAllOpenOrders(self):
        return self.openTrades()

    def openTrades(self):
        mk = lambda acct: Trade(contract=Stock("QQQM", "SMART", "USD"),  # noqa: E731
                                order=Order(orderRef="oi:a", action="BUY", totalQuantity=0.5, lmtPrice=202.0, account=acct),
                                orderStatus=OrderStatus(status="PreSubmitted", filled=0.1))
        return [mk("U1"), mk("U9")]

    def reqExecutions(self, flt=None):
        self.calls.append(("reqExecutions", flt))

    def fills(self):
        t = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)   # 北京 10-20 10:00
        fx = _Forex("AUDUSD")
        fx.localSymbol = "AUD.USD"
        return [
            Fill(Stock("QQQM", "SMART", "USD"), Execution(execId="e1", acctNumber="U1", side="BOT", shares=0.495,
                 price=201.5, orderRef="oi:a", time=t), CommissionReport(commission=0.35), t),
            Fill(fx, Execution(execId="e2", acctNumber="U1", side="SLD", shares=495, price=0.7, time=t),
                 CommissionReport(commission=2.88, currency="AUD"), t),
            Fill(Stock("SPY", "SMART", "USD"), Execution(execId="e3", acctNumber="U9", side="BOT", shares=1, price=1,
                 time=t), CommissionReport(), t)]

    def sleep(self, s):
        pass

    def qualifyContracts(self, c):
        if getattr(c, "secType", "") == "CASH":
            if c.symbol != "AUD":
                return [None]
            c.localSymbol, c.conId = "AUD.USD", 14433401
            return [c]
        if c.symbol == "NOPE":
            return [None]
        c.conId = 756733
        return [c]

    def whatIfOrder(self, contract, order):
        self.calls.append(("whatIfOrder", contract, order))
        return self.whatif_state

    def placeOrder(self, contract, order):
        order.orderId = 123
        self.calls.append(("placeOrder", contract, order))
        log = [SimpleNamespace(message="No trading permissions for this contract")] if self.status == "Inactive" else []
        return SimpleNamespace(orderStatus=SimpleNamespace(status=self.status), log=log)

    def reqNewsProviders(self):
        return [SimpleNamespace(code="DJ-N"), SimpleNamespace(code="BRFG")]

    def reqHistoricalNews(self, con_id, codes, start, end, limit):
        self.calls.append(("news", codes, start, end, limit))
        fresh = datetime.now(timezone.utc)
        return [SimpleNamespace(time=fresh, providerCode="DJ-N", articleId="DJ-N$1", headline="{A:800015:L:en}QQQM Climbs"),
                SimpleNamespace(time=datetime(2026, 8, 1, tzinfo=timezone.utc), providerCode="DJ-N", articleId="old",
                                headline="{A:1}Old item")]

    def reqNewsArticle(self, provider, article_id):
        return SimpleNamespace(articleType=0, articleText="<p>&#10;  Stocks &amp; bonds</p><p>rally</p>")

    def disconnect(self):
        self.calls.append("disconnect")


def test_gateway_read_mappings():
    ib = FakeIB()
    g = ia._IbGateway(ib, "U1")
    assert g.accounts() == ["U1"]
    assert g.cash() == {"USD": 312.5, "AUD": 5.0}            # CashBalance / $LEDGER-CashBalance 都认，BASE 汇总行不算一个币种
    assert g.positions() == {"QQQM": 0.495}                  # 只要非零 STK
    assert g.open_orders() == [{"ref": "oi:a", "side": "BUY", "symbol": "QQQM", "left": pytest.approx(0.4),
                                "lmt": 202.0, "status": "PreSubmitted"}]   # 别的账户的挂单不要
    f = g.fills(days=7)
    (_, flt), = [c for c in ib.calls if c[0] == "reqExecutions"]
    assert flt.acctCode == "U1" and len(flt.time) == len("20261013-00:00:00")   # 按时间回看，不只当日
    assert [x["exec_id"] for x in f] == ["e1", "e2"]          # 别的账户的成交不要
    assert (f[0]["symbol"], f[0]["side"], f[0]["qty"], f[0]["commission"], f[0]["sec_type"], f[0]["date"]) == \
        ("QQQM", "BOT", 0.495, 0.35, "STK", "2026-10-20")
    assert (f[1]["symbol"], f[1]["side"], f[1]["sec_type"], f[1]["commission"], f[1]["commission_ccy"]) == \
        ("AUD.USD", "SLD", "CASH", 2.88, "AUD")                # 换汇佣金按券商回报的币种（AUD）


def test_gateway_orders_whatif_and_rejection():
    ib = FakeIB()
    g = ia._IbGateway(ib, "U1")
    assert g.conid("NOPE") is None
    cid = g.conid("QQQM")
    assert cid == 756733
    assert g.whatif(cid, "BUY", 0.495, 202.0, "oi:r") == {"commission": 0.35, "warning": None, "error": None}
    _, contract, o = ib.calls[-1]
    assert contract.symbol == "QQQM"
    assert (o.action, o.totalQuantity, o.orderType, o.lmtPrice, o.tif, o.account, o.orderRef, o.outsideRth) == \
        ("BUY", 0.495, "LMT", 202.0, "DAY", "U1", "oi:r", False)
    ib.whatif_state = OrderState()                            # 网关没回结果（被拒）
    assert g.whatif(cid, "BUY", 0.495, 202.0, "oi:r")["error"]
    ib.whatif_state = OrderState(minCommission=1.0, maxCommission=1.2)   # 只给 min / max 佣金：也算成功
    assert g.whatif(cid, "BUY", 1, 202.0, "oi:r")["error"] is None
    ib.whatif_state = OrderState(commission=0.35, warningText="")
    assert g.place(cid, "SELL", 0.3, 89.1, "oi:s") == {"order_id": 123, "status": "PreSubmitted"}
    ib.status = "Inactive"
    with pytest.raises(RuntimeError, match="rejected Inactive No trading permissions"):
        g.place(cid, "BUY", 0.1, 202.0, "oi:t")
    ib.status = "PendingSubmit"                               # 等不到明确回执：不当成功
    with pytest.raises(RuntimeError, match="unconfirmed PendingSubmit"):
        g.place(cid, "BUY", 0.1, 202.0, "oi:u")
    g.close()
    assert ib.calls[-1] == "disconnect"


class _Agent:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self.reply


_INFO = {
    "QQQM": {"quoteType": "ETF", "currency": "USD", "exchange": "NMS", "longName": "Invesco NASDAQ 100 ETF",
             "shortName": "Invesco NASDAQ 100 ETF", "regularMarketPrice": 60.0, "averageDailyVolume3Month": 3e6},
    "VGSH": {"quoteType": "ETF", "currency": "USD", "exchange": "NMS", "longName": "Vanguard Short-Term Treasury ETF",
             "regularMarketPrice": 58.0, "averageDailyVolume3Month": 2e6},
    "TQQQ": {"quoteType": "ETF", "currency": "USD", "exchange": "NMS", "longName": "ProShares UltraPro QQQ",
             "regularMarketPrice": 80.0, "averageDailyVolume3Month": 5e7},
    "PENY": {"quoteType": "EQUITY", "currency": "USD", "exchange": "NCM", "longName": "Penny Corp",
             "regularMarketPrice": 2.0, "averageDailyVolume3Month": 1e8},
    "TINY": {"quoteType": "EQUITY", "currency": "USD", "exchange": "NYQ", "longName": "Tiny Inc",
             "regularMarketPrice": 30.0, "averageDailyVolume3Month": 1e4},
}


def test_check_tradable_rules():
    assert ia._check_tradable("QQQM", _INFO["QQQM"]) is None
    assert ia._check_tradable("QQQM", {**_INFO["QQQM"], "regularMarketPrice": 296.0}, 100.0) == "price_above_order_cap"   # 整股买不起
    assert ia._check_tradable("VGSH", _INFO["VGSH"]) is None          # Short-Term 不是做空
    assert ia._check_tradable("TQQQ", _INFO["TQQQ"]) == "leveraged_or_inverse"
    assert ia._check_tradable("PENY", _INFO["PENY"]) == "price_below_min"
    assert ia._check_tradable("TINY", _INFO["TINY"]) == "illiquid"
    assert ia._check_tradable("600519.SS", {}) == "not_us_ticker"
    assert ia._check_tradable("BRK.B", {**_INFO["QQQM"]}) == "dotted_ticker_unsupported"
    assert ia._check_tradable("BHP", {**_INFO["QQQM"], "exchange": "ASX"}) == "not_us_listed"
    assert ia._check_tradable("SHOP", {**_INFO["QQQM"], "currency": "CAD"}) == "not_usd"
    assert ia._check_tradable("BTC", {**_INFO["QQQM"], "quoteType": "CRYPTOCURRENCY"}) == "not_stock_or_etf"


def test_pick_validates_and_replaces_targets(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, targets=("GLDM",))
    monkeypatch.setattr(ia, "_ticker_info", lambda s: _INFO[s])
    reply = json.dumps({"picks": [{"symbol": "qqqm", "why": "纳指"}, {"symbol": "TQQQ", "why": "x"},
                                  {"symbol": "PENY", "why": "x"}, {"symbol": "VGSH", "why": "现金替代"}],
                        "note": "偏防守"})
    agent = _Agent(f"好的：\n```json\n{reply}\n```")
    out = ia.pick(TODAY, agent=agent)
    assert out["status"] == "ok" and [a["symbol"] for a in out["picks"]] == ["QQQM", "VGSH"]
    assert {r["symbol"]: r["reason"] for r in out["rejected"]} == \
        {"TQQQ": "leveraged_or_inverse", "PENY": "price_below_min"}
    targets = MemoryStore(home.root).read("strategy").metadata["target_assets"]
    assert [(t["symbol"], t["channel"], t["max_single_invest_cny"]) for t in targets] == \
        [("QQQM", "IBKR", 710), ("VGSH", "IBKR", 710)]
    assert "_why" not in targets[0]
    assert json.loads((home.root / ".ibkr" / "picks" / f"{TODAY}.json").read_text())["previous"] == ["GLDM"]
    assert "账户背景：测试" in agent.prompts[0] and "USD 300.00" in agent.prompts[0]


def test_pick_accepts_bare_symbol_list_and_skips_junk(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, targets=("GLDM",))
    monkeypatch.setattr(ia, "_ticker_info", lambda s: _INFO[s])
    out = ia.pick(TODAY, agent=_Agent('{"picks": ["QQQM", 5, null, {"symbol": "VGSH", "why": "x"}]}'))
    assert out["status"] == "ok" and [a["symbol"] for a in out["picks"]] == ["QQQM", "VGSH"]


def test_pick_all_invalid_keeps_targets(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, targets=("GLDM",))
    monkeypatch.setattr(ia, "_ticker_info", lambda s: _INFO[s])
    out = ia.pick(TODAY, agent=_Agent('{"picks": [{"symbol": "TQQQ"}]}'))
    assert out["status"] == "error" and out["reason"] == "no_valid_picks"
    assert [t["symbol"] for t in MemoryStore(home.root).read("strategy").metadata["target_assets"]] == ["GLDM"]


def test_pick_only_on_monday_unless_forced(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, targets=("GLDM",))
    monkeypatch.setattr(ia, "_ticker_info", lambda s: _INFO[s])
    agent = _Agent('{"picks": [{"symbol": "QQQM"}]}')
    assert ia.pick("2026-10-20", agent=agent)["reason"] == "not_pick_day" and agent.prompts == []
    assert ia.pick("2026-10-20", agent=agent, force=True)["status"] == "ok"


# ---------------- decide ----------------

def test_decide_prompt_validation_and_file(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, holdings=[{"symbol": "QQQM", "kind": "etf", "units": 0.5, "avg_cost": 180.0, "unit_label": "股",
                           "cost_currency": "USD", "proxy_kind": "direct"}])
    _verdict(home, "QQQM", "HOLD")
    _verdict(home, "GLDM", "ACCUMULATE", 80)
    _prices(monkeypatch, {"QQQM": 200.0, "GLDM": 50.0})
    picks = home.root / ".ibkr" / "picks"
    picks.mkdir(parents=True)
    (picks / "2026-10-19.json").write_text(json.dumps({"picks": [{"symbol": "GLDM", "_why": "对冲仓位"}]}))
    reply = json.dumps({"orders": [
        {"symbol": "gldm", "side": "buy", "usd": 40, "why": "委员会也看多"},
        {"symbol": "QQQM", "side": "SELL", "usd": "all", "why": "止盈"},
        {"symbol": "AAPL", "side": "BUY", "usd": 50},           # 不在名单
        {"symbol": "GLDM", "side": "SELL", "usd": 10},          # 一个标的一单
        {"symbol": "QQQM", "side": "HOLD", "usd": 10},          # 方向非法（但同标的已有单，按先到先拒）
        {"symbol": "GLDM", "side": "BUY", "usd": "nan"}], "note": "偏谨慎"})
    agent = _Agent(f"```json\n{reply}\n```")
    out = ia.decide(TODAY, agent=agent)
    assert out["status"] == "ok"
    assert [(o["symbol"], o["side"], o["full"], o["usd"]) for o in out["orders"]] == \
        [("GLDM", "BUY", False, 40.0), ("QQQM", "SELL", True, 0.0)]
    assert {r["symbol"]: r["reason"] for r in out["rejected"]}["AAPL"] == "not_in_list"
    prompt = agent.prompts[0]
    assert "GLDM：ACCUMULATE（置信度 0.70，建议 USD 80）" in prompt and "QQQM：HOLD" in prompt
    assert "GLDM：对冲仓位" in prompt and "浮盈 +11.1%" in prompt and "账户背景：测试" in prompt
    saved = json.loads((home.root / ".ibkr" / "decisions" / f"{TODAY}.json").read_text())
    assert saved["orders"] == out["orders"] and saved["note"] == "偏谨慎"


def test_decide_reuses_todays_file_and_llm_error_writes_nothing(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _prices(monkeypatch, {"QQQM": 200.0, "GLDM": 50.0})

    class Boom:
        def run(self, prompt):
            raise TimeoutError("llm timeout")
    with pytest.raises(TimeoutError):
        ia.decide(TODAY, agent=Boom())
    assert not (home.root / ".ibkr" / "decisions" / f"{TODAY}.json").exists()   # 下次重跑还能问
    first = _Agent('{"orders": [{"symbol": "QQQM", "side": "BUY", "usd": 30}]}')
    assert ia.decide(TODAY, agent=first)["status"] == "ok"
    second = _Agent('{"orders": [{"symbol": "GLDM", "side": "BUY", "usd": 99}]}')
    again = ia.decide(TODAY, agent=second)
    assert again["reason"] == "already_decided" and second.prompts == []
    assert [o["symbol"] for o in again["orders"]] == ["QQQM"]


# ---------------- CLI ----------------

def test_cli_daily_runs_stages_in_order_and_isolates_failure(monkeypatch, capfd):
    import openinvest.jobs.daily_report as dr
    import openinvest.jobs.verdict_review as vr
    calls = []
    monkeypatch.setattr(ia, "pick", lambda force=False: calls.append(("pick", force)) or {"status": "skipped"})
    monkeypatch.setattr(dr, "run", lambda send_email=True, include_report=False: calls.append(("daily_report", send_email)) or {})
    monkeypatch.setattr(ia, "decide", lambda: calls.append(("decide",)) or {"status": "ok"})
    monkeypatch.setattr(ia, "submit", lambda: (_ for _ in ()).throw(ConnectionRefusedError("gateway down")))
    monkeypatch.setattr(vr, "run", lambda **kw: calls.append(("verdict_review",)) or {"status": "ok"})
    alerts = []
    monkeypatch.setattr(ia, "notify_failure", lambda phase, problems: alerts.append((phase, sorted(problems))))
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "ibkr_autopilot", "daily"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    assert calls == [("pick", False), ("daily_report", False), ("decide",), ("verdict_review",)]   # 日报绝不发邮件
    out = json.loads(capfd.readouterr().out)
    assert out["submit"]["status"] == "error" and "gateway down" in out["submit"]["error"]
    assert alerts == [("daily", ["submit"])]


def test_cli_alerts_on_error_status_without_exception(monkeypatch, capfd):
    alerts = []
    monkeypatch.setattr(ia, "notify_failure", lambda phase, problems: alerts.append((phase, sorted(problems))))
    monkeypatch.setattr(ia, "reconcile", lambda: {"status": "error", "reason": "account_mismatch: 配置 'U1'，会话 ['U9']"})
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "ibkr_autopilot", "reconcile"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1 and alerts == [("reconcile", ["reconcile"])]   # 不再静默退出 0
    monkeypatch.setattr(ia, "reconcile", lambda: {"status": "ok"})
    alerts.clear()
    cli.main()                                                # 正常结束不报警、不退出
    assert alerts == []


def test_validate_orders_rejects_bad_amounts():
    ok, rejected = ia._validate_orders(
        [{"symbol": "QQQM", "side": "BUY", "usd": "nan"}, {"symbol": "GLDM", "side": "BUY", "usd": -5},
         {"symbol": "XLV", "side": "BUY", "usd": "all"}, {"symbol": "VEA", "side": "SELL", "usd": "ALL"},
         "not-a-dict"], {"QQQM", "GLDM", "XLV", "VEA"})
    assert [(o["symbol"], o["full"]) for o in ok] == [("VEA", True)]   # 只有 SELL 能写 all
    assert [r["reason"] for r in rejected] == ["bad_amount", "bad_amount", "bad_amount", "not_in_list"]


# ---------------- 常驻会话：自己交 JSON / 盘中批次 / 新闻 / brief ----------------

def test_session_orders_json_and_intraday_batch(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _prices(monkeypatch, {"QQQM": 40.0, "GLDM": 30.0})
    out = ia.decide(TODAY, orders_json='{"orders": [{"symbol": "QQQM", "side": "BUY", "usd": 90, "why": "早盘"}]}')
    assert out["status"] == "ok" and out["batch"] == 1 and out["orders"][0]["symbol"] == "QQQM"
    b = FakeBroker()
    ia.submit(TODAY, broker=b)
    assert [o["ref"] for o in b.placed] == [f"oi:{TODAY}/QQQM/BUY"]          # 90 → 2 股 = 80.8
    # 盘中新闻触发：第 2 批。文件、orderRef、claim 都和第 1 批分开；单日额度共用
    second = json.dumps({"orders": [{"symbol": "QQQM", "side": "BUY", "usd": 90},
                                    {"symbol": "AAPL", "side": "BUY", "usd": 50}]})
    out = ia.decide(TODAY, batch=2, orders_json=second)
    assert out["batch"] == 2 and [o["symbol"] for o in out["orders"]] == ["QQQM"]   # 名单外照样拒
    assert (home.root / ".ibkr" / "decisions" / f"{TODAY}.b2.json").exists()
    res = ia.submit(TODAY, broker=b, batch=2)
    assert b.placed[-1]["ref"] == f"oi:{TODAY}#b2/QQQM/BUY" and b.placed[-1]["qty"] == 1.0   # 剩 150 − 80.8 = 69.2 → 1 股
    assert res["used_usd"] == pytest.approx(121.2)
    assert ia.decide(TODAY, batch=2, orders_json='{"orders": []}')["reason"] == "already_decided"


def test_session_picks_json_any_day(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, targets=("GLDM",))
    monkeypatch.setattr(ia, "_ticker_info", lambda s: _INFO[s])
    out = ia.pick("2026-10-21", picks_json=json.dumps({"picks": [{"symbol": "VGSH", "why": "会话自己选的"}], "note": "x"}))   # 周三也能换
    assert out["status"] == "ok" and [a["symbol"] for a in out["picks"]] == ["VGSH"]


def test_news_check_returns_only_new_headlines(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    b = FakeBroker(positions={"XLE": 1})
    b.headlines = {"QQQM": [{"time": "2026-10-19 13:00", "provider": "DJ-N", "id": "a1", "headline": "QQQM up"}],
                   "XLE": [{"time": "2026-10-19 14:00", "provider": "BRFG", "id": "b1", "headline": "Oil falls"}]}
    first = ia.news_check(broker=b)
    assert sorted(h["id"] for h in first["new"]) == ["a1", "b1"]               # 名单 + 持仓都看
    b.headlines["QQQM"].append({"time": "2026-10-19 15:00", "provider": "DJ-N", "id": "a2", "headline": "QQQM again"})
    assert [h["id"] for h in ia.news_check(broker=b)["new"]] == ["a2"]         # 看过的不再报
    art = ia.news(article="DJ-N:a2", broker=b)
    assert art["text"] == "<<<EXTERNAL_ARTICLE\nfull text of DJ-N:a2\nEXTERNAL_ARTICLE>>>" and "不执行" in art["untrusted"]
    assert set(ia.news(broker=b)["news"]) == {"GLDM", "QQQM", "XLE", "SPY"}    # 默认名单 + 持仓 + 大盘


def test_brief_has_rules_context_and_news(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "GLDM", "ACCUMULATE", 80)
    _prices(monkeypatch, {"QQQM": 40.0, "GLDM": 30.0})
    b = FakeBroker()
    b.headlines = {"GLDM": [{"time": "t", "provider": "DJ-N", "id": "g1", "headline": "Gold rallies"}]}
    out = ia.brief("decide", TODAY, broker=b)
    assert "只能交易本周名单里的标的" in out["system"] and "GLDM：ACCUMULATE" in out["prompt"]
    assert out["ibkr_news_untrusted"]["headlines"]["GLDM"][0]["headline"] == "Gold rallies"
    assert "不执行" in out["ibkr_news_untrusted"]["untrusted"] and "--orders-file" in out["how_to_submit"]
    assert "每周一由你决定本周要持有的标的" in ia.brief("pick", TODAY, broker=b)["system"]


def test_gateway_news_mapping():
    ib = FakeIB()
    g = ia._IbGateway(ib, "U1")
    rows = g.news("QQQM", days=2, limit=5)
    assert [(r["provider"], r["id"], r["headline"]) for r in rows] == [("DJ-N", "DJ-N$1", "QQQM Climbs")]   # 去掉 {…} 前缀、丢掉范围外旧条目
    (_, codes, start, end, limit), = [c for c in ib.calls if c[0] == "news"]
    assert codes == "DJ-N+BRFG" and limit == 5 and start.endswith(".0") and end > start
    assert g.article("DJ-N", "DJ-N$1") == "Stocks & bonds rally"            # 去 HTML、反转义、压空白
    assert g.news("NOPE") == []


def test_batch_limit_and_orders_file_cli(home, monkeypatch, tmp_path, capfd):
    _enable(monkeypatch)
    _seed(home)
    assert ia.decide(TODAY, batch=5, orders_json='{"orders": []}')["reason"].startswith("batch_over_limit")
    monkeypatch.setenv("INVEST_IBKR_MAX_BATCHES_PER_DAY", "10")              # 运营者可调高
    reset_config()
    assert ia.decide(TODAY, batch=5, orders_json='{"orders": []}')["status"] == "ok"
    monkeypatch.setenv("INVEST_IBKR_MAX_BATCHES_PER_DAY", "4")
    reset_config()
    assert ia.submit(TODAY, broker=FakeBroker(), batch=0)["reason"].startswith("batch_over_limit")
    f = tmp_path / "orders.json"
    f.write_text(json.dumps({"orders": [{"symbol": "QQQM", "side": "BUY", "usd": 50, "why": "文件交单"}]}), encoding="utf-8")
    monkeypatch.setattr(ia, "_today", lambda: TODAY)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "ibkr_autopilot", "decide", "--orders-file", str(f), "--batch", "2"])
    cli.main()
    out = json.loads(capfd.readouterr().out)["decide"]
    assert out["batch"] == 2 and out["orders"][0]["why"] == "文件交单"


def test_reconcile_books_commission_in_its_own_currency(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash=0.0)
    home.write("portfolio", "state", {"cash": {"USD": 0.0, "AUD": 500.0}, "holdings": [], "schema_version": 2}, "")
    fx = {**_fill("AUD.USD", "SLD", 495.0, 0.7, "fx-2", fee=2.88, sec="CASH"), "commission_ccy": "AUD"}
    b = FakeBroker(cash=346.5, extra_cash={"AUD": round(5.0 - 2.88, 2)}, fills=[fx])
    assert ia.reconcile(broker=b)["cash_drift"] == {}          # 换汇佣金扣在 AUD 上，不是 USD
    pm = PortfolioManager(home)
    assert pm.cash_amount("AUD") == pytest.approx(2.12) and pm.cash_amount("USD") == pytest.approx(346.5)


def test_reconcile_books_broker_confirmed_negative_cash_without_halting(home, monkeypatch, tmp_path):
    """10-20 实盘：我们换了 AUD 495，盈透下单时又从未结算的 AUD 自动换了 151 → AUD 实际为负。账本照实记，不停手。"""
    _enable(monkeypatch)
    _seed(home, cash=0.0)
    home.write("portfolio", "state", {"cash": {"USD": 0.0, "AUD": 500.0}, "holdings": [], "schema_version": 2}, "")
    fills = [{**_fill("AUD.USD", "SLD", 495.0, 0.6979, "fx-ours", fee=2.8792, sec="CASH"), "commission_ccy": "AUD"},
             {**_fill("SCHD", "BOT", 3.0, 34.2995, "s1", fee=1.0), "commission_ccy": "USD"},
             {**_fill("AUD.USD", "SLD", 147.44, 0.6978, "fx-auto", fee=0.0, ref="", sec="CASH"), "commission_ccy": "AUD"}]
    b = FakeBroker(positions={"SCHD": 3.0}, cash=round(495 * 0.6979 - 102.8985 - 1.0 + 147.44 * 0.6978, 2),
                   extra_cash={"AUD": round(500 - 495 - 2.8792 - 147.44, 2)}, fills=fills)
    out = ia.reconcile(broker=b)
    assert out["status"] == "ok" and out["problems"] == [] and not (tmp_path / "STOP").exists()
    pm = PortfolioManager(home)
    assert pm.cash_amount("AUD") == pytest.approx(500 - 495 - 2.88 - 147.44, abs=0.02)   # 负的，照实记
    assert pm.find_holding("SCHD")["units"] == 3.0 and out["cash_drift"] == {}


def test_gateway_buying_power(monkeypatch):
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate", lambda a, b="CNY", as_of_date=None: 0.7 if (a, b) == ("AUD", "USD") else 1 / 0.7)
    g = ia._IbGateway(FakeIB(), "U1")
    assert g.buying_power_usd() == pytest.approx(500 * 0.7)    # AvailableFunds AUD 500 → USD
