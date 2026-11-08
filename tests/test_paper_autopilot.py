"""jobs/paper_autopilot.py 测试：全自动模拟账户（默认关 / 整手成交 / 手续费 / 换汇 / 幂等 / 休市闸）
+ `autopilot_daily` 顺序 + experiments/paper_autopilot/clone_home.py。

全部在 tmp INVEST_HOME 里造持仓 + 当日裁决 md；行情 / 汇率 / TradesDB 都 monkeypatch，不联网。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

import openinvest.cli as cli
import openinvest.db.trades_db as trades_db_mod
import openinvest.jobs.paper_autopilot as ap
from openinvest.core import decision_ledger
from openinvest.core import memory_store as ms
from openinvest.core.config import API_SETTABLE, AutopilotConfig, load_config, reset_config, set_persisted_override
from openinvest.core.decision_ledger import list_decisions
from openinvest.core.memory_store import MemoryStore
import openinvest.core.portfolio_manager as pm_mod
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.quotes import QuoteSnapshot

TODAY = "2026-10-19"
CNY_POS = {"symbol": "510300.SS", "kind": "etf", "units": 300.0, "avg_cost": 5.0,
           "unit_label": "股", "cost_currency": "CNY", "proxy_kind": "direct"}


class _FakeTradesDB:
    def __init__(self, *a, **kw):
        pass

    def list_trades(self, limit=10000):
        return []


def _freeze_ledger_clock(monkeypatch):
    """list_decisions 的 days 窗口 / 7 天匹配窗、portfolio_history 的成交 ts 都按真实时钟算；
    TODAY 写死 → 钉到 TODAY 收盘后，否则过几天就找不到裁决 / 成交匹配不上"""
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(f"{TODAY}T18:00:00").replace(tzinfo=tz)
    for mod in (decision_ledger, pm_mod):
        monkeypatch.setattr(mod, "datetime", _FakeDT)


@pytest.fixture
def home(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)
    monkeypatch.setattr(trades_db_mod, "TradesDB", _FakeTradesDB)
    _freeze_ledger_clock(monkeypatch)
    reset_config()
    store = MemoryStore(mem)
    (mem / ".autopilot_origin.json").write_text(json.dumps({"cloned_at": TODAY}))
    return store


def _seed(store: MemoryStore, cash=None, holdings=None):
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Aggressive"}, "")
    store.write("strategy", "strategy", {"target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
                                          "target_assets": []}, "")
    positions = holdings if holdings is not None else [dict(CNY_POS)]
    if (store.root / ".autopilot_origin.json").exists():
        positions = [{**h, "channel": h.get("channel", "paper_autopilot")} for h in positions]
    store.write("portfolio", "state", {"cash": cash if cash is not None else {"CNY": 30000.0},
                                        "holdings": positions,
                                        "schema_version": 2}, "")


def _verdict(store: MemoryStore, symbol: str, verdict: str, alloc=None, date=TODAY):
    d = store.root / ".committee" / date
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# Committee: {symbol}", "", f"**Date**: {date}", f"**Symbol**: {symbol}",
             f"**Verdict**: {verdict} (confidence 0.70)"]
    if alloc is not None:
        lines.append(f"**Suggested allocation CNY**: {alloc:,}")
    (d / f"{ms_safe(symbol)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def ms_safe(symbol: str) -> str:
    from openinvest.utils.symbols import safe_symbol
    return safe_symbol(symbol)


def _market(monkeypatch, prices: dict, fx: dict | None = None):
    """prices: {symbol: (price, currency, bar_date | None, is_stale)}（缺省 bar_date=TODAY）"""
    seen = {}

    def fake_quote(holding):
        sym = holding["symbol"]
        seen[sym] = dict(holding)
        p = prices.get(sym)
        if p is None:
            return None
        price, ccy, *rest = p
        bar = rest[0] if rest else TODAY
        stale = rest[1] if len(rest) > 1 else False
        return QuoteSnapshot(symbol=sym, price=price, currency=ccy, unit="x", last_updated=bar, is_stale=stale)

    rates = {"CNY": 1.0, **(fx or {})}
    monkeypatch.setattr(ap, "get_quote", fake_quote)
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate",
                        lambda quote, base="CNY", as_of_date=None: rates.get(quote))
    return seen


def _enable(monkeypatch):
    monkeypatch.setenv("INVEST_AUTOPILOT_ENABLED", "true")
    reset_config()


def _pm(store):
    return PortfolioManager(store)


def _decision(did):
    return next(d for d in list_decisions(days=3) if d["decision_id"] == did)


# ---------------- 配置 ----------------

def test_config_defaults_and_whitelist():
    c = AutopilotConfig()
    assert (c.enabled, c.commission_bps, c.min_order_cny) == (False, 3.0, 500.0)
    assert load_config().autopilot == c
    assert API_SETTABLE["autopilot.enabled"]["type"] == "bool"
    assert API_SETTABLE["autopilot.commission_bps"]["type"] == "float"
    assert set_persisted_override("autopilot.enabled", True).autopilot.enabled is True


def test_disabled_is_noop(home, monkeypatch):
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    assert ap.run(TODAY) == {"status": "skipped", "reason": "autopilot_disabled"}
    pm = _pm(home)
    assert pm.cash_amount("CNY") == 30000.0 and pm.find_holding("510300.SS")["units"] == 300.0
    assert _decision(f"{TODAY}/510300.SS")["execution"] is None


def test_enabling_on_primary_home_cannot_mutate_or_relabel_real_ledger(home, monkeypatch):
    (home.root / ".autopilot_origin.json").unlink()
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    before = home.path_of("portfolio").read_bytes()
    monkeypatch.setattr(ap, "get_quote", lambda *args: pytest.fail("primary home must stop before quoting"))
    assert ap.run(TODAY) == {"status": "error", "reason": "paper_home_requires_independent_clone"}
    assert home.path_of("portfolio").read_bytes() == before and home.read_history() == []
    assert not (home.root / ".state" / "paper_autopilot.json").exists()


@pytest.mark.parametrize("contamination", ["domain", "history", "holding"])
def test_clone_marker_cannot_relabel_existing_real_ledger(home, monkeypatch, contamination):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    pm = _pm(home)
    if contamination == "domain":
        with pm.with_portfolio_tx() as p:
            p["ledger_domain"] = "client_reported"
    elif contamination == "history":
        pm.deposit_cash("CNY", 100, source="mcp")
    else:
        with pm.with_portfolio_tx() as p:
            positions = list(p["holdings"])
            positions[0]["channel"] = "user"
            p["holdings"] = positions
    before = home.path_of("portfolio").read_bytes()
    history_before = home.read_history()
    out = ap.run(TODAY)
    assert out["status"] == "error" and out["reason"].startswith("paper_home_has_non_simulated_")
    assert home.path_of("portfolio").read_bytes() == before and home.read_history() == history_before


# ---------------- 买 / 卖 ----------------

def test_accumulate_lot_buy_fee_and_execution(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3050)   # 3050/5=610 股 → 整手 600
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    out = ap.run(TODAY)
    assert out["status"] == "ok" and out["skipped"] == []
    f = out["filled"][0]
    assert (f["symbol"], f["side"], f["units"], f["price"]) == ("510300.SS", "buy", 600.0, 5.0)
    assert out["fees_cny"] == pytest.approx(0.9)   # 3000 × 3bps
    pm = _pm(home)
    assert pm.find_holding("510300.SS")["units"] == 900.0
    assert pm.cash_amount("CNY") == pytest.approx(30000 - 3000 - 0.9)
    hist = home.read_history()
    assert [h["action"] for h in hist] == ["buy", "fee"]
    assert hist[0]["source"] == "paper_autopilot" and hist[1]["cash_effect"] == -0.9
    assert all(h["execution_domain"] == "paper_simulation" for h in hist)
    assert all(h["evidence_basis"] == "paper_simulated_fill" for h in hist)
    assert all(h["fee_status"] == "estimated" for h in hist)
    assert all(h["source_reference"] == f"paper_autopilot:{TODAY}/510300.SS" for h in hist)
    d = _decision(f"{TODAY}/510300.SS")
    assert d["executed"] is None and d["execution"] is None
    assert d["matched_trades"] == [] and d["simulated_executed"] is True
    assert d["simulated_execution"]["reason"] == "paper_autopilot"
    persisted = json.loads((home.root / ".dreams" / "executions.jsonl").read_text().strip())
    assert persisted["executed"] is False and persisted["simulated_execution"] is True


def test_trim_sells_by_amount_and_sell_clears(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, holdings=[dict(CNY_POS, units=1000.0)])
    _verdict(home, "510300.SS", "TRIM", -1000)   # alloc 负数=方向；1000/5=200 股
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    f = ap.run(TODAY)["filled"][0]
    assert (f["side"], f["units"]) == ("sell", 200.0)
    pm = _pm(home)
    assert pm.find_holding("510300.SS")["units"] == 800.0
    assert pm.cash_amount("CNY") == pytest.approx(30000 + 1000 - 0.3)

    # 次日 SELL：清仓，不受 min_order 限制
    _verdict(home, "510300.SS", "SELL", -1, date="2026-10-20")
    _market(monkeypatch, {"510300.SS": (5.0, "CNY", "2026-10-20")})
    f2 = ap.run("2026-10-20")["filled"][0]
    assert f2["units"] == 800.0 and _pm(home).find_holding("510300.SS")["units"] == 0.0


def test_paper_book_keeps_domain_without_leaking_producer_scope(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    assert ap.run(TODAY)["filled"]
    _pm(home).deposit_cash("CNY", 100, source="mcp")
    activity = home.read_history()[-1]
    assert activity["source"] == "mcp" and activity["execution_domain"] == "paper_simulation"
    assert activity["evidence_basis"] == "client_reported_not_independently_verified"
    assert activity.get("source_reference") is None
    assert activity.get("fee_status") is None
    from openinvest.calc.ledger_replay import replay_ledger
    activities, _ = replay_ledger([activity], alias={})
    assert activities[0].external is True


def test_empty_paper_scope_does_not_relabel_a_client_book(tmp_path):
    store = MemoryStore(tmp_path / "client" / "memory")
    _seed(store)
    pm = _pm(store)
    with pm.with_portfolio_tx() as portfolio:
        portfolio["ledger_domain"] = "client_reported"
    before = store.path_of("portfolio").read_bytes()
    with pm.recording_scope(
        evidence_basis="paper_simulated_fill", execution_domain="paper_simulation",
        source_reference="temporary-paper-scope", fee_status="estimated", trade_date=TODAY,
    ):
        pass
    assert store.path_of("portfolio").read_bytes() == before and store.read_history() == []
    pm.deposit_cash("CNY", 100, source="mcp")
    activity, = store.read_history()
    assert activity["source"] == "mcp" and activity["execution_domain"] == "client_reported"
    assert activity["evidence_basis"] == "client_reported_not_independently_verified"
    assert activity.get("source_reference") is None and activity.get("fee_status") is None


def test_sell_without_position_is_skipped_and_recorded(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, holdings=[])
    _verdict(home, "510300.SS", "SELL", -3000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    out = ap.run(TODAY)
    assert out["filled"] == [] and out["skipped"] == [{"symbol": "510300.SS", "reason": "no_position"}]
    ex = _decision(f"{TODAY}/510300.SS")["simulated_execution"]
    assert ex["executed"] is False and ex["reason"] == "paper_autopilot:no_position"


def test_otc_fund_unsupported_is_recorded_not_filled(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "FUND:000001", "ACCUMULATE", 3000)
    _market(monkeypatch, {})   # 不该取价
    out = ap.run(TODAY)
    assert out["filled"] == [] and out["skipped"] == [{"symbol": "FUND:000001", "reason": "unsupported_otc_fund"}]
    ex = _decision(f"{TODAY}/FUND:000001")["simulated_execution"]
    assert ex["executed"] is False and ex["reason"] == "paper_autopilot:unsupported_otc_fund"
    assert home.read_history() == [] and _pm(home).cash_amount("CNY") == 30000.0


def test_hold_and_no_alloc_do_nothing(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "HOLD", -5000)
    _verdict(home, "159949.SZ", "ACCUMULATE")   # 没 alloc
    _market(monkeypatch, {"510300.SS": (5.0, "CNY"), "159949.SZ": (1.0, "CNY")})
    out = ap.run(TODAY)
    assert out["filled"] == [] and out["skipped"] == [{"symbol": "159949.SZ", "reason": "no_alloc"}]
    assert _decision(f"{TODAY}/510300.SS")["execution"] is None
    assert _decision(f"{TODAY}/159949.SZ")["execution"] is None
    assert home.read_history() == [] and _pm(home).cash_amount("CNY") == 30000.0


# ---------------- 现金 / 门槛 ----------------

def test_cash_shortfall_shrinks_then_no_cash(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash={"CNY": 2000.0})
    _verdict(home, "510300.SS", "ACCUMULATE", 5000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    f = ap.run(TODAY)["filled"][0]
    assert f["units"] == 300.0                      # 2000 现金（含手续费）→ 整手只够 300 股
    assert _pm(home).cash_amount("CNY") >= 0

    _seed(home, cash={"CNY": 100.0})                # 同日另一账户：钱不够一手
    _verdict(home, "159949.SZ", "ACCUMULATE", 5000)
    _market(monkeypatch, {"159949.SZ": (1.0, "CNY")})
    ap.run(TODAY)   # 510300 已有 execution 被过滤；159949 一手要 100 元 + 手续费 > 100
    assert _decision(f"{TODAY}/159949.SZ")["simulated_execution"]["reason"] == "paper_autopilot:no_cash"
    assert _pm(home).cash_amount("CNY") == 100.0


def test_min_order_gate(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 300)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    out = ap.run(TODAY)
    assert out["skipped"] == [{"symbol": "510300.SS", "reason": "too_small"}] and home.read_history() == []
    assert _decision(f"{TODAY}/510300.SS")["simulated_execution"]["simulated_execution"] is False


def test_aud_symbol_auto_fx_two_legs_with_idempotency_keys(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash={"CNY": 30000.0, "AUD": 0.0}, holdings=[])
    _verdict(home, "NDQ.AX", "ACCUMULATE", 2000)    # 2000/4.7=425.5 AUD → 10 股 @40 = 400 AUD
    _market(monkeypatch, {"NDQ.AX": (40.0, "AUD")}, fx={"AUD": 4.7})
    out = ap.run(TODAY)
    f = out["filled"][0]
    assert (f["units"], f["currency"]) == (10.0, "AUD") and f["amount_cny"] == pytest.approx(1880.0)
    pm = _pm(home)
    assert pm.find_holding("NDQ.AX")["cost_currency"] == "AUD"   # 无持仓：币种按后缀猜
    assert pm.cash_amount("AUD") == pytest.approx(0.01, abs=0.011)   # 换入 = 成本 + 手续费 + 1 分余量
    assert pm.cash_amount("CNY") == pytest.approx(30000 - 400.13 * 4.7, abs=0.02)
    keys = pm.portfolio.metadata["ledger_idempotency_keys"]
    for k in (f"paper-fx-{TODAY}-NDQ.AX-AUD-out", f"paper-fx-{TODAY}-NDQ.AX-AUD-in",
              f"paper-{TODAY}-NDQ.AX-buy", f"paper-fee-{TODAY}-NDQ.AX-buy"):
        assert k in keys
    acts = [(h["action"], h.get("symbol"), h.get("source")) for h in home.read_history()]
    assert acts[:2] == [("withdraw", "CNY", "paper_autopilot"), ("deposit", "AUD", "paper_autopilot")]
    assert all(h["execution_domain"] == "paper_simulation" for h in home.read_history())
    assert all(h["evidence_basis"] == "paper_simulated_fill" for h in home.read_history())
    fx_rows = home.read_history()[:2]
    assert all(h["source_reference"]["kind"] == "fx_conversion" for h in fx_rows)
    from openinvest.calc.ledger_replay import replay_ledger, external_flow
    activities, _ = replay_ledger(fx_rows, alias={})
    assert all(not activity.external and external_flow(activity) == 0 for activity in activities)
    assert _pm(home).portfolio.get("ledger_domain") == "paper_simulation"


def test_gold_without_holding_uses_cny_per_gram(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, holdings=[])
    _verdict(home, "GC=F", "BUY", 1500)
    seen = _market(monkeypatch, {"GC=F": (880.0, "CNY", None)})   # 黄金 proxy 不带 bar 日期
    f = ap.run(TODAY)["filled"][0]
    assert f["units"] == 1.7045                                  # 1500/880 向下取 4 位
    assert seen["GC=F"]["proxy_kind"] == "gold_cny_per_gram"
    h = _pm(home).find_holding("GC=F")
    assert (h["cost_currency"], h["unit_label"], h["kind"]) == ("CNY", "克", "metal")


# ---------------- 幂等 ----------------

def test_rerun_same_day_does_not_double_book(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})
    ap.run(TODAY)
    snap = (_pm(home).cash_amount("CNY"), _pm(home).find_holding("510300.SS")["units"])
    assert ap.run(TODAY)["filled"] == []                      # 已有 execution 记录 → 不再处理

    (home.root / ".dreams" / "executions.jsonl").unlink()     # 抹掉 execution：claim 闸兜底
    assert ap.run(TODAY)["skipped"] == [{"symbol": "510300.SS", "reason": "already_done_today"}]

    (home.root / ".state" / "paper_autopilot.json").unlink()  # 再抹 claim：账本 idempotency_key 兜底
    (home.root / ".dreams" / "executions.jsonl").unlink(missing_ok=True)
    out = ap.run(TODAY)
    assert out["filled"] == [] and out["skipped"][0]["reason"] == "duplicate_key"
    assert (_pm(home).cash_amount("CNY"), _pm(home).find_holding("510300.SS")["units"]) == snap
    assert [h["action"] for h in home.read_history()] == ["buy", "fee"]
    # duplicate_key 不写新反馈，模拟匹配仍可恢复，但不能升级成用户真实采纳。
    d = _decision(f"{TODAY}/510300.SS")
    assert d["execution"] is None and d["executed"] is None
    assert d["simulated_execution"] is None and d["simulated_executed"] is True


def test_currency_mismatch_is_terminal_skip(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)   # 510300.SS 持仓按 CNY 计价
    _verdict(home, "510300.SS", "TRIM", -1000)
    _market(monkeypatch, {"510300.SS": (5.0, "USD")}, fx={"USD": 7.0})
    assert ap.run(TODAY)["skipped"] == [{"symbol": "510300.SS", "reason": "currency_mismatch"}]
    assert home.read_history() == []


def test_buy_failure_after_fx_rolls_back_conversion(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home, cash={"CNY": 30000.0, "AUD": 0.0}, holdings=[])
    _verdict(home, "NDQ.AX", "ACCUMULATE", 2000)
    _market(monkeypatch, {"NDQ.AX": (40.0, "AUD")}, fx={"AUD": 4.7})

    def boom(self, *a, **kw):
        raise ValueError("boom")
    monkeypatch.setattr(PortfolioManager, "buy", boom)
    out = ap.run(TODAY)
    assert out["skipped"] == [{"symbol": "NDQ.AX", "reason": "buy_failed"}]
    pm = _pm(home)
    assert pm.cash_amount("CNY") == pytest.approx(30000.0) and pm.cash_amount("AUD") == pytest.approx(0.0)
    assert _decision(f"{TODAY}/NDQ.AX")["simulated_execution"]["reason"] == "paper_autopilot:buy_failed"
    assert all(h["execution_domain"] == "paper_simulation" for h in home.read_history())
    assert [h["source_reference"]["kind"] for h in home.read_history()] == [
        "fx_conversion", "fx_conversion", "fx_conversion_rollback", "fx_conversion_rollback"]
    from openinvest.calc.ledger_replay import replay_ledger, external_flow
    activities, _ = replay_ledger(home.read_history(), alias={})
    assert all(not a.external and external_flow(a) == 0 for a in activities)


# ---------------- 休市 / 陈旧行情 ----------------

def test_no_bar_today_skips_and_retries_later(home, monkeypatch):
    _enable(monkeypatch)
    _seed(home)
    _verdict(home, "510300.SS", "ACCUMULATE", 3000)
    _market(monkeypatch, {"510300.SS": (5.0, "CNY", "2026-10-16")})
    out = ap.run(TODAY)
    assert out["skipped"] == [{"symbol": "510300.SS", "reason": "market_closed"}]
    assert home.read_history() == [] and _pm(home).cash_amount("CNY") == 30000.0
    assert _decision(f"{TODAY}/510300.SS")["execution"] is None   # 没记 execution

    _market(monkeypatch, {"510300.SS": (5.0, "CNY", "2026-10-16", True)})   # 行情源没拉到
    assert ap.run(TODAY)["skipped"][0]["reason"] == "stale_quote"

    _market(monkeypatch, {"510300.SS": (5.0, "CNY")})   # unclaim 过 → bar 到了当日可补成交
    assert ap.run(TODAY)["filled"][0]["units"] == 600.0


# ---------------- autopilot_daily ----------------

def test_autopilot_daily_runs_three_stages_in_order(monkeypatch, capfd):
    import openinvest.jobs.daily_report as dr
    import openinvest.jobs.verdict_review as vr
    calls = []
    monkeypatch.setattr(dr, "run", lambda send_email=True, include_report=False: calls.append(("daily_report", send_email)) or {"status": "success"})
    monkeypatch.setattr(ap, "run", lambda today=None: calls.append(("paper_autopilot",)) or {"status": "ok"})
    monkeypatch.setattr(vr, "run", lambda **kw: calls.append(("verdict_review",)) or {"status": "ok"})
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "autopilot_daily"])
    cli.main()
    assert calls == [("daily_report", False), ("paper_autopilot",), ("verdict_review",)]   # 日报绝不发邮件
    out = json.loads(capfd.readouterr().out)
    assert set(out) == {"daily_report", "paper_autopilot", "verdict_review"}


def test_autopilot_daily_isolates_stage_failure(monkeypatch, capfd):
    import openinvest.jobs.daily_report as dr
    import openinvest.jobs.verdict_review as vr

    def boom(**kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(dr, "run", boom)
    monkeypatch.setattr(ap, "run", lambda today=None: {"status": "ok"})
    monkeypatch.setattr(vr, "run", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "argv", ["openinvest", "autopilot_daily"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    out = json.loads(capfd.readouterr().out)
    assert out["daily_report"]["status"] == "error" and out["verdict_review"]["status"] == "ok"


# ---------------- clone_home ----------------

def _load_clone():
    p = Path(__file__).resolve().parents[1] / "experiments" / "paper_autopilot" / "clone_home.py"
    spec = importlib.util.spec_from_file_location("clone_home", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_clone_home(tmp_path, monkeypatch):
    clone_home = _load_clone()
    monkeypatch.setattr(clone_home, "_total_cny", lambda pm: 12345.0)
    src, dst = tmp_path / "src", tmp_path / "dst"
    s = MemoryStore(src / "memory")
    _seed(s, cash={"CNY": 1000.0, "AUD": 50.0})
    s.write("portfolio", "state", {**s.read("portfolio").metadata, "ledger_idempotency_keys": ["old"]}, "")
    (src / "memory" / ".committee" / TODAY).mkdir(parents=True)
    (src / "memory" / ".committee" / TODAY / "x.md").write_text("secret")
    (src / "memory" / "portfolio_history.jsonl").write_text("{}\n")

    origin = clone_home.clone(src, dst)
    mem = dst / "memory"
    assert origin["src_total_cny"] == 12345.0
    assert json.loads((mem / ".autopilot_origin.json").read_text())["src_total_cny"] == 12345.0
    assert (mem / "user.md").read_bytes() == (src / "memory" / "user.md").read_bytes()
    assert not (mem / ".committee").exists() and not (mem / "portfolio_history.jsonl").exists()
    pm = PortfolioManager(MemoryStore(mem))
    assert pm.cash == {"CNY": 1000.0, "AUD": 50.0}
    assert [h["channel"] for h in pm.holdings] == ["paper_autopilot"]
    assert "ledger_idempotency_keys" not in pm.portfolio.metadata
    with pytest.raises(SystemExit):   # 拒绝覆盖
        clone_home.clone(src, dst)
