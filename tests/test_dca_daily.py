"""jobs/dca_daily.py 测试：每日定投跟踪投影 + 幂等，不改变正式账本。

覆盖：
- auto_dca_enabled=False → 跳过，不动账本
- 启用后按 amount_cny 保存独立 tracking_estimate；持仓、cash、history 和实际流量均不动
- 同日同 symbol/plan 只有一份完整不可变收据；早claim不能代表完成（ADR-016）
- 拉不到价 → 跳过，不留下完成标志（同日可重试）
- 休市（最新 bar 不是今天：节假日/周末）→ skip(market_closed)、不动账本、可重试
- 行情源没拉到（is_stale）→ skip(stale_quote)；同日 18:30 / 21:30 重跑补记且只记一次
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openinvest.core.config import reset_config
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.quotes import QuoteSnapshot
import openinvest.jobs.dca_daily as dca


def _seed(tmp_path: Path, cash=None, holdings=None) -> MemoryStore:
    """临时 memory 里 seed user/strategy/portfolio，避免污染真实 memory/"""
    s = MemoryStore(tmp_path / "memory")
    s.write("user", "user", {"display_name": "T", "risk_tolerance": "Aggressive",
                              "exchange_buffer_cny": 0.0}, "")
    s.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "510300.SS", "max_single_invest_cny": 6000.0}],
    }, "")
    s.write("portfolio", "state", {
        "cash": cash if cash is not None else {"CNY": 30000.0},
        "holdings": holdings if holdings is not None else [],
        "schema_version": 2,
    }, "")
    return s


@pytest.fixture(autouse=True)
def _reset_config_each():
    """env 改动需 reset config 缓存才生效；前后都 reset 防跨 test 污染"""
    reset_config()
    yield
    reset_config()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """tmp memory（已 seed 510300 300 股 @5.0 + ¥30k 现金）+ 把 PortfolioManager() 指过去"""
    s = _seed(tmp_path, cash={"CNY": 30000.0}, holdings=[
        {"symbol": "510300.SS", "kind": "etf", "units": 300.0, "avg_cost": 5.0,
         "unit_label": "股", "cost_currency": "CNY", "proxy_kind": "direct"},
    ])
    from openinvest.core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
    return s


@pytest.fixture(autouse=True)
def _formal_ledger_unchanged_in_dca_tests(request):
    # Keep this module's invariant separate from the reusable seeded fixture:
    # other source-integrity suites intentionally use that fixture to add rows.
    if "seeded" not in request.fixturenames:
        yield
        return
    s = request.getfixturevalue("seeded")
    portfolio = s.path_of("portfolio")
    before = portfolio.read_bytes()
    yield
    # Every DCA branch, including retries, must leave formal financial state intact.
    assert portfolio.read_bytes() == before
    assert s.read_history() == []
    assert not (s.root / "portfolio_history.jsonl").exists()


def _estimates(store):
    return [json.loads(path.read_text())["payload"]
            for path in (store.root / ".evidence/tracking_estimates").glob("*.json")]


def _fixed_quote(price=5.0, bar_date=None, is_stale=False):
    return lambda holding: QuoteSnapshot(
        symbol=str(holding.get("symbol")), price=price, currency="CNY", unit="股",
        last_updated=bar_date, is_stale=is_stale)


def _sh_date(days_ago=0) -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_disabled_skips(seeded, monkeypatch):
    """auto_dca_enabled 默认 False → 跳过，账本不动"""
    monkeypatch.delenv("INVEST_DCA_AUTO_DCA_ENABLED", raising=False)
    out = dca.run()
    assert out["status"] == "skipped"
    assert out["reason"] == "auto_dca_disabled"
    pm = PortfolioManager(seeded)
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)
    assert pm.find_holding("510300.SS")["units"] == 300.0


def test_enabled_no_symbols_skips(seeded, monkeypatch):
    """启用但没配 symbols → 跳过"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.delenv("INVEST_DCA_AUTO_DCA_SYMBOLS", raising=False)
    out = dca.run()
    assert out["status"] == "skipped"
    assert out["reason"] == "no_dca_symbols"


def test_enabled_records_only_a_tracking_estimate(seeded, monkeypatch):
    """¥100/5.0=20 股只是投影；不能据此断言成交、增加持仓或写真实外部流。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "100")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out = dca.run()
    assert out["status"] == "success"
    r = out["results"][0]
    assert r["status"] == "estimated" and r["projected_quantity"] == pytest.approx(20.0)
    assert r["execution_observed"] is False and r["portfolio_mutated"] is False

    pm = PortfolioManager(seeded)
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)         # 子弹池现金不动
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(300.0)
    estimate, = _estimates(seeded)
    assert estimate["quote"]["projected_quantity"] == 20
    assert estimate["actual_quantity"] is None and estimate["actual_fee"] is None
    assert estimate["execution_observed"] is False and estimate["cash_flow_recorded"] is False


def test_idempotent_same_day(seeded, monkeypatch):
    """同日同计划跑两次：只保存第一份报价投影，持仓不增加。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    first = dca.run()
    monkeypatch.setattr(dca, "get_quote", lambda holding: pytest.fail("a completed receipt must avoid a new quote request"))
    out2 = dca.run()
    assert out2["results"][0]["status"] == "skipped"
    assert out2["results"][0]["reason"] == "already_projected_this_plan_today"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(300.0)
    estimate, = _estimates(seeded)
    assert estimate["estimate_id"] == first["results"][0]["estimate_id"]
    assert estimate["quote"]["price"] == 5.0 and estimate["quote"]["projected_quantity"] == 20
    assert out2["results"][0]["price"] == 5.0 and out2["results"][0]["projected_quantity"] == 20


def test_changed_plan_creates_a_separate_projection_without_summing_into_holdings(seeded, monkeypatch):
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "100")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    first = dca.run()["results"][0]
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "200")
    revised = dca.run()["results"][0]
    assert first["status"] == revised["status"] == "estimated"
    assert first["estimate_id"] != revised["estimate_id"]
    assert (first["projected_quantity"], revised["projected_quantity"]) == (20, 40)
    assert len(_estimates(seeded)) == 2
    assert PortfolioManager(seeded).find_holding("510300.SS")["units"] == 300


def test_unheld_symbol_skipped(seeded, monkeypatch):
    """未持有的 symbol → 跳过(not_tracked)：不猜币种（避免把 USD 价当 CNY 记错账）"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "AAPL")  # 未持有
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(200.0))  # 即便能取价也不该记
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "not_tracked"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("AAPL") is None
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)


def test_units_too_small_skipped_without_marking_completion(seeded, monkeypatch):
    """金额/价格使 units 舍入为 0 → 跳过(amount_too_small)，不能产生完成标志。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "0.000001")  # /5 → round6 = 0
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "amount_too_small"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0  # 没买


def test_quote_exception_isolated_not_aborting(seeded, monkeypatch):
    """取价抛异常 → 记 error 并继续（不 raise 中断整批）；同日可重试。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")

    def boom(h):
        raise RuntimeError("yf down")
    monkeypatch.setattr(dca, "get_quote", boom)
    out = dca.run()                       # 不应抛
    assert out["status"] == "success"
    assert out["results"][0]["status"] == "error"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0  # 没买

    # 同日重试：有价 → 投影，仍没有实际成交
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    assert dca.run()["results"][0]["status"] == "estimated"
    assert len(_estimates(seeded)) == 1


def test_no_price_skips_without_marking_completion(seeded, monkeypatch):
    """拉不到价 → 跳过；同日有价后能补投影（不存在提前落盘的完成标志）。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", lambda h: None)
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "no_price"

    assert _estimates(seeded) == []
    # 同日重试：这次有价 → 投影
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out2 = dca.run()
    assert out2["results"][0]["status"] == "estimated"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(300.0)
    assert len(_estimates(seeded)) == 1


def test_market_closed_stale_bar_skips_without_ledger_write(seeded, monkeypatch):
    """2026-10 国庆事故：休市日 yfinance 最新 bar 是节前 → 不得按旧价记账。

    skip(market_closed)：持仓 / 流水 / 幂等闸全不动（三次运行都一样）。
    """
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date(days_ago=1)))
    history_before = len(PortfolioManager(seeded).store.read_history())

    for _ in range(3):
        out = dca.run()
        assert out["results"][0] == {"symbol": "510300.SS", "status": "skipped",
                                     "reason": "market_closed"}
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0
    assert len(pm.store.read_history()) == history_before
    assert pm.store.state_get("dca_estimated", []) == []   # 没有早claim
    assert _estimates(seeded) == []


def test_trading_day_retry_projects_exactly_once(seeded, monkeypatch):
    """交易日 15:30 行情源抖动（旧 bar + is_stale）→ stale_quote 跳过；18:30 拿到今天的 bar
    → 投影一次；21:30 → already_projected_this_plan_today。整天没有正式成交（ADR-016）。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    history_before = len(PortfolioManager(seeded).store.read_history())

    monkeypatch.setattr(dca, "get_quote",
                        _fixed_quote(5.0, bar_date=_sh_date(days_ago=1), is_stale=True))
    assert dca.run()["results"][0]["reason"] == "stale_quote"          # 15:30
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    assert dca.run()["results"][0]["status"] == "estimated"                           # 18:30
    assert dca.run()["results"][0]["reason"] == "already_projected_this_plan_today"    # 21:30

    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(300.0)
    assert len(pm.store.read_history()) == history_before
    assert len(_estimates(seeded)) == 1


def _enable_dca(monkeypatch):
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "100")


def test_old_claim_without_a_receipt_cannot_skip_the_missing_audit_projection(seeded, monkeypatch):
    from openinvest.core.evidence_eligibility import digest
    _enable_dca(monkeypatch)
    # Reproduce the old crash artifact exactly. It is not evidence of a completed write.
    legacy_plan = {"symbol": "510300.SS", "amount_cny": 100.0, "purpose": "tracking_projection"}
    seeded.state_claim("dca_estimated", f"{_sh_date()}:510300.SS:{digest(legacy_plan)}")
    assert _estimates(seeded) == []
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    result = dca.run()["results"][0]
    assert result["status"] == "estimated" and result["projected_quantity"] == 20
    assert len(_estimates(seeded)) == 1


def test_process_death_before_first_receipt_leaves_no_false_completion_and_can_retry(seeded, monkeypatch):
    from openinvest.services import ledger_evidence
    _enable_dca(monkeypatch)
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    with monkeypatch.context() as scoped:
        scoped.setattr(ledger_evidence, "record_tracking_estimate",
                       lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("synthetic process death before receipt")))
        with pytest.raises(SystemExit, match="before receipt"):
            dca.run()
    assert _estimates(seeded) == [] and seeded.state_get("dca_estimated", []) == []
    result = dca.run()["results"][0]
    assert result["status"] == "estimated" and result["projected_quantity"] == 20
    assert len(_estimates(seeded)) == 1


def test_process_death_after_receipt_before_response_replays_the_first_quote_without_fetching(seeded, monkeypatch):
    from openinvest.services import ledger_evidence
    _enable_dca(monkeypatch)
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    original = ledger_evidence.record_tracking_estimate

    def write_then_die(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("synthetic process death after receipt")

    with monkeypatch.context() as scoped:
        scoped.setattr(ledger_evidence, "record_tracking_estimate", write_then_die)
        with pytest.raises(SystemExit, match="after receipt"):
            dca.run()
    path, = (seeded.root / ".evidence/tracking_estimates").glob("*.json")
    before = path.read_bytes()
    monkeypatch.setattr(dca, "get_quote", lambda holding: pytest.fail("committed audit receipt must be sufficient"))
    replay = dca.run()["results"][0]
    assert replay["reason"] == "already_projected_this_plan_today"
    assert replay["price"] == 5.0 and replay["projected_quantity"] == 20
    assert path.read_bytes() == before and len(_estimates(seeded)) == 1


def test_concurrent_runs_have_one_complete_receipt_and_all_responses_use_its_quote(seeded, monkeypatch):
    _enable_dca(monkeypatch)
    barrier = threading.Barrier(8)
    guard, requested = threading.Lock(), []

    def quote(holding):
        with guard:
            price = 5.0 + len(requested)
            requested.append(price)
        barrier.wait(timeout=10)
        return _fixed_quote(price, bar_date=_sh_date())(holding)

    monkeypatch.setattr(dca, "get_quote", quote)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: dca.run()["results"][0], range(8)))
    estimate, = _estimates(seeded)
    assert len(requested) == 8 and estimate["quote"]["price"] in (5, 6, 7, 8, 9, 10, 11, 12)
    assert sum(row["status"] == "estimated" for row in results) == 1
    assert sum(row["status"] == "skipped" for row in results) == 7
    assert {row["estimate_id"] for row in results} == {estimate["estimate_id"]}
    assert all(row["price"] == estimate["quote"]["price"] for row in results)
    assert all(row["projected_quantity"] == estimate["quote"]["projected_quantity"] for row in results)


def test_same_plan_changed_quote_preserves_the_original_bytes_and_result(seeded, monkeypatch):
    _enable_dca(monkeypatch)
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    first = dca.run()["results"][0]
    path, = (seeded.root / ".evidence/tracking_estimates").glob("*.json")
    before = path.read_bytes()
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(4.0, bar_date=_sh_date()))
    replay = dca.run()["results"][0]
    assert replay["reason"] == "already_projected_this_plan_today"
    assert replay["price"] == first["price"] == 5.0
    assert replay["projected_quantity"] == first["projected_quantity"] == 20
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["invalid_json", "wrong_hash", "invalid_quote", "incomplete_quote"])
def test_corrupt_receipt_is_not_completion_or_permission_to_fetch_and_replace_it(seeded, monkeypatch, damage):
    from openinvest.core.evidence_eligibility import digest
    _enable_dca(monkeypatch)
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    dca.run()
    path, = (seeded.root / ".evidence/tracking_estimates").glob("*.json")
    envelope = json.loads(path.read_text())
    if damage == "invalid_json":
        path.write_text("{")
    else:
        if damage == "wrong_hash":
            envelope["sha256"] = "0" * 64
        elif damage == "invalid_quote":
            envelope["payload"]["quote"]["price"] = 0
            envelope["sha256"] = digest(envelope["payload"])
        else:
            del envelope["payload"]["quote"]["projected_quantity"]
            envelope["sha256"] = digest(envelope["payload"])
        path.write_text(json.dumps(envelope))
    corrupt_bytes = path.read_bytes()
    monkeypatch.setattr(dca, "get_quote", lambda holding: pytest.fail("corrupt receipts require reconciliation"))
    result = dca.run()["results"][0]
    assert result["status"] == "error" and "reason" in result
    assert path.read_bytes() == corrupt_bytes
    assert len(list((seeded.root / ".evidence/tracking_estimates").glob("*.json"))) == 1


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1, 0, None, True])
def test_invalid_or_nonfinite_price_cannot_write_a_completion_receipt(seeded, monkeypatch, price):
    _enable_dca(monkeypatch)
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(price, bar_date=_sh_date()))
    result = dca.run()["results"][0]
    assert result["status"] == "skipped" and result["reason"] == "no_price"
    assert _estimates(seeded) == []


def test_schedule_runs_three_times_after_close():
    """yml 排程：平日北京 15:30 / 18:30 / 21:30（收盘后首跑 + 两次补记机会），周末不跑。"""
    import yaml
    from openinvest.scheduler.cron import crontab_trigger
    cfg = yaml.safe_load((Path(dca.__file__).with_suffix(".yml")).read_text(encoding="utf-8"))
    trig = crontab_trigger(cfg["schedule"], timezone=cfg["timezone"])
    now = datetime(2026, 10, 9, 0, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周五
    fires = []
    for _ in range(4):
        nxt = trig.get_next_fire_time(None, now)
        fires.append(nxt.strftime("%a %H:%M"))
        now = nxt + timedelta(seconds=1)
    assert fires == ["Fri 15:30", "Fri 18:30", "Fri 21:30", "Mon 15:30"]
