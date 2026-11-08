"""Real CLI processes and HTTP MCP dispatch write only the synthetic owner book."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.ledger_evidence import account_source_status
from tests.test_mcp_http_integration import http_server, session  # noqa: F401


SYMBOL = "SYNTHETIC"


@pytest.fixture
def pm(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "owner" / "memory")
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT", store.root)
    store.write("user", "user", {"display_name": "Synthetic owner"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "updated": "2020-01-01T00:00:00+00:00",
        "cash": {"CNY": 1000.0}, "holdings": [{"symbol": SYMBOL, "kind": "etf", "units": 10.0,
        "avg_cost": 5.0, "cost_currency": "CNY", "unit_label": "股", "proxy_kind": "direct"}]}, "")
    return PortfolioManager(store)


def _as_of():
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8))).isoformat()


def _memory_files(pm):
    return {str(path.relative_to(pm.store.root)): path.read_bytes()
            for path in pm.store.root.rglob("*") if path.is_file()}


def _cli(pm, source_file, *, as_of, locator, key, extra=()):
    environment = {**os.environ, "INVEST_HOME": str(pm.store.root.parent),
                   "INVEST_EMBEDDED_SCHEDULER": "0", "PYTHON_DOTENV_DISABLED": "1"}
    return subprocess.run([
        sys.executable, "-m", "openinvest.cli", "record_balance_observation",
        "--reference-symbol", SYMBOL, "--as-of", as_of,
        "--source-text-file", str(source_file), "--source-locator", locator,
        "--idempotency-key", key, *extra,
    ], env=environment, cwd=pm.store.root.parent, text=True, capture_output=True, timeout=30)


async def _http_record(server, arguments):
    async with session(server) as client:
        return await client.call_tool("record_balance_observation", arguments)


def test_real_cli_preserves_original_statement_and_absolute_native_balances_on_replay(pm, tmp_path):
    source = "  用户原始声明\n份额 7.25 股；每股平均成本 4.25 元。\n "
    source_file = tmp_path / "original-statement.txt"
    source_file.write_text(source, encoding="utf-8")
    locator = {"artifact_id": "source:synthetic-cli-statement", "page": 2,
               "parent_ids": ["source:synthetic-cli-ledger"]}
    stamp = _as_of()
    parameters = dict(as_of=stamp, locator=json.dumps(locator, ensure_ascii=False), key="cli-balance",
                      extra=("--reported-quantity", "7.25", "--quantity-unit", "股", "--native-instrument", SYMBOL,
                             "--reported-cost", "4.25", "--cost-currency", "CNY"))
    first = _cli(pm, source_file, **parameters)
    assert first.returncode == 0, first.stderr or first.stdout
    receipt = json.loads(first.stdout)
    assert receipt["status"] == "current" and receipt["replayed"] is False
    assert receipt["as_of"] == stamp and datetime.fromisoformat(receipt["observed_at"]) > datetime.fromisoformat(stamp)
    assert receipt["request"]["source_text"] == source and receipt["request"]["source_locator"] == locator
    assert receipt["execution_observed"] is False and receipt["cash_flow_recorded"] is False
    assert receipt["historical_execution_eligible"] is False
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 7.25 and fresh.find_holding(SYMBOL)["avg_cost"] == 4.25
    assert fresh.cash_amount("CNY") == 1000
    assert all(row["action"] not in {"buy", "sell"} and row["cash_effect"] == 0
               for row in pm.store.read_history())
    saved, = (pm.store.root / ".evidence/account_observations").glob("*.json")
    assert json.loads(saved.read_text())["payload"]["request"]["source_text"] == source
    after_first = _memory_files(pm)
    replay = _cli(pm, source_file, **parameters)
    assert replay.returncode == 0, replay.stderr or replay.stdout
    repeated = json.loads(replay.stdout)
    assert repeated["replayed"] is True and repeated["observation_id"] == receipt["observation_id"]
    assert repeated["as_of"] == stamp and repeated["observed_at"] == receipt["observed_at"]
    assert _memory_files(pm) == after_first


def test_real_http_mcp_value_only_keeps_native_quantity_and_cost_unconfirmed(pm, http_server):
    pm.store.append_history({"ts": "2020-01-01T00:00:00+00:00", "action": "buy", "symbol": SYMBOL,
        "source": "dca_daily", "units": 20, "price": 5, "currency": "CNY", "funding_source": "external_funding"})
    before = pm.store.path_of("portfolio").read_bytes()
    source = "  原始资料仅声明市值 1234.56 元；没有份额或成本。\n"
    locator = {"kind": "client_statement", "artifact_id": "source:synthetic-http-statement",
               "parent_ids": ["source:synthetic-http-ledger"], "page": 1}
    stamp = _as_of()
    arguments = {"reference_symbol": SYMBOL, "as_of": stamp, "source_text": source,
                 "source_locator": json.dumps(locator, ensure_ascii=False), "idempotency_key": "http-value-only",
                 "reported_value": 1234.56, "value_currency": "CNY"}
    response = asyncio.run(_http_record(http_server, arguments))
    assert not response.isError
    receipt = response.structuredContent["result"]
    assert receipt["status"] == "current" and receipt["portfolio_mutated"] is False
    assert receipt["as_of"] == stamp and receipt["request"]["source_text"] == source
    assert receipt["request"]["source_locator"] == locator
    assert receipt["execution_observed"] is False and receipt["historical_execution_eligible"] is False
    assert pm.store.path_of("portfolio").read_bytes() == before
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 10 and fresh.find_holding(SYMBOL)["avg_cost"] == 5
    assert fresh.cash_amount("CNY") == 1000
    grade = account_source_status(fresh)["holdings"][SYMBOL]
    assert grade["current_value"] == "client_observed_balance"
    assert grade["current_quantity"] == grade["cost_basis"] == "estimated_or_proxy_unreconciled"
    assert grade["balance_observations"]["current_value"]["as_of"] == stamp
    assert grade["independent_broker_verification"] is False
    after_first = _memory_files(pm)
    replay = asyncio.run(_http_record(http_server, arguments))
    assert not replay.isError and replay.structuredContent["result"]["replayed"] is True
    assert _memory_files(pm) == after_first


@pytest.mark.parametrize("transport", ["cli", "http"])
def test_malformed_locator_fails_without_writing_an_observation(pm, tmp_path, request, transport):
    before = _memory_files(pm)
    stamp = _as_of()
    if transport == "cli":
        source_file = tmp_path / "invalid-locator-statement.txt"
        source_file.write_text("合成余额声明。", encoding="utf-8")
        result = _cli(pm, source_file, as_of=stamp, locator='{"artifact_id":', key="bad-cli-locator",
                      extra=("--reported-value", "50", "--value-currency", "CNY"))
        assert result.returncode != 0
        assert json.loads(result.stdout)["status"] == "error"
    else:
        result = asyncio.run(_http_record(request.getfixturevalue("http_server"), {
            "reference_symbol": SYMBOL, "as_of": stamp, "source_text": "合成余额声明。",
            "source_locator": '{"artifact_id":', "idempotency_key": "bad-http-locator",
            "reported_value": 50, "value_currency": "CNY",
        }))
        assert result.isError
        assert any("source_locator_json_invalid" in getattr(block, "text", "") for block in result.content)
    assert _memory_files(pm) == before
