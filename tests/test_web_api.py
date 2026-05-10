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
    # v2 结构：v1 fallback 已退场，测试数据直接用 cash dict + holdings list
    store.write("portfolio", "state", {
        "schema_version": 2,
        "cash": {"CNY": 12345.67, "AUD": 100.0},
        "holdings": [
            {
                "symbol": "NDQ.AX",
                "kind": "etf",
                "units": 128.0,
                "unit_label": "股",
                "avg_cost": 53.86,
                "cost_currency": "AUD",
                "channel": "CommSec",
                "display_name": "BetaShares Nasdaq 100",
                "proxy_kind": "direct",
            },
            {
                "symbol": "GC=F",
                "kind": "metal",
                "units": 50.0,
                "unit_label": "克",
                "avg_cost": 1000.0,
                "cost_currency": "CNY",
                "channel": "浙商积存金",
                "display_name": "黄金（浙商积存金）",
                "yfinance_proxy": "GC=F",
                "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            },
        ],
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

    # 委员会任务落盘目录切到 tmp，避免污染真实 memory/.committee/
    monkeypatch.setattr(web_api, "COMMITTEE_DIR", tmp_store.root / ".committee")

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
    # PR 1 GET 端点
    assert "/api/portfolio" in paths
    assert "/api/strategy" in paths
    assert "/api/gold" in paths
    assert "/api/ndq" in paths
    assert "/api/history" in paths
    assert "/api/daily" in paths
    assert "/api/health" in paths
    # PR 2 写操作 + 委员会
    assert "/api/deposit" in paths
    assert "/api/withdraw" in paths
    assert "/api/gold/buy" in paths
    assert "/api/gold/sell" in paths
    assert "/api/gold/set" in paths
    assert "/api/gold/offset" in paths
    assert "/api/committee/run" in paths
    assert "/api/committee/{task_id}" in paths


# ============ PR 2: 写操作端点测试 ============

def test_deposit_cny(client, tmp_store):
    r = client.post("/api/deposit", json={"currency": "cny", "amount": 1000.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # 原 12345.67 + 1000 = 13345.67
    assert body["cash_cny"] == pytest.approx(13345.67)
    assert body["history_appended"] is True

    # 验证 portfolio.md 真的被改了（v2: cash dict）
    p = tmp_store.read("portfolio")
    cash = p.get("cash") or {}
    assert float(cash.get("CNY", 0)) == pytest.approx(13345.67)
    # v1 旧字段已清
    assert "cash_cny" not in p.metadata

    # 验证 history.jsonl 多了一条
    rows = tmp_store.read_history()
    assert any(r["action"] == "deposit" and r["units"] == 1000.0 for r in rows)


def test_deposit_aud(client, tmp_store):
    r = client.post("/api/deposit", json={"currency": "aud", "amount": 50.0})
    assert r.status_code == 200
    assert r.json()["aud_cash"] == pytest.approx(150.0)


def test_deposit_validation(client):
    """金额必须 > 0；负数 / 0 / 缺字段都应 422"""
    assert client.post("/api/deposit", json={"currency": "cny", "amount": 0}).status_code == 422
    assert client.post("/api/deposit", json={"currency": "cny", "amount": -100}).status_code == 422
    assert client.post("/api/deposit", json={"currency": "usd", "amount": 100}).status_code == 422
    assert client.post("/api/deposit", json={"amount": 100}).status_code == 200  # currency 默认 cny


def test_withdraw(client, tmp_store):
    r = client.post("/api/withdraw", json={"currency": "cny", "amount": 1000.0})
    assert r.status_code == 200
    # 原 12345.67 - 1000 = 11345.67
    assert r.json()["cash_cny"] == pytest.approx(11345.67)


def test_withdraw_insufficient_blocked(client):
    """v2 阶段 5: AUD 余额 100 但扣 500 → 拒绝（防 AUD -6894 类事故）"""
    r = client.post("/api/withdraw", json={"currency": "aud", "amount": 500.0})
    assert r.status_code == 400
    assert "余额不足" in r.json()["detail"]


def test_gold_buy_avg_cost(client, tmp_store):
    """加权均价：原 50g@1000 → 再买 10g@1100 → 60g 均价应为 (50000+11000)/60 = 1016.67"""
    r = client.post("/api/gold/buy", json={"grams": 10.0, "price_per_gram": 1100.0})
    assert r.status_code == 200
    body = r.json()
    assert body["gold_grams"] == pytest.approx(60.0)
    assert body["gold_avg_cost_cny_per_gram"] == pytest.approx(1016.67, abs=0.01)
    rows = tmp_store.read_history()
    assert any(r["action"] == "bought" and r["units"] == 10.0 for r in rows)


def test_gold_sell_with_fee(client, tmp_store):
    """卖 10g @ 1100，手续费 0.38%：毛 11000 → 净 11000*(1-0.0038)=10958.20"""
    r = client.post("/api/gold/sell", json={"grams": 10.0, "price_per_gram": 1100.0})
    assert r.status_code == 200
    body = r.json()
    assert body["gold_grams"] == pytest.approx(40.0)
    # 现金 12345.67 + 10958.20 = 23303.87
    assert body["cash_cny"] == pytest.approx(23303.87, abs=0.01)


def test_gold_sell_insufficient(client):
    """卖 100g 但只有 50g → 400 错误，且 portfolio 未变"""
    r = client.post("/api/gold/sell", json={"grams": 100.0, "price_per_gram": 1100.0})
    assert r.status_code == 400
    # 验证持仓未变
    g = client.get("/api/gold").json()
    assert g["grams"] == 50.0


def test_gold_set_no_history(client, tmp_store):
    r = client.post("/api/gold/set", json={"grams": 99.5})
    assert r.status_code == 200
    assert r.json()["gold_grams"] == 99.5
    assert r.json()["history_appended"] is False
    assert tmp_store.read_history() == []


def test_gold_offset_writes_strategy(client, tmp_store, monkeypatch):
    """报浙商克价 → 反推 offset → 写回 strategy.md"""
    # mock infer_offset_pct 返回固定值
    monkeypatch.setattr(web_api, "infer_offset_pct", lambda bank_price: 0.025)

    r = client.post("/api/gold/offset", json={"bank_price": 1130.0})
    assert r.status_code == 200
    assert "0.025" in r.json()["message"] or "+2.50%" in r.json()["message"]
    s = tmp_store.read("strategy")
    gold = next(a for a in s.get("target_assets") if a.get("symbol") == "GC=F")
    assert gold["price_offset_pct"] == 0.025


def test_concurrent_deposit_no_lost_update(client, tmp_store):
    """10 个并发 deposit 100，最终现金 = 原 + 1000，不丢任何一笔（fcntl 锁回归）"""
    import concurrent.futures

    def _do_deposit():
        return client.post("/api/deposit", json={"currency": "cny", "amount": 100}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(lambda _: _do_deposit(), range(10)))

    assert all(c == 200 for c in codes), f"some requests failed: {codes}"
    final = tmp_store.read("portfolio")
    # 原 12345.67 + 10 * 100 = 13345.67（v2: cash dict）
    cash = final.get("cash") or {}
    assert float(cash.get("CNY", 0)) == pytest.approx(13345.67, abs=0.01)
    rows = tmp_store.read_history()
    # 至少 10 条 deposit 流水（其他测试可能也写了，所以用 >=）
    deposit_count = sum(1 for r in rows if r.get("action") == "deposit")
    assert deposit_count >= 10


# ============ PR 2: 委员会异步测试 ============

def test_committee_run_and_status_done(client, monkeypatch):
    """触发委员会 → 立即返回 task_id → 模拟瞬时执行后查到 done 状态

    v3 升级：合并端点后，run() 调用 run_committee_for_symbol（不再走 daily_report.run）
    """
    fake_verdict = {"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0, "dominant_view": "risk"}
    fake_result = {
        "asset": "NDQ.AX",
        "verdict": fake_verdict,
        "report": None,
        "debate": {"max_rounds": 1, "final_round": 1, "converged": True,
                   "quant_history": ["q1"], "risk_history": ["r1"]},
    }
    import core.committee_runner as cr_mod
    import core.committee as cm_mod
    monkeypatch.setattr(cr_mod, "run_committee_for_symbol",
                        lambda sym, **kw: fake_result)
    # macro_view 也 mock 掉（避免真调 LLM）
    monkeypatch.setattr("connectors.web_api.run_macro_view", lambda data: "fake macro",
                        raising=False)
    # 直接 mock import 路径
    import core.committee
    monkeypatch.setattr(core.committee, "run_macro_view", lambda data: "fake macro")

    r = client.post("/api/committee/run", json={"note": "smoke test", "symbols": ["NDQ.AX"]})
    assert r.status_code == 200
    body = r.json()
    task_id = body["task_id"]
    assert body["status"] == "queued"
    assert body["poll_url"] == f"/api/committee/{task_id}"

    # 等异步任务完成
    import time
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        s = client.get(f"/api/committee/{task_id}")
        assert s.status_code == 200
        final = s.json()
        if final["status"] in ("done", "error"):
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "done", f"expected done, got {final}"
    # 新结构：result.by_asset[symbol].verdict
    assert "by_asset" in final["result"]
    assert final["result"]["by_asset"]["NDQ.AX"]["verdict"] == fake_verdict
    assert final["note"] == "smoke test"


def test_committee_run_error_path(client, monkeypatch):
    """run_committee_for_symbol 抛异常时 status=error 且 error 字段记录"""
    def _boom(sym, **kw):
        raise RuntimeError("LLM API down")

    import core.committee_runner as cr_mod
    monkeypatch.setattr(cr_mod, "run_committee_for_symbol", _boom)
    import core.committee
    monkeypatch.setattr(core.committee, "run_macro_view", lambda data: "fake macro")

    r = client.post("/api/committee/run", json={"symbols": ["NDQ.AX"]})
    task_id = r.json()["task_id"]

    import time
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        s = client.get(f"/api/committee/{task_id}").json()
        if s["status"] in ("done", "error"):
            final = s
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "error"
    assert "RuntimeError" in final["error"] and "LLM API down" in final["error"]


def test_committee_status_404(client):
    r = client.get("/api/committee/nonexistent_task_xyz")
    assert r.status_code == 404


# ============ v2 holdings CRUD ============

def test_holdings_list_v2(client, tmp_store, monkeypatch):
    """GET /api/holdings 返回 v2 通用结构（cash + holdings）"""
    # 模拟一次写入触发 v1→v2 自动迁移
    client.post("/api/deposit", json={"currency": "cny", "amount": 0.01})  # 触发迁移
    r = client.get("/api/holdings")
    assert r.status_code == 200
    body = r.json()
    assert "cash" in body
    assert "holdings" in body
    assert "CNY" in body["cash"]
    syms = [h["symbol"] for h in body["holdings"]]
    assert "NDQ.AX" in syms
    assert "GC=F" in syms


def test_create_holding(client, tmp_store):
    """POST /api/holdings 新增 AAPL（追踪仓）"""
    r = client.post("/api/holdings", json={
        "symbol": "AAPL", "kind": "equity",
        "cost_currency": "USD", "is_tracking_only": True,
        "display_name": "Apple Inc.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["is_tracking_only"] is True


def test_create_duplicate_holding_409(client, tmp_store):
    """触发自动迁移把 NDQ 进 holdings，再 POST 同 symbol → 409"""
    client.post("/api/deposit", json={"currency": "cny", "amount": 0.01})  # 触发迁移
    r = client.post("/api/holdings", json={
        "symbol": "NDQ.AX", "kind": "etf", "cost_currency": "AUD",
    })
    assert r.status_code == 409


def test_update_holding(client, tmp_store):
    """PUT /api/holdings/{symbol} 部分字段更新"""
    client.post("/api/deposit", json={"currency": "cny", "amount": 0.01})  # 触发迁移
    r = client.put("/api/holdings/NDQ.AX", json={"display_name": "新显示名"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "新显示名"


def test_update_holding_404(client):
    r = client.put("/api/holdings/UNKNOWN", json={"display_name": "x"})
    assert r.status_code == 404


def test_delete_holding_with_units_blocked(client, tmp_store):
    """units > 0 时拒绝删除（防误删）"""
    client.post("/api/deposit", json={"currency": "cny", "amount": 0.01})  # 触发迁移
    r = client.delete("/api/holdings/NDQ.AX")
    assert r.status_code == 400
    assert "持仓" in r.json()["detail"]


def test_delete_tracking_holding_ok(client, tmp_store):
    """追踪仓 units=0，允许删"""
    client.post("/api/holdings", json={
        "symbol": "TSLA", "kind": "equity", "cost_currency": "USD",
        "is_tracking_only": True,
    })
    r = client.delete("/api/holdings/TSLA")
    assert r.status_code == 200


# ============ v2 cash CRUD ============

def test_cash_deposit_any_currency(client, tmp_store):
    """v2: 任意币种（USD）"""
    r = client.post("/api/cash/USD/deposit", json={"amount": 500})
    assert r.status_code == 200, r.text

    p = tmp_store.read("portfolio")
    cash = p.get("cash") or {}
    assert cash.get("USD") == 500.0


def test_cash_withdraw_negative_blocked(client, tmp_store):
    """v2 withdraw 余额不足 → 400（PM 关切的'AUD -6894 不再发生'）"""
    r = client.post("/api/cash/USD/withdraw", json={"amount": 100})
    assert r.status_code == 400
    assert "余额不足" in r.json()["detail"]


def test_cash_withdraw_invalid_currency(client):
    """非字母 / 长度错误的币种 → 400"""
    r = client.post("/api/cash/X/deposit", json={"amount": 100})
    assert r.status_code == 400


# ============ symbols search ============

def test_symbols_search_mocked(client, monkeypatch):
    """yfinance Search mock + 验证 endpoint 包装正确"""
    class FakeSearch:
        def __init__(self, q, max_results=5):
            self.quotes = [
                {"symbol": "AAPL", "shortname": "Apple Inc.", "longname": None,
                 "exchange": "NMS", "quoteType": "EQUITY"},
                {"symbol": "APLE", "shortname": "Apple Hospitality REIT",
                 "exchange": "NYQ", "quoteType": "EQUITY"},
            ][:max_results]

    import yfinance
    monkeypatch.setattr(yfinance, "Search", FakeSearch, raising=False)

    r = client.get("/api/symbols/search?q=apple&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["results"][0]["symbol"] == "AAPL"


def test_symbols_search_failure_returns_empty(client, monkeypatch):
    """yfinance Search 抛异常 → 空 list（不让前端崩）"""
    import yfinance

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("yfinance API down")

    monkeypatch.setattr(yfinance, "Search", _Boom, raising=False)

    r = client.get("/api/symbols/search?q=anything")
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ============ strategy 写端点 ============

def test_put_allocations_ok(client, tmp_store):
    r = client.put("/api/strategy/allocations", json={
        "target_allocation_stock": 0.6,
        "target_allocation_cash": 0.4,
    })
    assert r.status_code == 200, r.text
    s = tmp_store.read("strategy")
    assert s.get("target_allocation_stock") == 0.6
    assert s.get("target_allocation_cash") == 0.4


def test_put_allocations_must_sum_to_one(client, tmp_store):
    """0.6 + 0.3 = 0.9 schema 拒绝，且原值未被破坏（rollback 验证）"""
    before = tmp_store.read("strategy").get("target_allocation_stock")

    r = client.put("/api/strategy/allocations", json={
        "target_allocation_stock": 0.6,
        "target_allocation_cash": 0.3,
    })
    assert r.status_code == 400
    assert "schema" in r.text.lower() or "1.0" in r.text

    # 关键：原值未被破坏（commit-on-success 语义）
    after = tmp_store.read("strategy").get("target_allocation_stock")
    assert after == before, "schema fail 应该 rollback，原值不能被改"


def test_post_asset_new(client, tmp_store):
    r = client.post("/api/strategy/asset", json={
        "symbol": "VAS.AX",
        "display_name": "Vanguard Australia",
        "channel": "CommSec",
        "max_single_invest_cny": 8000,
    })
    assert r.status_code == 200, r.text
    s = tmp_store.read("strategy")
    syms = [a.get("symbol") for a in s.get("target_assets")]
    assert "VAS.AX" in syms


def test_post_asset_duplicate_409(client):
    """已有 NDQ.AX → 409 conflict"""
    r = client.post("/api/strategy/asset", json={
        "symbol": "NDQ.AX",
        "max_single_invest_cny": 5000,
    })
    assert r.status_code == 409


def test_put_asset_partial(client, tmp_store):
    """只改 max_single_invest_cny，其他字段保留"""
    r = client.put("/api/strategy/asset/NDQ.AX", json={"max_single_invest_cny": 15000})
    assert r.status_code == 200, r.text
    s = tmp_store.read("strategy")
    ndq = next(a for a in s.get("target_assets") if a.get("symbol") == "NDQ.AX")
    assert ndq.get("max_single_invest_cny") == 15000
    # 原有 display_name 必须保留
    assert ndq.get("display_name") == "BetaShares Nasdaq 100"


def test_put_asset_offset_out_of_range_400(client, tmp_store):
    """offset 0.5 超出 ±0.1，schema 拒绝 + rollback"""
    before = tmp_store.read("strategy")
    r = client.put("/api/strategy/asset/GC=F", json={"price_offset_pct": 0.5})
    assert r.status_code == 422  # Pydantic 在请求层就拦了

    # 即使 422，写入也未发生
    after = tmp_store.read("strategy")
    assert before.metadata == after.metadata


def test_put_asset_404(client):
    r = client.put("/api/strategy/asset/UNKNOWN", json={"max_single_invest_cny": 1000})
    assert r.status_code == 404


def test_delete_asset(client, tmp_store):
    r = client.delete("/api/strategy/asset/GC=F")
    assert r.status_code == 200
    s = tmp_store.read("strategy")
    syms = [a.get("symbol") for a in s.get("target_assets")]
    assert "GC=F" not in syms
    assert "NDQ.AX" in syms  # 另一个还在


def test_delete_last_asset_blocked(client, tmp_store):
    """schema 要求至少 1 个 asset；删到最后一个应被拒绝 + rollback"""
    # 先删一个
    client.delete("/api/strategy/asset/GC=F")
    # 再删最后一个 → 应该被 schema 挡住
    r = client.delete("/api/strategy/asset/NDQ.AX")
    assert r.status_code == 400
    s = tmp_store.read("strategy")
    syms = [a.get("symbol") for a in s.get("target_assets")]
    assert "NDQ.AX" in syms, "删失败必须 rollback，最后一个 asset 还在"


def test_strategy_writes_dont_break_body(client, tmp_store):
    """改 frontmatter 不能动 body（人类写的策略说明）"""
    # 先写一个有 body 的 strategy
    tmp_store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "X", "max_single_invest_cny": 1000}],
    }, "# 重要的人类写的策略说明\n\n不要丢这段")

    client.put("/api/strategy/asset/X", json={"max_single_invest_cny": 2000})
    s = tmp_store.read("strategy")
    assert "重要的人类写的策略说明" in s.body
    assert "不要丢这段" in s.body


def test_openapi_includes_strategy_writes(client):
    """新端点必须出现在 OpenAPI schema 里"""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/strategy/allocations" in paths
    assert "/api/strategy/asset" in paths
    assert "/api/strategy/asset/{symbol}" in paths
    # method 检查
    assert "put" in paths["/api/strategy/allocations"]
    assert "post" in paths["/api/strategy/asset"]
    assert "put" in paths["/api/strategy/asset/{symbol}"]
    assert "delete" in paths["/api/strategy/asset/{symbol}"]
