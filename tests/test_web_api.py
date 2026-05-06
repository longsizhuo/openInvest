"""connectors/web_api.py — FastAPI 只读端点测试

策略：
- 用 tmp_path 隔离 memory，避免污染真实持仓
- monkeypatch `_new_pm` 返回用临时 store 的 PortfolioManager
- monkeypatch yfinance / 金价快照，避免测试打外网
- 验证响应 JSON 结构 + 关键字段值
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from connectors import web_api
from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager


# ---------- 测试数据 ----------

def _seed_memory(store: MemoryStore) -> None:
    """填充 user / strategy / portfolio 三个 md 文件，让 PortfolioManager 能初始化"""
    store.write("user", "user", {
        "display_name": "TestUser",
        "risk_tolerance": "Balanced",
        "exchange_buffer_cny": 5000,
    }, "# user body")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [
            {
                "symbol": "NDQ.AX",
                "display_name": "BetaShares Nasdaq 100",
                "channel": "CommSec",
                "max_single_invest_cny": 10000,
            },
            {
                "symbol": "GC=F",
                "display_name": "黄金 (浙商积存金)",
                "channel": "浙商积存金",
                "max_single_invest_cny": 5000,
                "price_offset_pct": 0.012,
                "sell_fee_pct": 0.0038,
            },
        ],
    }, "# strategy body")
    store.write("portfolio", "state", {
        "cash_cny": 12345.67,
        "aud_cash": 100.0,
        "ndq_shares": 128.0,
        "gold_grams": 50.0,
        "gold_avg_cost_cny_per_gram": 1000.0,
    }, "# portfolio body")


# ---------- Fixtures ----------

@pytest.fixture
def tmp_store(tmp_path):
    """每个测试一份干净的 memory dir"""
    store = MemoryStore(tmp_path / "memory")
    _seed_memory(store)
    return store


@pytest.fixture
def client(tmp_store, monkeypatch):
    """TestClient + 把 _new_pm 切成临时 store 版本"""
    def _new_pm_fake() -> PortfolioManager:
        return PortfolioManager(store=tmp_store)
    monkeypatch.setattr(web_api, "_new_pm", _new_pm_fake)

    # MemoryStore() 默认构造仍走 MEMORY_ROOT，但 /api/history 和 /api/daily
    # 在 web_api 里 new MemoryStore() 是为了读 history.jsonl / daily/，它们
    # 在 tmp_store 里不存在，需要也指向 tmp_path
    real_init = MemoryStore.__init__

    def _init_default_to_tmp(self, root=None):
        return real_init(self, root or tmp_store.root)
    monkeypatch.setattr(MemoryStore, "__init__", _init_default_to_tmp)

    # 屏蔽外部网络：金价快照 + yfinance
    @dataclass
    class FakeSnap:
        gold_usd_per_oz: float = 2400.0
        usdcny_rate: float = 7.2
        spot_cny_per_gram: float = 1100.0
        bank_cny_per_gram: float = 1113.2
        offset_pct: float = 0.012
        is_stale: bool = False

    monkeypatch.setattr(web_api, "get_gold_snapshot", lambda offset_pct=0.0: FakeSnap(offset_pct=offset_pct))

    # 假 NDQ.AX K 线（5d）
    fake_df = pd.DataFrame(
        {"Close": [40.0, 41.0, 42.0, 41.5, 42.5]},
        index=pd.date_range("2026-04-28", periods=5, freq="D"),
    )
    monkeypatch.setattr(web_api, "get_history_data", lambda symbol, period="5d": fake_df)

    return TestClient(web_api.app)


# ---------- 端点测试 ----------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "invest-web-api"
    assert "timestamp" in body


def test_portfolio_full(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    b = r.json()

    assert b["cash"]["cny"] == 12345.67
    assert b["cash"]["aud"] == 100.0

    # 黄金：50g * 1100/g = 55000，浮盈 = (1100 - 1000) * 50 = 5000
    assert b["gold"]["grams"] == 50.0
    assert b["gold"]["avg_cost_cny_per_gram"] == 1000.0
    assert b["gold"]["spot_cny_per_gram"] == 1100.0
    assert b["gold"]["market_value_cny"] == 55000.0
    assert b["gold"]["pnl_cny"] == 5000.0
    assert b["gold"]["is_stale"] is False

    # NDQ.AX：last 42.5 / prev 41.5 → +2.41%
    assert b["ndq"]["shares"] == 128.0
    assert b["ndq"]["last_price_aud"] == 42.5
    assert b["ndq"]["prev_close_aud"] == 41.5
    assert b["ndq"]["day_change_pct"] == pytest.approx(2.4096, rel=1e-3)


def test_strategy(client):
    r = client.get("/api/strategy")
    assert r.status_code == 200
    b = r.json()
    assert b["target_allocation_stock"] == 0.7
    assert b["target_allocation_cash"] == 0.3
    syms = [a["symbol"] for a in b["target_assets"]]
    assert "NDQ.AX" in syms and "GC=F" in syms

    gold = next(a for a in b["target_assets"] if a["symbol"] == "GC=F")
    assert gold["price_offset_pct"] == 0.012
    assert gold["sell_fee_pct"] == 0.0038


def test_gold_endpoint(client):
    r = client.get("/api/gold")
    assert r.status_code == 200
    b = r.json()
    assert b["grams"] == 50.0
    assert b["spot_cny_per_gram"] == 1100.0
    assert b["bank_cny_per_gram"] == 1113.2


def test_ndq_endpoint(client):
    r = client.get("/api/ndq")
    assert r.status_code == 200
    b = r.json()
    assert b["shares"] == 128.0
    assert b["last_price_aud"] == 42.5


def test_history_empty(client):
    """tmp memory 里没有 portfolio_history.jsonl"""
    r = client.get("/api/history")
    assert r.status_code == 200
    b = r.json()
    assert b["count"] == 0
    assert b["rows"] == []


def test_history_with_rows(client, tmp_store):
    tmp_store.append_history({"action": "deposit", "symbol": "CNY", "units": 1000.0})
    tmp_store.append_history({"action": "bought", "symbol": "GOLD-CNY", "units": 5.0, "price_per_unit": 1040.0})
    r = client.get("/api/history?limit=10")
    assert r.status_code == 200
    b = r.json()
    assert b["count"] == 2
    # 倒序：最近的在前
    assert b["rows"][0]["action"] == "bought"
    assert b["rows"][1]["action"] == "deposit"


def test_history_limit_validation(client):
    r = client.get("/api/history?limit=0")
    assert r.status_code == 422
    r = client.get("/api/history?limit=2000")
    assert r.status_code == 422


def test_daily_empty(client):
    r = client.get("/api/daily")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_daily_with_entries(client, tmp_store):
    tmp_store.append_daily("Strategy", "今日决策：加仓 NDQ.AX 5 股", date="2026-05-05")
    tmp_store.append_daily("Risk", "无重大风险", date="2026-05-06")
    r = client.get("/api/daily?since=30")
    assert r.status_code == 200
    b = r.json()
    assert b["count"] == 2
    dates = [e["date"] for e in b["entries"]]
    assert "2026-05-05" in dates
    assert "2026-05-06" in dates


def test_openapi_schema(client):
    """OpenAPI schema 必须可达，前端用 openapi-typescript 拉它生成 TS 类型"""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    paths = schema["paths"]
    assert "/api/portfolio" in paths
    assert "/api/strategy" in paths
    assert "/api/gold" in paths
    assert "/api/ndq" in paths
    assert "/api/history" in paths
    assert "/api/daily" in paths
    assert "/api/health" in paths
