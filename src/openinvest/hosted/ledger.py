"""Shared hosted ledger setup and preferences for the browser and MCP adapters."""
import hashlib
import json
import os

from pydantic import BaseModel, ConfigDict, Field


class Initialization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    risk: str | None = Field(default=None, pattern=r"^(Conservative|Balanced|Aggressive)$")
    symbols: list[str] = Field(default_factory=list, max_length=50)


def apply_base_currency():
    from openinvest.paths import INVEST_ROOT
    marker = INVEST_ROOT / ".hosted-initialization.json"
    if marker.exists():
        spec = Initialization.model_validate(json.loads(marker.read_text()))
        # Core config reads its own defaults/API overrides, not this ledger's config.yaml.
        os.environ["INVEST_PORTFOLIO_BASE_CURRENCY"] = spec.currency
        from openinvest.core.config import reset_config
        reset_config()


def initialize(data):
    from openinvest.paths import INVEST_ROOT
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.schemas import validate_portfolio, validate_strategy
    from openinvest.services.symbol_map import normalize_symbol
    import re
    import yaml
    spec = Initialization.model_validate(data)
    store = MemoryStore()
    marker = INVEST_ROOT / ".hosted-initialization.json"
    expected = spec.model_dump()
    resuming = marker.exists() and json.loads(marker.read_text()) == expected
    if not resuming and (marker.exists() or any(store.path_of(n).exists() for n in ("user", "strategy", "portfolio"))):
        raise ValueError("Ledger already initialized; import/reset is not implicit")
    symbols = [normalize_symbol(s) for s in spec.symbols]
    if any(not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=:_-]{0,39}", s) or re.fullmatch(r"\d{6}", s) for s in symbols):
        raise ValueError("Use explicit market symbols")
    targets = [{"symbol": s, "max_single_invest_cny": 0} for s in symbols]
    strategy = {"target_allocation_stock": 0.0, "target_allocation_cash": 1.0, "target_assets": targets, "allow_empty_targets": True}
    portfolio = {"schema_version": 2, "cash": {spec.currency: 0.0}, "holdings": []}
    validate_strategy(strategy)
    validate_portfolio(portfolio)
    # Validate every document before writing; no external model parses onboarding.
    config = INVEST_ROOT / "config.yaml"
    if config.exists() and not resuming:
        raise ValueError("Existing configuration cannot be overwritten")
    if not marker.exists():
        from openinvest.core.memory_store import _atomic_write_text
        _atomic_write_text(marker, json.dumps(expected))
    apply_base_currency()
    # Retrying the same onboarding after interruption fills missing documents only.
    if all(store.path_of(n).exists() for n in ("user", "strategy", "portfolio")) and config.exists():
        return {"initialized": True}
    config.write_text(yaml.safe_dump({"portfolio": {"base_currency": spec.currency}}), encoding="utf-8", newline="\n")
    if not store.path_of("user").exists():
        store.write("user", "user", {"display_name": "Investor", "risk_tolerance": spec.risk}, "")
    if not store.path_of("strategy").exists():
        store.write("strategy", "strategy", strategy, "")
    if not store.path_of("portfolio").exists():
        store.write("portfolio", "portfolio", portfolio, "")
    from openinvest.core.config import reset_config
    reset_config()
    return {"initialized": True}


class LedgerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=64, max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    risk: str | None = Field(default=None, pattern=r"^(Conservative|Balanced|Aggressive)$")
    symbols: list[str] | None = Field(default=None, max_length=50)


def _currency_locked(store, portfolio):
    """Reporting-currency migration is intentionally separate from editing preferences."""
    from openinvest.paths import INVEST_ROOT
    if portfolio.get("holdings") or portfolio.get("pending_fund_orders"):
        return True
    if any(float(value or 0) != 0 for value in portfolio.get("cash", {}).values()):
        return True
    # A round trip back to zero is still historical financial activity. Prepared
    # analysis snapshots also bind the original currency, even without execution.
    if any(p.exists() and p.stat().st_size for p in (
        store.root / "portfolio_history.jsonl", store.root / ".dreams" / "executions.jsonl",
    )):
        return True
    analysis = INVEST_ROOT / "db" / "hosted_analyses.sqlite3"
    if analysis.exists():
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(f"file:{analysis}?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM analyses LIMIT 1").fetchone():
                return True
    return any((INVEST_ROOT / "docs" / "reports").rglob("*.md"))


def ledger_settings():
    from openinvest.paths import INVEST_ROOT
    from openinvest.core.memory_store import MemoryStore
    marker = INVEST_ROOT / ".hosted-initialization.json"
    store = MemoryStore()
    if not marker.exists() or not all(store.path_of(n).exists() for n in ("user", "strategy", "portfolio")):
        return {"initialized": False}
    user, strategy, portfolio = (store.read(n).metadata for n in ("user", "strategy", "portfolio"))
    value = {"initialized": True, "currency": json.loads(marker.read_text())["currency"],
             "risk": user.get("risk_tolerance"),
             "symbols": [a["symbol"] for a in strategy.get("target_assets", [])],
             "currency_editable": not _currency_locked(store, portfolio)}
    # Include the complete target metadata, so an Agent update cannot be lost
    # while a browser with stale symbol rows saves its own changes.
    revision = {**value, "targets": strategy.get("target_assets", [])}
    value["revision"] = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
    return value


def update_ledger_settings(data):
    from openinvest.paths import INVEST_ROOT
    from openinvest.core.memory_store import MemoryStore, _atomic_write_text
    from openinvest.core.schemas import validate_strategy, validate_user
    from openinvest.services.symbol_map import normalize_symbol
    import re
    import yaml
    spec = LedgerUpdate.model_validate(data)
    current = ledger_settings()
    if not current["initialized"]:
        raise ValueError("ledger_not_initialized")
    if spec.expected_revision != current["revision"]:
        raise ValueError("settings_conflict")
    if "currency" in spec.model_fields_set and spec.currency is None:
        raise ValueError("invalid_currency")
    if "symbols" in spec.model_fields_set and spec.symbols is None:
        raise ValueError("invalid_symbols")
    if spec.currency and spec.currency != current["currency"] and not current["currency_editable"]:
        raise ValueError("currency_has_history")
    store = MemoryStore()
    user = dict(store.read("user").metadata)
    strategy = dict(store.read("strategy").metadata)
    if "risk" in spec.model_fields_set:
        user["risk_tolerance"] = spec.risk
    if spec.symbols is not None:
        symbols = list(dict.fromkeys(normalize_symbol(s) for s in spec.symbols))
        if any(not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=:_-]{0,39}", s) or re.fullmatch(r"\d{6}", s) for s in symbols):
            raise ValueError("invalid_symbols")
        existing = {a["symbol"]: a for a in strategy.get("target_assets", [])}
        strategy["target_assets"] = [existing.get(s, {"symbol": s, "max_single_invest_cny": 0}) for s in symbols]
        strategy["allow_empty_targets"] = True
    validate_user(user)
    validate_strategy(strategy)
    # The worker serializes internal HTTP and MCP requests under one lease lock.
    # No holdings, cash amounts, receipts or prior decisions are rewritten here.
    if "risk" in spec.model_fields_set:
        store.update_fields("user", risk_tolerance=spec.risk)
    if spec.symbols is not None:
        store.update_fields("strategy", target_assets=strategy["target_assets"], allow_empty_targets=True)
    if spec.currency and spec.currency != current["currency"]:
        marker = INVEST_ROOT / ".hosted-initialization.json"
        saved = json.loads(marker.read_text())
        saved["currency"] = spec.currency
        # Marker is authoritative for the worker's reporting currency on restart.
        _atomic_write_text(marker, json.dumps(saved))
        config = INVEST_ROOT / "config.yaml"
        contents = yaml.safe_load(config.read_text()) or {}
        contents.setdefault("portfolio", {})["base_currency"] = spec.currency
        _atomic_write_text(config, yaml.safe_dump(contents))
        apply_base_currency()
    return ledger_settings()
