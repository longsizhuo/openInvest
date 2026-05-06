"""core/schemas.py — Pydantic 数据层 schema 测试 (v2 通用化)

覆盖：
- 合法 strategy / portfolio (v2) / user 通过校验
- 关键约束：分配比例总和、负数、重复 symbol、范围越界、币种格式
- helper 函数会去掉 name/type/updated 元字段
- v2 portfolio 的 Holding / cash dict 结构
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.schemas import (
    Holding,
    PortfolioData,
    StrategyData,
    TargetAsset,
    UserData,
    validate_portfolio,
    validate_strategy,
    validate_user,
)


# ---------- TargetAsset ----------

def test_target_asset_minimal():
    a = TargetAsset(symbol="NDQ.AX", max_single_invest_cny=10000)
    assert a.symbol == "NDQ.AX"
    assert a.price_offset_pct is None  # 可选字段


def test_target_asset_offset_range():
    """offset 限制 ±10%（防 typo 把 0.005 写成 5）"""
    with pytest.raises(ValidationError, match="price_offset_pct"):
        TargetAsset(symbol="GC=F", max_single_invest_cny=5000, price_offset_pct=0.5)


def test_target_asset_negative_cap_rejected():
    with pytest.raises(ValidationError, match="max_single_invest_cny"):
        TargetAsset(symbol="X", max_single_invest_cny=-100)


# ---------- StrategyData ----------

def _valid_strategy(**override):
    base = {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [
            {"symbol": "NDQ.AX", "max_single_invest_cny": 10000},
            {"symbol": "GC=F", "max_single_invest_cny": 5000, "sell_fee_pct": 0.0038},
        ],
    }
    base.update(override)
    return base


def test_strategy_valid():
    s = StrategyData(**_valid_strategy())
    assert len(s.target_assets) == 2


def test_strategy_allocations_must_sum_to_one():
    """0.6 + 0.3 = 0.9，不通过"""
    with pytest.raises(ValidationError, match="必须 ≈ 1.0"):
        StrategyData(**_valid_strategy(target_allocation_stock=0.6))


def test_strategy_allocations_floating_tolerance():
    """0.7 + 0.295 = 0.995 在 ±0.01 容忍范围内，通过"""
    StrategyData(**_valid_strategy(
        target_allocation_stock=0.7,
        target_allocation_cash=0.295,
    ))


def test_strategy_no_duplicate_symbols():
    bad = _valid_strategy(target_assets=[
        {"symbol": "NDQ.AX", "max_single_invest_cny": 10000},
        {"symbol": "NDQ.AX", "max_single_invest_cny": 5000},
    ])
    with pytest.raises(ValidationError, match="重复 symbol"):
        StrategyData(**bad)


def test_strategy_must_have_at_least_one_asset():
    bad = _valid_strategy(target_assets=[])
    with pytest.raises(ValidationError):
        StrategyData(**bad)


def test_strategy_extra_field_allowed_for_legacy():
    """老 md 可能含未声明字段，用 extra=allow 兜住，不 break"""
    s = StrategyData(**_valid_strategy(legacy_field="should_be_ignored"))
    # extra=allow 时字段被保留
    assert getattr(s, "legacy_field", None) == "should_be_ignored"


# ---------- Holding (v2) ----------

def test_holding_minimal():
    h = Holding(symbol="NDQ.AX", kind="etf", cost_currency="AUD")
    assert h.symbol == "NDQ.AX"
    assert h.units == 0.0
    assert h.is_tracking_only is False
    assert h.proxy_kind == "direct"


def test_holding_full():
    h = Holding(
        symbol="GC=F", kind="metal", units=50.0, unit_label="克",
        avg_cost=1000.0, cost_currency="CNY", channel="浙商积存金",
        yfinance_proxy="GC=F", proxy_kind="gold_cny_per_gram",
        sell_fee_pct=0.0038,
    )
    assert h.units == 50.0
    assert h.proxy_kind == "gold_cny_per_gram"


def test_holding_tracking_only():
    h = Holding(symbol="TSLA", kind="equity", cost_currency="USD", is_tracking_only=True)
    assert h.is_tracking_only is True
    assert h.units == 0.0  # 追踪仓允许 units=0


def test_holding_negative_units_rejected():
    with pytest.raises(ValidationError, match="units"):
        Holding(symbol="X", kind="equity", cost_currency="USD", units=-1)


def test_holding_invalid_currency():
    """币种 normalize 后必须 3-5 个大写字母；小写自动 normalize 成大写（宽容输入）"""
    # 空字符串 normalize 后还是空 → 拒
    with pytest.raises(ValidationError):
        Holding(symbol="X", kind="equity", cost_currency="")
    # 符号 → 拒
    with pytest.raises(ValidationError):
        Holding(symbol="X", kind="equity", cost_currency="¥")
    # 超长 → 拒
    with pytest.raises(ValidationError):
        Holding(symbol="X", kind="equity", cost_currency="HKDOLLAR")
    # 小写应该被 normalize 为大写并通过（宽容输入）
    h = Holding(symbol="X", kind="equity", cost_currency="cny")
    assert h.cost_currency == "CNY"


def test_holding_currency_normalized():
    """field_validator 自动 strip + uppercase"""
    h = Holding(symbol="X", kind="equity", cost_currency="  usd  ")
    assert h.cost_currency == "USD"


def test_holding_invalid_kind():
    with pytest.raises(ValidationError, match="kind"):
        Holding(symbol="X", kind="weird_kind", cost_currency="USD")


def test_holding_invalid_symbol_chars():
    """symbol 不能含空格/中文"""
    with pytest.raises(ValidationError):
        Holding(symbol="apple stock", kind="equity", cost_currency="USD")
    with pytest.raises(ValidationError):
        Holding(symbol="苹果", kind="equity", cost_currency="USD")


# ---------- PortfolioData (v2) ----------

def test_portfolio_minimal_defaults():
    p = PortfolioData()
    assert p.cash == {}
    assert p.holdings == []
    assert p.schema_version == 2


def test_portfolio_with_data():
    p = PortfolioData(
        cash={"CNY": 12345.67, "AUD": -100.0},
        holdings=[
            {"symbol": "NDQ.AX", "kind": "etf", "units": 256, "cost_currency": "AUD"},
            {"symbol": "GC=F", "kind": "metal", "units": 50, "cost_currency": "CNY"},
        ],
    )
    assert p.cash["CNY"] == 12345.67
    assert p.cash["AUD"] == -100.0  # 负现金允许
    assert len(p.holdings) == 2


def test_portfolio_currency_keys_normalized():
    """key 自动 uppercase + strip"""
    p = PortfolioData(cash={"  cny": 100, "usd": 50})
    assert "CNY" in p.cash
    assert "USD" in p.cash
    assert "cny" not in p.cash


def test_portfolio_no_duplicate_symbols():
    with pytest.raises(ValidationError, match="重复 symbol"):
        PortfolioData(holdings=[
            {"symbol": "NDQ.AX", "kind": "etf", "units": 100, "cost_currency": "AUD"},
            {"symbol": "NDQ.AX", "kind": "etf", "units": 50, "cost_currency": "AUD"},
        ])


def test_portfolio_extra_field_allowed_for_legacy():
    """旧 v1 数据迁移期可能有 cash_cny 等冗余字段，不应 break"""
    p = PortfolioData(
        cash={"CNY": 100},
        holdings=[],
        cash_cny=999,  # v1 残留
        ndq_shares=999,
    )
    assert p.cash["CNY"] == 100  # 新结构有效
    # v1 字段保留在 model 但不被业务代码读


def test_portfolio_schema_version_must_be_2():
    """v1 数据 schema_version=1 不应通过新 schema 校验"""
    with pytest.raises(ValidationError):
        PortfolioData(schema_version=1)
    with pytest.raises(ValidationError):
        PortfolioData(schema_version=3)


# ---------- UserData ----------

def test_user_risk_tolerance_enum():
    UserData(risk_tolerance="Balanced")
    UserData(risk_tolerance="Aggressive")
    with pytest.raises(ValidationError, match="risk_tolerance"):
        UserData(risk_tolerance="YOLO")


# ---------- helper：validate_*() 去掉元字段 ----------

def test_validate_strategy_strips_meta_fields():
    md_metadata = {
        "name": "strategy",        # MemoryStore 元字段，不应进 schema
        "type": "strategy",
        "updated": "2026-05-06T00:00:00",
        **_valid_strategy(),
    }
    out = validate_strategy(md_metadata)
    assert "name" not in out
    assert "type" not in out
    assert "updated" not in out
    assert out["target_allocation_stock"] == 0.7


def test_validate_strategy_excludes_none():
    """exclude_none：optional 字段为 None 时不写进 md，frontmatter 干净"""
    out = validate_strategy({
        **_valid_strategy(),
        # max_single_invest_cny / target_asset 是 Optional，应被剔除
    })
    assert "max_single_invest_cny" not in out
    assert "target_asset" not in out


def test_validate_portfolio_blocks_bad_data():
    """v2: 重复 symbol 应拒"""
    with pytest.raises(ValidationError):
        validate_portfolio({
            "cash": {"CNY": 100},
            "holdings": [
                {"symbol": "X", "kind": "equity", "units": 1, "cost_currency": "USD"},
                {"symbol": "X", "kind": "equity", "units": 2, "cost_currency": "USD"},
            ],
        })


def test_validate_portfolio_v2_clean_output():
    """合法数据 → output dict 含 cash/holdings/schema_version，不含元字段"""
    out = validate_portfolio({
        "name": "portfolio", "type": "state", "updated": "2026-05-06",
        "cash": {"CNY": 1000},
        "holdings": [{"symbol": "AAPL", "kind": "equity", "units": 10, "cost_currency": "USD"}],
        "schema_version": 2,
    })
    assert "name" not in out
    assert "type" not in out
    assert out["cash"] == {"CNY": 1000.0}
    assert out["holdings"][0]["symbol"] == "AAPL"
    assert out["schema_version"] == 2


def test_validate_user_blocks_bad_risk():
    with pytest.raises(ValidationError):
        validate_user({"risk_tolerance": "INVALID"})
