"""jobs/ibkr_autopilot.py 测试：盈透实盘 autopilot（默认关 / 试算 / 金额硬闸 / 清仓 / 幂等 / STOP / 对账 / 选股校验）。

tmp INVEST_HOME 里造持仓 + 当日裁决 md；盈透网关用 FakeIB（只实现本模块调用的方法），Order / Stock 用 ib_async 真类；
行情 / 汇率 / yfinance info / LLM 全 monkeypatch，不联网、不连网关。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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


class FakeIB:
    def __init__(self, cash=300.0, positions=None, fills=None, status="PreSubmitted", unknown=()):
        self.cash, self.pos, self._fills, self.status = cash, dict(positions or {}), list(fills or []), status
        self.unknown, self.placed, self.whatifs, self.open = set(unknown), [], [], []

    def managedAccounts(self):
        return [ACCT]

    def accountValues(self, account=""):
        return [SimpleNamespace(tag="CashBalance", currency="USD", value=str(self.cash), account=ACCT),
                SimpleNamespace(tag="CashBalance", currency="AUD", value="999", account=ACCT)]

    def positions(self, account=""):
        return [SimpleNamespace(contract=SimpleNamespace(symbol=s, secType="STK"), position=q)
                for s, q in self.pos.items()]

    def reqExecutions(self):
        return []

    def fills(self):
        return self._fills

    def sleep(self, s):
        pass

    def openTrades(self):
        return self.open

    def qualifyContracts(self, c):
        return [None] if c.symbol in self.unknown else [c]

    def whatIfOrder(self, c, o):
        self.whatifs.append((c.symbol, o))
        return SimpleNamespace(commission=0.35, warningText="")

    def placeOrder(self, c, o):
        o.orderId = 100 + len(self.placed)
        log = [SimpleNamespace(message="No trading permissions")] if self.status == "Inactive" else []
        t = SimpleNamespace(order=o, orderStatus=SimpleNamespace(status=self.status, filled=0.0), log=log)
        self.placed.append((c.symbol, o))
        self.open.append(t)
        return t


def _fill(sym, side, qty, px, exec_id, fee=0.35, ref="oi:x", sec="STK"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=sym, secType=sec, currency="USD"),
        execution=SimpleNamespace(execId=exec_id, acctNumber=ACCT, side=side, shares=qty, price=px, orderRef=ref,
                                  time=datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)),
        commissionReport=SimpleNamespace(commission=fee, currency="USD"))


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
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"QQQM": 200.0})
    ib = FakeIB()
    out = ia.submit(TODAY, ib=ib)
    assert out["status"] == "error" and out["reason"].startswith("caps_") and ib.placed == []


def test_notify_escapes_html(monkeypatch):
    sent = {}
    monkeypatch.setattr("openinvest.services.notifier.send_email_html", lambda **kw: sent.update(kw))
    ia._notify("s", ['<img src=x onerror=alert(1)> & "x"'])
    assert "<img" not in sent["html_body"] and "&lt;img" in sent["html_body"]
    assert sent["plain_body"] == '<img src=x onerror=alert(1)> & "x"'


def test_disabled_is_noop(home):
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    ib = FakeIB()
    assert ia.submit(TODAY, ib=ib)["reason"] == "ibkr_disabled"
    assert ia.reconcile(ib=ib)["reason"] == "ibkr_disabled"
    assert ia.pick(TODAY, agent=object())["reason"] == "ibkr_disabled"
    assert ib.placed == [] and ib.whatifs == []


def test_stop_file_and_account_mismatch(home, monkeypatch, tmp_path):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"QQQM": 200.0})
    (tmp_path / "STOP").write_text("manual\n")
    ib = FakeIB()
    assert ia.submit(TODAY, ib=ib)["status"] == "halted" and ib.placed == []
    (tmp_path / "STOP").unlink()
    monkeypatch.setenv("INVEST_IBKR_ACCOUNT", "U999")
    reset_config()
    assert ia.submit(TODAY, ib=ib)["reason"].startswith("account_mismatch") and ib.placed == []


# ---------------- submit ----------------

def test_dry_run_only_whatif(home, monkeypatch):
    _enable(monkeypatch, dry_run=True)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"QQQM": 200.0})
    ib = FakeIB()
    out = ia.submit(TODAY, ib=ib)
    assert ib.placed == [] and len(ib.whatifs) == 1
    row = out["placed"][0]
    assert (row["qty"], row["lmt"], row["dry_run"], row["commission"]) == (0.495, 202.0, True, 0.35)
    assert _decision("QQQM")["execution"] is None          # 试算不记执行
    ia.submit(TODAY, ib=ib)
    assert len(ib.whatifs) == 2                              # 试算不占 claim，可反复跑


def test_live_buy_capped_limit_order_and_idempotent(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)                # 委员会要 500，单笔闸钳到 100
    _prices(monkeypatch, {"QQQM": 200.0})
    ib = FakeIB()
    out = ia.submit(TODAY, ib=ib)
    (sym, o), = ib.placed
    assert (sym, o.action, o.totalQuantity, o.lmtPrice, o.tif, o.account, o.outsideRth) == \
        ("QQQM", "BUY", 0.495, 202.0, "DAY", ACCT, False)
    assert o.orderRef == f"oi:{TODAY}/QQQM" and o.orderType == "LMT"
    assert out["placed"][0]["max_usd"] == pytest.approx(99.99)
    ex = _decision("QQQM")["execution"]
    assert ex["executed"] is True and ex["reason"] == "ibkr:PreSubmitted:100"
    assert home.sent and "下单" in home.sent[-1][0]
    ia.submit(TODAY, ib=ib)                                  # 重跑：盈透已有同 orderRef → 不二次下单
    assert len(ib.placed) == 1
    ib.open.clear()                                          # 网关重启丢了挂单视图：已记执行的裁决不再出现
    assert ia.submit(TODAY, ib=ib)["placed"] == [] and len(ib.placed) == 1


def test_claim_blocks_second_order_when_execution_write_failed(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"QQQM": 200.0})
    monkeypatch.setattr(ia, "record_execution", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    ib = FakeIB()
    ia.submit(TODAY, ib=ib)
    ib.open.clear()
    assert ia.submit(TODAY, ib=ib)["skipped"] == [{"symbol": "QQQM", "reason": "already_done_today"}]
    assert len(ib.placed) == 1


def test_day_cap_shrinks_later_orders(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "GLDM", "ACCUMULATE", 500)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"GLDM": 50.0, "QQQM": 200.0})
    ib = FakeIB()
    out = ia.submit(TODAY, ib=ib)
    (s1, o1), (s2, o2) = ib.placed
    assert (s1, o1.totalQuantity) == ("GLDM", 1.9801)        # 100 / 50.5
    assert (s2, o2.totalQuantity) == ("QQQM", 0.2475)        # 剩 150 − 99.995 ≈ 50.005 → /202
    assert out["used_usd"] <= 150.0


def test_open_orders_and_todays_fills_count_toward_day_cap(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 500)
    _prices(monkeypatch, {"QQQM": 200.0})
    ib = FakeIB(fills=[_fill("GLDM", "BOT", 2.0, 50.0, "e1", ref="oi:2026-10-19/GLDM")])   # 已成交 100
    ia.submit(TODAY, ib=ib)
    (_, o), = ib.placed
    assert o.totalQuantity == round(int(50 / 202 * 1e4) / 1e4, 4)


def test_exit_sells_position_not_in_targets(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _prices(monkeypatch, {"XLE": 90.0})
    ib = FakeIB(positions={"XLE": 0.3})
    out = ia.submit(TODAY, ib=ib)
    (sym, o), = ib.placed
    assert (sym, o.action, o.totalQuantity, o.lmtPrice, o.orderRef) == ("XLE", "SELL", 0.3, 89.1, f"oi:exit:{TODAY}/XLE")
    assert out["placed"][0]["exit"] is True


def test_sell_capped_and_no_short(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "SELL")
    _verdict(home, "GLDM", "TRIM", 50)
    _prices(monkeypatch, {"QQQM": 200.0, "GLDM": 50.0})
    ib = FakeIB(positions={"QQQM": 1.0})                     # GLDM 没仓位
    out = ia.submit(TODAY, ib=ib)
    (sym, o), = ib.placed
    assert (sym, o.action, o.totalQuantity) == ("QQQM", "SELL", 0.5)   # 清仓 200 被单笔闸钳到 100
    assert {"symbol": "GLDM", "reason": "no_position"} in out["skipped"]
    assert _decision("GLDM")["execution"]["executed"] is False


def test_skips_non_target_tiny_and_unknown(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash=15.0)
    _verdict(home, "AAPL", "ACCUMULATE", 80)                 # 不在名单 → 不碰
    _verdict(home, "QQQM", "ACCUMULATE", 80)                 # 现金 15 → no_cash
    _verdict(home, "GLDM", "HOLD")
    _prices(monkeypatch, {"QQQM": 200.0, "AAPL": 200.0})
    ib = FakeIB(cash=15.0)
    out = ia.submit(TODAY, ib=ib)
    assert ib.placed == [] and out["skipped"] == [{"symbol": "QQQM", "reason": "no_cash"}]
    assert _decision("AAPL")["execution"] is None


def test_unknown_contract_and_rejection_are_terminal(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "GLDM", "ACCUMULATE", 60)
    _verdict(home, "QQQM", "ACCUMULATE", 60)
    _prices(monkeypatch, {"GLDM": 50.0, "QQQM": 200.0})
    ib = FakeIB(unknown={"GLDM"}, status="Inactive")
    out = ia.submit(TODAY, ib=ib)
    reasons = {s["symbol"]: s["reason"] for s in out["skipped"]}
    assert reasons["GLDM"] == "not_tradable_at_ibkr"
    assert reasons["QQQM"].startswith("rejected: Inactive No trading permissions")
    assert _decision("QQQM")["execution"]["executed"] is False


def test_stale_quote_unclaims_for_retry(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 60)
    _prices(monkeypatch, {"QQQM": 200.0}, bar="2026-10-01")
    ib = FakeIB()
    assert ia.submit(TODAY, ib=ib)["skipped"] == [{"symbol": "QQQM", "reason": "stale_quote"}]
    _prices(monkeypatch, {"QQQM": 200.0})
    ia.submit(TODAY, ib=ib)
    assert len(ib.placed) == 1


# ---------------- reconcile ----------------

def test_reconcile_books_fill_and_fee_idempotently(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    f = _fill("QQQM", "BOT", 0.495, 201.5, "0001.01")
    ib = FakeIB(positions={"QQQM": 0.495}, fills=[f, _fill("AUD.USD", "BOT", 1, 0.65, "fx1", sec="CASH")])
    ib.cash = round(300 - 0.495 * 201.5 - 0.35, 2)
    out = ia.reconcile(ib=ib)
    assert out["status"] == "ok" and [b["exec_id"] for b in out["booked"]] == ["0001.01"]
    pm = PortfolioManager(home)
    assert pm.find_holding("QQQM")["units"] == pytest.approx(0.495)
    assert pm.cash_amount("USD") == pytest.approx(300 - 0.495 * 201.5 - 0.35, abs=0.01)   # 账本按分取整
    assert out["cash_drift_usd"] == pytest.approx(0, abs=0.01)
    again = ia.reconcile(ib=ib)
    assert again["booked"] == [] and PortfolioManager(home).find_holding("QQQM")["units"] == pytest.approx(0.495)
    assert [h["action"] for h in home.read_history()] == ["buy", "fee"]


def test_reconcile_position_drift_halts(home, monkeypatch, tmp_path):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "QQQM", "ACCUMULATE", 60)
    _prices(monkeypatch, {"QQQM": 200.0})
    ib = FakeIB(positions={"QQQM": 1.0})                     # 券商有、账本没有 → 有人在账户里动了手
    out = ia.reconcile(ib=ib)
    assert out["status"] == "halted" and (tmp_path / "STOP").exists()
    assert ia.submit(TODAY, ib=ib)["status"] == "halted" and ib.placed == []


# ---------------- pick ----------------

class _Agent:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self.reply


_INFO = {
    "QQQM": {"quoteType": "ETF", "currency": "USD", "exchange": "NMS", "longName": "Invesco NASDAQ 100 ETF",
             "shortName": "Invesco NASDAQ 100 ETF", "regularMarketPrice": 200.0, "averageDailyVolume3Month": 3e6},
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
    assert ia._check_tradable("VGSH", _INFO["VGSH"]) is None          # Short-Term 不是做空
    assert ia._check_tradable("TQQQ", _INFO["TQQQ"]) == "leveraged_or_inverse"
    assert ia._check_tradable("PENY", _INFO["PENY"]) == "price_below_min"
    assert ia._check_tradable("TINY", _INFO["TINY"]) == "illiquid"
    assert ia._check_tradable("600519.SS", {}) == "not_us_ticker"
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


# ---------------- CLI ----------------

def test_cli_daily_runs_stages_in_order_and_isolates_failure(monkeypatch, capfd):
    import openinvest.jobs.daily_report as dr
    import openinvest.jobs.verdict_review as vr
    calls = []
    monkeypatch.setattr(ia, "pick", lambda force=False: calls.append(("pick", force)) or {"status": "skipped"})
    monkeypatch.setattr(dr, "run", lambda send_email=True, include_report=False: calls.append(("daily_report", send_email)) or {})
    monkeypatch.setattr(ia, "submit", lambda: (_ for _ in ()).throw(ConnectionRefusedError("gateway down")))
    monkeypatch.setattr(vr, "run", lambda **kw: calls.append(("verdict_review",)) or {"status": "ok"})
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "ibkr_autopilot", "daily"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    assert calls == [("pick", False), ("daily_report", False), ("verdict_review",)]   # 日报绝不发邮件
    out = json.loads(capfd.readouterr().out)
    assert out["submit"]["status"] == "error" and "gateway down" in out["submit"]["error"]
