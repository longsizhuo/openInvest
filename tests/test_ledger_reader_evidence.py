"""Shared readers cannot promote unknown balances or costs into actual P&L."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.balance_observations import record_observation
from openinvest.services.ledger_evidence import account_source_status, record_tracking_estimate
from openinvest.services.skill_views import build_status_view
from openinvest.utils.portfolio_summary import portfolio_summary_text
from openinvest.utils.quotes import QuoteSnapshot


PRICES = {"GOLD": 150.0, "ETF": 40.0}


@pytest.fixture
def pm(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory")
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT", store.root)
    store.write("user", "user", {"display_name": "Synthetic", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 1000.0}, "holdings": [
        {"symbol": "GOLD", "kind": "metal", "units": 10.0, "avg_cost": 100.0,
         "cost_currency": "CNY", "unit_label": "克", "proxy_kind": "gold_cny_per_gram", "yfinance_proxy": "GC=F"},
        {"symbol": "ETF", "kind": "etf", "units": 5.0, "avg_cost": 20.0,
         "cost_currency": "CNY", "unit_label": "股", "proxy_kind": "direct"},
    ]}, "")
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", lambda holding: QuoteSnapshot(
        symbol=holding["symbol"], price=PRICES[holding["symbol"]], currency="CNY", unit="x",
        last_updated="2026-11-01", is_stale=False))
    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm: {"status": "insufficient_history"})
    return PortfolioManager(store)


def _summary(pm):
    return portfolio_summary_text(pm, total_assets=2700.0, current_prices=PRICES)


def _estimate(pm):
    record_tracking_estimate(pm.store, symbol="GOLD", day=datetime.now(timezone.utc).date().isoformat(),
                             plan={"amount": 100}, quote={"projected_quantity": 11})


def _observe(pm, **fields):
    stamp = datetime.now(timezone.utc).isoformat()
    receipt = record_observation(pm, "GOLD", as_of=stamp, source_text="合成账户资料，按声明字段记录。",
                                 source_locator="synthetic:reader-test", idempotency_key="reader-observation", **fields)
    assert receipt["status"] == "current", receipt["field_disposition"]
    return stamp


def test_source_integrity_failure_is_unknown_across_rows_gold_and_summary(pm, monkeypatch):
    directory = pm.store.root / ".state"
    directory.mkdir(exist_ok=True)
    (directory / "account_observation_heads.json").write_text('{"broken":true}')
    assert account_source_status(pm)["status"] == "unavailable"
    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm: pytest.fail("unavailable evidence cannot request actual performance"))
    view = build_status_view()
    assert "来源资格" in view["source_warning"]
    for row in view["all_holdings"]:
        assert row["units"] is None and row["avg_cost"] is None
        assert row["pnl"] is None and row["pnl_pct"] is None
        assert row["market_value"] is None and row["market_value_cny"] is None
        assert row["source_evidence"]["current_quantity"] == "unknown"
        assert row["tracking_projection"]["units"] > 0
    gold = view["gold"]
    assert all(gold[field] is None for field in (
        "grams", "avg_cost_cny_per_gram", "value_cny", "pnl_cny", "pnl_pct"))
    assert view["total_assets_cny"] is None and view["cash"]["cny"] is None
    assert view["performance"]["financial_performance_eligible"] is False
    text = _summary(pm)
    assert text.startswith("[QUALITY_ALERT]")
    assert "10.0000" not in text and "100.00" not in text and "+500.00" not in text
    assert "5.0000" not in text and "20.00" not in text
    assert "现金: 未知" in text and "¥0" not in text


def test_missing_row_grade_does_not_default_to_client_recorded(pm, monkeypatch):
    monkeypatch.setattr("openinvest.services.ledger_evidence.account_source_status", lambda pm: {
        "status": "client_recorded", "holdings": {}, "ledger_domain": "client_reported",
    })
    view = build_status_view()
    assert "source_warning" in view
    assert all(row["units"] is None and row["avg_cost"] is None for row in view["all_holdings"])
    assert view["gold"]["grams"] is None and view["gold"]["pnl_cny"] is None
    assert "10.0000" not in _summary(pm) and "100.00" not in _summary(pm)


def test_confirmed_quantity_does_not_promote_an_unconfirmed_cost_into_pnl(pm):
    _estimate(pm)
    stamp = _observe(pm, reported_quantity=10, quantity_unit="克", native_instrument="GOLD")
    grade = account_source_status(pm)["holdings"]["GOLD"]
    assert grade["current_quantity"] == "client_observed_balance"
    assert grade["cost_basis"] == "estimated_or_proxy_unreconciled"
    view = build_status_view()
    row = next(row for row in view["all_holdings"] if row["symbol"] == "GOLD")
    assert row["units"] == 10 and row["avg_cost"] is None
    assert row["pnl"] is None and row["pnl_pct"] is None
    assert row["source_evidence"]["balance_observations"]["current_quantity"]["as_of"] == stamp
    assert row["source_evidence"]["independent_broker_verification"] is False
    assert view["gold"]["grams"] == 10 and view["gold"]["avg_cost_cny_per_gram"] is None
    assert view["gold"]["pnl_cny"] is None and view["gold"]["pnl_pct"] is None
    text = _summary(pm)
    assert "10.0000 克" in text and "成本来源待核" in text
    assert "均价 100.00" not in text and "+500.00" not in text and stamp in text


def test_reported_value_preserves_as_of_without_inferring_quantity_or_cost(pm):
    _estimate(pm)
    stamp = _observe(pm, reported_value=1234.56, value_currency="CNY")
    view = build_status_view()
    row = next(row for row in view["all_holdings"] if row["symbol"] == "GOLD")
    assert row["units"] is None and row["avg_cost"] is None and row["market_value"] is None
    observed = row["source_evidence"]["balance_observations"]["current_value"]
    assert observed["as_of"] == stamp and observed["reported"]["value"] == 1234.56
    assert view["gold"]["grams"] is None and view["gold"]["pnl_cny"] is None
    text = _summary(pm)
    assert stamp in text and "1234.56" in text
    assert "10.0000" not in text and "均价" not in next(line for line in text.splitlines() if "**GOLD**" in line)


def test_unknown_cost_cannot_return_through_a_missing_price_total_fallback(pm, monkeypatch):
    _estimate(pm)
    _observe(pm, reported_quantity=10, quantity_unit="克", native_instrument="GOLD")
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", lambda holding: None)
    view = build_status_view()
    assert view["total_assets_cny"] is None
    assert view["gold"]["avg_cost_cny_per_gram"] is None and view["gold"]["pnl_cny"] is None
    text = portfolio_summary_text(pm, total_assets=2700, current_prices={"GOLD": float("nan"), "ETF": 40})
    assert "未核跟踪投影" in text and "均价 ¥100.00" not in text


def test_broker_fee_pending_hides_cost_and_profit_including_gold_compatibility(pm, monkeypatch):
    evidence = account_source_status(pm)
    evidence.update(status="requires_reconciliation", allocation_cash_eligible=False, broker_commission_pending=1)
    evidence["holdings"]["GOLD"]["cost_basis"] = "broker_commission_pending"
    monkeypatch.setattr("openinvest.services.ledger_evidence.account_source_status", lambda pm: evidence)
    view = build_status_view()
    row = next(row for row in view["all_holdings"] if row["symbol"] == "GOLD")
    assert row["units"] == 10 and row["avg_cost"] is None and row["pnl"] is None
    assert view["gold"]["avg_cost_cny_per_gram"] is None and view["gold"]["pnl_cny"] is None
    assert view["cash"]["cny"] is None and view["total_assets_cny"] is None
    text = _summary(pm)
    assert "手续费待核" in text and "均价 100.00" not in text and "+500.00" not in text


def test_one_unqualified_gold_member_cannot_leave_a_partial_aggregate_as_a_complete_balance(pm, monkeypatch):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][1].update(kind="metal", proxy_kind="gold_cny_per_gram", unit_label="克")
    evidence = account_source_status(PortfolioManager(pm.store))
    evidence["status"] = "requires_reconciliation"
    evidence["holdings"]["GOLD"].update(current_quantity="estimated_or_proxy_unreconciled", cost_basis="unknown")
    monkeypatch.setattr("openinvest.services.ledger_evidence.account_source_status", lambda pm: evidence)
    view = build_status_view()
    assert view["gold"]["grams"] is None and view["gold"]["value_cny"] is None
    assert view["gold"]["pnl_cny"] is None and view["gold"]["avg_cost_cny_per_gram"] is None
    assert view["gold"]["tracking_projection"]["grams"] == 15


def test_qualified_paper_book_keeps_simulated_arithmetic_and_labels_it(pm):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["ledger_domain"] = "paper_simulation"
    pm._reload()
    view = build_status_view()
    assert "模拟账户" in view["account_label"]
    assert view["gold"]["grams"] == 10 and view["gold"]["pnl_cny"] == 500
    assert view["gold"]["pnl_basis"] == "paper_simulated"
    assert view["total_assets_cny"] == 2700
    assert all(row["pnl_basis"] == "paper_simulated" for row in view["all_holdings"])
    text = _summary(pm)
    assert "[PAPER ACCOUNT]" in text and "模拟浮盈 +50.00%" in text


def test_complete_client_book_retains_normal_quantities_costs_and_unrealized_profit(pm):
    view = build_status_view()
    row = next(row for row in view["all_holdings"] if row["symbol"] == "GOLD")
    assert row["units"] == 10 and row["avg_cost"] == 100 and row["pnl"] == 500
    assert view["gold"]["grams"] == 10 and view["gold"]["pnl_cny"] == 500
    assert view["total_assets_cny"] == 2700 and "source_warning" not in view
    text = _summary(pm)
    assert "10.0000 克" in text and "均价 ¥100.00" in text and "浮盈 +50.00%" in text
