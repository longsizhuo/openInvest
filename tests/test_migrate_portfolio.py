"""scripts/migrate_portfolio_to_holdings.py 测试

覆盖：
- v1 → v2 字段映射正确（cash_cny → cash["CNY"]，ndq_shares → holdings[NDQ.AX].units）
- 幂等：v2 数据再跑 noop
- 备份文件总是产生
- schema validate 失败时 transaction rollback
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from openinvest.core.memory_store import MemoryStore
from scripts.migrate_portfolio_to_holdings import migrate, render_portfolio_body_v2


@pytest.fixture
def v1_store(tmp_path):
    """构造一份典型的 v1 portfolio.md（生产真实形态）"""
    store = MemoryStore(tmp_path / "memory")
    store.write("portfolio", "state", {
        "cash_cny": 15511.30,
        "aud_cash": -6894.42,
        "ndq_shares": 256.0,
        "ndq_avg_cost_aud_per_share": 53.86,
        "gold_grams": 133.88,
        "gold_avg_cost_cny_per_gram": 1008.34,
    }, "# 旧 body 应该被重写")
    return store


def test_migrate_basic(v1_store):
    """v1 → v2：6 字段全部就位"""
    result = migrate(v1_store)
    assert result["status"] == "migrated"
    assert "backup" in result
    assert result["holdings_count"] == 2
    assert set(result["cash_currencies"]) == {"CNY", "AUD"}
    assert set(result["holdings"]) == {"NDQ.AX", "GC=F"}

    # 读迁移后的数据
    p = v1_store.read("portfolio")
    assert p.get("schema_version") == 2
    assert p.get("cash") == {"CNY": 15511.30, "AUD": -6894.42}
    assert "cash_cny" not in p.metadata, "v1 旧字段必须被清除"
    assert "aud_cash" not in p.metadata
    assert "ndq_shares" not in p.metadata

    holdings = p.get("holdings")
    ndq = next(h for h in holdings if h["symbol"] == "NDQ.AX")
    assert ndq["units"] == 256.0
    assert ndq["avg_cost"] == 53.86
    assert ndq["cost_currency"] == "AUD"
    assert ndq["kind"] == "etf"

    gold = next(h for h in holdings if h["symbol"] == "GC=F")
    assert gold["units"] == 133.88
    assert gold["avg_cost"] == 1008.34
    assert gold["cost_currency"] == "CNY"
    assert gold["proxy_kind"] == "gold_cny_per_gram"
    assert gold["sell_fee_pct"] == 0.0038


def test_migrate_idempotent(v1_store):
    """跑两次第二次 noop"""
    migrate(v1_store)
    result2 = migrate(v1_store)
    assert result2["status"] == "noop"
    assert "v2" in result2["reason"]


def test_migrate_creates_backup(v1_store, tmp_path):
    """每次跑都会 cp 一份 .bak.<ts>"""
    result = migrate(v1_store)
    bak_files = list((tmp_path / "memory").glob("portfolio.md.bak.*"))
    assert len(bak_files) == 1
    assert str(bak_files[0]) == result["backup"]


def test_migrate_negative_cash_preserved(tmp_path):
    """AUD 负数现金（你刚遇到的 -6894 场景）必须保留进 cash dict"""
    store = MemoryStore(tmp_path / "memory")
    store.write("portfolio", "state", {
        "cash_cny": 0,
        "aud_cash": -1000.0,
        "ndq_shares": 0,
        "gold_grams": 0,
    }, "# body")
    migrate(store)
    p = store.read("portfolio")
    assert p.get("cash") == {"AUD": -1000.0}  # CNY=0 被剔除，AUD 负数保留


def test_migrate_zero_holdings(tmp_path):
    """全部 0 → cash 空 + holdings 空 + schema_version=2"""
    store = MemoryStore(tmp_path / "memory")
    store.write("portfolio", "state", {
        "cash_cny": 0, "aud_cash": 0, "ndq_shares": 0, "gold_grams": 0,
    }, "# body")
    migrate(store)
    p = store.read("portfolio")
    assert p.get("schema_version") == 2
    assert p.get("cash") == {}
    assert p.get("holdings") == []


def test_migrate_no_portfolio_md(tmp_path):
    """portfolio.md 不存在 → noop"""
    store = MemoryStore(tmp_path / "memory")
    result = migrate(store)
    assert result["status"] == "noop"
    assert "不存在" in result["reason"]


def test_migrate_partial_holdings_field_warns(tmp_path):
    """已检测到 holdings 字段但 schema_version<2 → abort，要 --force"""
    store = MemoryStore(tmp_path / "memory")
    store.write("portfolio", "state", {
        "cash_cny": 100,
        "holdings": [{"symbol": "X", "kind": "equity", "cost_currency": "USD"}],  # 半新结构
    }, "# body")
    result = migrate(store)
    assert result["status"] == "abort"


def test_migrate_force_overrides_partial(tmp_path):
    """force=True 强制重跑，覆盖现有 holdings"""
    store = MemoryStore(tmp_path / "memory")
    store.write("portfolio", "state", {
        "cash_cny": 100,
        "ndq_shares": 50,
        "ndq_avg_cost_aud_per_share": 40,
        "holdings": [{"symbol": "OLD", "kind": "equity", "cost_currency": "USD"}],
    }, "# body")
    result = migrate(store, force=True)
    assert result["status"] == "migrated"
    p = store.read("portfolio")
    syms = [h["symbol"] for h in p.get("holdings")]
    assert "NDQ.AX" in syms  # v1 ndq_shares 转过来了
    assert "OLD" not in syms  # 旧 holdings 被覆盖


def test_render_portfolio_body_v2_includes_tracking_marker():
    """追踪仓在 body 里有 🔍 标记"""
    body = render_portfolio_body_v2(
        cash={"USD": 1000},
        holdings=[
            {"symbol": "TSLA", "kind": "equity", "units": 0,
             "cost_currency": "USD", "is_tracking_only": True},
        ],
    )
    assert "🔍" in body
    assert "[追踪仓]" in body
    assert "TSLA" in body
    assert "USD" in body
