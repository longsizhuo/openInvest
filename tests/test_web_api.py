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

from openinvest.connectors import web_api
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager


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
    # 路由用 Depends(get_pm) 注入 PM：测试用 dependency_overrides 覆盖（比 patch 函数干净）；
    # monkeypatch.setitem 在用例结束后自动移除这个 override，不会泄漏到别的用例
    monkeypatch.setitem(web_api.app.dependency_overrides, web_api.get_pm, _new_pm_fake)

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

    monkeypatch.setattr("openinvest.connectors.web_api.routers.read.get_gold_snapshot", lambda offset_pct=0.0: FakeSnap(offset_pct=offset_pct))

    # 假 NDQ.AX K 线（5d）
    fake_df = pd.DataFrame(
        {"Close": [40.0, 41.0, 42.0, 41.5, 42.5]},
        index=pd.date_range("2026-04-28", periods=5, freq="D"),
    )
    monkeypatch.setattr("openinvest.connectors.web_api.routers.read.get_history_data", lambda symbol, period="5d": fake_df)

    # 委员会任务落盘目录切到 tmp，避免污染真实 memory/.committee/
    monkeypatch.setattr("openinvest.connectors.web_api.routers.committee.COMMITTEE_DIR", tmp_store.root / ".committee")

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
    monkeypatch.setattr("openinvest.connectors.web_api.routers.write.infer_offset_pct", lambda bank_price: 0.025)

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
    import openinvest.core.runner.session as cr_mod
    monkeypatch.setattr(cr_mod, "run_committee_for_symbol",
                        lambda sym, **kw: fake_result)
    # macro_view 也 mock（session 在 dispatch 之前调一次共享 macro，避免真 LLM）
    # 三路径统一架构后，session 通过 core.runner.session.run_macro_view 引用调用，
    # 这是真正生效的 mock 点（committee_runner 拆包后 cr_mod 指向 core.runner.session）
    monkeypatch.setattr(cr_mod, "run_macro_view", lambda data, **kw: "fake macro")
    monkeypatch.setattr(cr_mod, "get_macro_data", lambda: {})
    # wealth_view loader 也 mock（避免读真 user.md / 触发 LLM）
    # event_brief multi 召回也 mock 掉（避免 EventStore init）
    monkeypatch.setattr(cr_mod, "resolve_event_brief_multi", lambda syms: "")

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

    import openinvest.core.runner.session as cr_mod
    monkeypatch.setattr(cr_mod, "run_committee_for_symbol", _boom)
    # session 共享 prep 也 mock（避免真 LLM）
    monkeypatch.setattr(cr_mod, "run_macro_view", lambda data, **kw: "fake macro")
    monkeypatch.setattr(cr_mod, "get_macro_data", lambda: {})
    monkeypatch.setattr(cr_mod, "resolve_event_brief_multi", lambda syms: "")

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


# ============ GUI 同步链路回归测试 ============
# 2026-05-19 用户反馈"GUI 不显示 NapCat 同步的持仓 / 决策回放空白"。
# 后端本身没缓存（每请求 new PortfolioManager → 直接读 disk），但中间层
# 可能缓存住 GET 响应。这一组测试守住三件事：
#   1. 写 portfolio.md 后下一次 /api/holdings 立即拿到新数据
#   2. 写一份委员会 transcript 后 /api/committee_sessions 立即列出
#   3. /api/portfolio /api/holdings /api/committee_sessions 必须发
#      `Cache-Control: no-store`（防 Caddy / CDN / 浏览器缓存住）


def test_holdings_reads_disk_no_cache(client, tmp_store):
    """模拟 NapCat 写 portfolio.md：下一次 /api/holdings 必须看到新增的资产"""
    # 初始：tmp_store 已经种了 NDQ.AX + GC=F
    r1 = client.get("/api/holdings")
    syms_before = {h["symbol"] for h in r1.json()["holdings"]}
    assert syms_before == {"NDQ.AX", "GC=F"}

    # 模拟 NapCat 在锁内追加一个 AAPL 持仓（绕过 web_api 走 PortfolioManager 的写路径）
    from openinvest.core.portfolio_manager import PortfolioManager as _PM
    pm = _PM(store=tmp_store)
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        holdings.append({
            "symbol": "AAPL",
            "kind": "equity",
            "units": 10,
            "unit_label": "股",
            "avg_cost": 180.0,
            "cost_currency": "USD",
            "is_tracking_only": False,
        })
        p["holdings"] = holdings

    # 关键断言：不能依赖时间间隔，下一次 GET 必须立即看到 AAPL
    r2 = client.get("/api/holdings")
    syms_after = {h["symbol"] for h in r2.json()["holdings"]}
    assert "AAPL" in syms_after, "portfolio.md 写完后 /api/holdings 必须立刻反映"
    # 同时验证 no-store header（防中间层缓存）
    assert r2.headers.get("cache-control") == "no-store"


def test_portfolio_endpoint_no_store_header(client):
    """/api/portfolio 也必须发 no-store"""
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_portfolio_state_endpoint(client, tmp_store):
    """轻量 mtime 探针端点：agent / GUI 可以靠这个判断是否需要拉全量"""
    r = client.get("/api/portfolio/state")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert body["mtime"] is not None
    assert body["size"] > 0
    assert body["holdings_count"] == 2  # NDQ.AX + GC=F
    assert "CNY" in body["cash_currencies"]
    assert r.headers.get("cache-control") == "no-store"


def test_committee_sessions_reads_disk_after_first_run(client, tmp_store):
    """模拟首次跑完委员会：transcript 写到 memory/.committee/<date>/<sym>.md
    后，/api/committee_sessions 必须立刻能列出。

    用户报告"决策回放看不到内容"，这条守住"写完 .md 后端确实能读到"。
    """
    # 初始：没跑过委员会 → 空列表
    r0 = client.get("/api/committee_sessions")
    assert r0.status_code == 200
    assert r0.json()["count"] == 0
    assert r0.headers.get("cache-control") == "no-store"

    # 模拟 core/committee.py:_persist 落盘的格式
    committee_dir = tmp_store.root / ".committee" / "2026-05-19"
    committee_dir.mkdir(parents=True, exist_ok=True)
    (committee_dir / "NDQ_AX.md").write_text(
        "# Committee: BetaShares Nasdaq 100 ETF\n\n"
        "**Date**: 2026-05-19\n"
        "**Verdict**: HOLD (confidence 0.62)\n"
        "**Dominant view**: macro\n"
        "**Suggested allocation CNY**: 0\n\n"
        "## Macro Strategist (shared)\n\nfoo\n",
        encoding="utf-8",
    )

    # 立刻 GET 应该看到 1 条
    r1 = client.get("/api/committee_sessions")
    body = r1.json()
    assert body["count"] == 1, "transcript 写完后 list 端点必须立刻看到"
    s = body["sessions"][0]
    assert s["date"] == "2026-05-19"
    assert s["symbol"] == "NDQ_AX"
    assert s["verdict"] == "HOLD"
    assert s["confidence"] == 0.62
    assert s["suggested_alloc_cny"] == 0.0

    # 详情端点也必须能读到完整 markdown
    r2 = client.get("/api/committee_sessions/2026-05-19/NDQ_AX")
    assert r2.status_code == 200
    assert "BetaShares" in r2.json()["content"]


def test_committee_sessions_lists_multiple_dates_reverse_sorted(client, tmp_store):
    """多天的 transcript 必须按日期倒序"""
    base = tmp_store.root / ".committee"
    for date in ["2026-05-17", "2026-05-18", "2026-05-19"]:
        d = base / date
        d.mkdir(parents=True, exist_ok=True)
        (d / "GC_F.md").write_text(
            f"# Committee\n**Date**: {date}\n"
            "**Verdict**: HOLD (confidence 0.5)\n"
            "**Dominant view**: macro\n"
            "**Suggested allocation CNY**: 0\n",
            encoding="utf-8",
        )

    r = client.get("/api/committee_sessions?limit=10")
    sessions = r.json()["sessions"]
    dates = [s["date"] for s in sessions]
    assert dates == ["2026-05-19", "2026-05-18", "2026-05-17"], "必须按日期倒序"


# ---------- Skill-parity 端点（远端模式 hub-and-spoke）----------

@pytest.fixture
def skill_client(client, monkeypatch):
    """skill-parity 端点用的 client。

    services/skill_views.py 的 builder 走**函数内延迟 import**（utils.exchange_fee /
    utils.gold_price / utils.fx），所以要 patch 源模块属性，client fixture 只 patch
    了 web_api 命名空间里的别名。
    """
    import openinvest.utils.exchange_fee as ef
    import openinvest.utils.fx as fx
    import openinvest.utils.gold_price as gp

    @dataclass
    class FakeSnap:
        gold_usd_per_oz: float = 2400.0
        usdcny_rate: float = 7.2
        spot_cny_per_gram: float = 1100.0
        bank_cny_per_gram: float = 1113.2
        offset_pct: float = 0.012
        is_stale: bool = False

    fake_df = pd.DataFrame(
        {"Close": [40.0, 41.0, 42.0, 41.5, 42.5]},
        index=pd.date_range("2026-04-28", periods=5, freq="D"),
    )
    monkeypatch.setattr(ef, "get_history_data", lambda symbol, period="5d": fake_df)
    monkeypatch.setattr(gp, "get_gold_snapshot", lambda offset_pct=0.0: FakeSnap(offset_pct=offset_pct))
    monkeypatch.setattr(fx, "total_portfolio_value_cny", lambda pm, prices, base="CNY": (100000.0, "ok"))
    return client


# cmd_status 的输出 key 集——/api/skill/status 必须与 CLI 同形状（防 local/remote 漂移）
_CLI_STATUS_KEYS = {
    "user", "cash", "ndq", "gold", "all_holdings",
    "total_assets_cny", "fx", "live_prices",
}


def test_skill_status_matches_cli_shape(skill_client):
    r = skill_client.get("/api/skill/status")
    assert r.status_code == 200
    b = r.json()
    assert set(b.keys()) == _CLI_STATUS_KEYS, (
        f"/api/skill/status 输出形状漂移：{set(b.keys()) ^ _CLI_STATUS_KEYS}"
    )
    assert b["cash"]["cny"] == 12345.67
    assert b["user"]["name"] == "TestUser"
    assert b["total_assets_cny"] == 100000.0
    syms = [h["symbol"] for h in b["all_holdings"]]
    assert "NDQ.AX" in syms and "GC=F" in syms


def test_skill_strategy(skill_client):
    r = skill_client.get("/api/skill/strategy")
    assert r.status_code == 200
    b = r.json()
    assert set(b.keys()) == {"strategy", "long_term_insights", "insights_count"}
    syms = [a["symbol"] for a in b["strategy"]["target_assets"]]
    assert "NDQ.AX" in syms


def test_skill_history(skill_client, tmp_store):
    tmp_store.append_history({
        "ts_origin": "2026-06-01T10:00:00+08:00", "action": "buy",
        "symbol": "NDQ.AX", "units": 10, "price": 42.0, "source": "test",
    })
    r = skill_client.get("/api/skill/history?n=5")
    assert r.status_code == 200
    b = r.json()
    assert set(b.keys()) == {"recent_trades", "recent_debates"}
    assert b["recent_trades"][0]["symbol"] == "NDQ.AX"


def test_skill_what_if_pct(skill_client):
    r = skill_client.post("/api/skill/what_if", json={"symbol": "NDQ.AX", "pct": -5})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok"
    assert "NDQ.AX" in b["breakdown"]
    assert b["breakdown"]["NDQ.AX"]["scenario_price"] == pytest.approx(42.5 * 0.95)


def test_skill_what_if_unknown_symbol_is_cli_style_error(skill_client):
    """域内错误保持 CLI 语义：HTTP 200 + status=error dict（remote 端原样打印）"""
    r = skill_client.post("/api/skill/what_if", json={"symbol": "AAPL", "pct": -5})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "error"
    assert "AAPL" in b["error"]


def test_skill_buy_appends_history_with_remote_source(skill_client, tmp_store):
    r = skill_client.post("/api/skill/buy", json={
        "symbol": "510300.SS", "units": 1000, "price": 4.2, "kind": "etf",
    })
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["action"] == "new"

    pm = PortfolioManager(store=tmp_store)
    h = pm.holdings.find("510300.SS")
    assert h and h["units"] == 1000
    # 同步扣现金：12345.67 - 4200 = 8145.67
    assert pm.cash_amount("CNY") == pytest.approx(8145.67)
    trades = tmp_store.read_history()
    assert trades[-1]["source"] == "skill_remote"


def test_skill_sell_insufficient_units_is_400(skill_client):
    r = skill_client.post("/api/skill/sell", json={
        "symbol": "NDQ.AX", "units": 99999, "price": 42.0,
    })
    assert r.status_code == 400
    assert "99999" in r.json()["detail"]


def test_skill_sell_returns_cash(skill_client, tmp_store):
    r = skill_client.post("/api/skill/sell", json={
        "symbol": "NDQ.AX", "units": 28, "price": 42.0,
    })
    assert r.status_code == 200
    b = r.json()
    assert b["remaining_units"] == 100.0
    pm = PortfolioManager(store=tmp_store)
    # 卖出按 cost_currency=AUD 还现金：100 + 28*42 = 1276
    assert pm.cash_amount("AUD") == pytest.approx(1276.0)


def test_doctor_endpoint(skill_client, monkeypatch):
    """hub 视角 doctor：结构与 cmd_doctor 一致；删 LLM key 走 skipped 路径避免外网实测"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = skill_client.get("/api/doctor")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] in {"ready", "needs_setup"}
    assert {"ready_for_subcommands", "coordinator_ready", "direct_ready", "checks"} <= set(b.keys())
    names = [c["name"] for c in b["checks"]]
    assert "memory_initialized" in names
    mem = next(c for c in b["checks"] if c["name"] == "memory_initialized")
    assert mem["status"] == "ok", "seeded memory 应判定已初始化"


# ---------- Committee prepare/save RPC（coordinator 路径远端化）----------

def test_committee_prepare_unknown_symbol_cli_style_error(skill_client):
    r = skill_client.post("/api/committee/prepare", json={"symbol": "AAPL"})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "error"
    assert "AAPL" in b["error"] and "target_assets" in b["error"]


def test_committee_prepare_returns_self_contained_brief(skill_client, monkeypatch):
    """prepare 输出自包含 brief：prompts 6 段 + 确定性事实块 + instructions

    mock 口径对齐 tests/test_prepare_committee.py:_mock_world（同一个 service 函数）
    """
    import openinvest.core.runner.coordinator as cr
    import openinvest.core.regime_probability as rp
    import openinvest.jobs.daily_report_builder as drb
    import openinvest.utils.exchange_fee as ef
    import openinvest.utils.fx as fx

    fake_df = pd.DataFrame(
        {"Close": [100.0 + i for i in range(200)]},
        index=pd.date_range("2025-10-01", periods=200),
    )
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **k: fake_df)
    monkeypatch.setattr(ef, "analyze_multi_timeframe", lambda *a, **k: "MOCK_MARKET")
    monkeypatch.setattr(ef, "get_macro_data", lambda: "MOCK_MACRO")
    monkeypatch.setattr(cr, "load_sentiment_brief", lambda *a, **k: "SENT_SENTINEL")
    monkeypatch.setattr(cr, "load_valuation_brief", lambda *a, **k: "VAL_SENTINEL")
    monkeypatch.setattr(cr, "load_prior_insights", lambda *a, **k: "")
    monkeypatch.setattr(rp, "get_regime_forward_summary", lambda *a, **k: None)
    # coordinator 改用 build_reentry_reference（取回结构化 profile，与 session 路径对齐）
    monkeypatch.setattr(rp, "build_reentry_reference", lambda *a, **k: ("REENTRY_SENTINEL", None))
    monkeypatch.setattr(drb, "portfolio_summary_text", lambda *a, **k: "MOCK_PORTFOLIO")
    monkeypatch.setattr(fx, "total_portfolio_value_cny", lambda *a, **k: (0.0, "ok"))

    r = skill_client.post("/api/committee/prepare", json={"symbol": "NDQ.AX"})
    assert r.status_code == 200
    b = r.json()
    assert b["asset"]["symbol"] == "NDQ.AX"
    assert set(b["prompts"].keys()) == {
        "macro_strategist", "quant_round1", "risk_round1",
        "quant_round2_after_risk", "risk_round2_after_quant", "cio",
    }
    assert b["sentiment_brief"] == "SENT_SENTINEL"
    assert b["valuation_brief"] == "VAL_SENTINEL"
    assert b["reentry_reference"] == "REENTRY_SENTINEL"
    assert "INDEP_DEFENSE_FLAG" in b["instructions"]
    assert b["save_command"].endswith("save_committee NDQ.AX")


def test_committee_save_roundtrip(skill_client, tmp_store):
    transcript = (
        "=== MACRO ===\nmacro view text\n"
        "=== QUANT_R1 ===\nquant r1\n"
        "=== RISK_R1 ===\nrisk r1\n"
        "=== CIO ===\nverdict: HOLD\nconfidence: 0.55\nalloc_cny: 0\n"
    )
    r = skill_client.post("/api/committee/save", json={
        "symbol": "NDQ.AX", "transcript": transcript,
    })
    assert r.status_code == 200
    b = r.json()
    from pathlib import Path
    saved = Path(b["saved"])
    assert saved.exists(), "transcript 应落盘 hub memory/.committee"
    assert str(tmp_store.root) in str(saved), "必须落在（测试隔离的）memory root 下"
    assert "QUANT_R1" in saved.read_text(encoding="utf-8")
    assert "verdict" in b["verdict"] and "confidence" in b["verdict"]


def test_committee_save_empty_transcript_400(skill_client):
    r = skill_client.post("/api/committee/save", json={
        "symbol": "NDQ.AX", "transcript": "   \n",
    })
    assert r.status_code == 400


def test_committee_run_summary_includes_cio_memo(client, monkeypatch):
    """by_asset summary 必须带 cio_memo —— 远端 CLI run_committee 靠它渲染 memo"""
    from types import SimpleNamespace
    fake_verdict = {"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0, "dominant_view": "risk"}
    fake_result = {
        "asset": "NDQ.AX",
        "verdict": fake_verdict,
        "report": SimpleNamespace(cio_memo="## verdict\nHOLD — memo text"),
        "debate": {"max_rounds": 1, "final_round": 1, "converged": True,
                   "quant_history": ["q1"], "risk_history": ["r1"]},
    }
    import openinvest.core.runner.session as cr_mod
    monkeypatch.setattr(cr_mod, "run_committee_for_symbol", lambda sym, **kw: fake_result)
    monkeypatch.setattr(cr_mod, "run_macro_view", lambda data, **kw: "fake macro")
    monkeypatch.setattr(cr_mod, "get_macro_data", lambda: {})
    monkeypatch.setattr(cr_mod, "resolve_event_brief_multi", lambda syms: "")

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

    assert final is not None and final["status"] == "done", f"got {final}"
    assert final["result"]["by_asset"]["NDQ.AX"]["cio_memo"] == "## verdict\nHOLD — memo text"


# ---------- 可选 bearer token 鉴权（INVEST_API_TOKEN）----------
# TestClient 的 client.host 是 "testclient"（非 loopback）→ 正好测强制路径

def test_auth_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    assert client.get("/api/strategy").status_code == 200


def test_auth_enforced_when_token_set(client, monkeypatch):
    monkeypatch.setenv("INVEST_API_TOKEN", "hub-secret-123")

    # 无 token → 401；错 token → 401
    assert client.get("/api/strategy").status_code == 401
    r = client.get("/api/strategy", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # 响应体绝不回显 token
    assert "hub-secret-123" not in r.text

    # 正确 token → 200
    r = client.get("/api/strategy", headers={"Authorization": "Bearer hub-secret-123"})
    assert r.status_code == 200

    # /api/health 豁免（探活）
    assert client.get("/api/health").status_code == 200

    # 写端点同样被保护
    r = client.post("/api/skill/buy", json={"symbol": "X", "units": 1, "price": 1})
    assert r.status_code == 401


def test_auth_no_loopback_exemption(client, monkeypatch):
    """#106：token 设置后 loopback 来源也强制鉴权——反代下 client.host 恒为
    127.0.0.1，豁免等于把 token 变成摆设。"""
    monkeypatch.setenv("INVEST_API_TOKEN", "hub-secret-123")
    from starlette.testclient import TestClient
    from openinvest.connectors.web_api import app
    lo = TestClient(app, client=("127.0.0.1", 50000))
    assert lo.get("/api/strategy").status_code == 401
    assert lo.get("/api/strategy",
                  headers={"Authorization": "Bearer hub-secret-123"}).status_code == 200
    assert lo.get("/api/health").status_code == 200


# ---------- /api/config（ADR-017 config-via-API）----------

def test_config_endpoints_roundtrip(client):
    """GET/PUT/DELETE /api/config：白名单生效 + 校验 + 落盘往返（tmp store 隔离）。"""
    from openinvest.core.config import reset_config
    reset_config()

    # GET 默认
    r = client.get("/api/config")
    assert r.status_code == 200
    items = {it["key"]: it for it in r.json()["items"]}
    assert set(items) == {
        "language.invest_lang",
        "verdict.concentration_lens_enabled", "verdict.risk_profile",
        "verdict.cash_opportunity_cost_rule_enabled",  # ADR-024
        "verdict.gold_defense_dca_enabled", "dreaming.llm_verify_enabled",
        "dca.auto_dca_enabled", "dca.auto_dca_amount_cny",
        # ADR-017: event RAG + staleness 阈值也进 config API 白名单(558d9e9)
        "event.enabled", "event.min_severity", "event.max_rounds",
        "event.max_per_source", "event.rag_top_k", "event.rag_window_days",
        "event.rag_min_severity",
        # 2026-07-03: event_watch 扫描窗口 + 价格异动哨兵(ADR-025)
        "event.watch_schedule",
        "event.sentinel_enabled", "event.sentinel_atr_mult",
        "event.sentinel_cooldown_min", "event.sentinel_schedule",
        "staleness.price_stale_days", "staleness.hard_abort_stale_days",
    }
    assert items["verdict.concentration_lens_enabled"]["value"] is False  # ADR-020: default OFF
    assert items["verdict.concentration_lens_enabled"]["overridden"] is False

    # PUT bool override (flip to True, the non-default)
    r = client.put("/api/config", json={"key": "verdict.concentration_lens_enabled", "value": True})
    assert r.status_code == 200
    cl = {it["key"]: it for it in r.json()["items"]}["verdict.concentration_lens_enabled"]
    assert cl["value"] is True and cl["overridden"] is True

    # PUT enum
    assert client.put("/api/config", json={"key": "verdict.risk_profile", "value": "aggressive"}).status_code == 200
    # PUT float（DCA 金额，str → float 归一）+ 负值 400
    rf = client.put("/api/config", json={"key": "dca.auto_dca_amount_cny", "value": "150"})
    assert rf.status_code == 200
    assert {it["key"]: it for it in rf.json()["items"]}["dca.auto_dca_amount_cny"]["value"] == 150.0
    assert client.put("/api/config", json={"key": "dca.auto_dca_amount_cny", "value": -5}).status_code == 400
    # 非白名单 → 400；enum 非法 → 400
    assert client.put("/api/config", json={"key": "verdict.alloc_cny_ceiling", "value": 1}).status_code == 400
    assert client.put("/api/config", json={"key": "verdict.risk_profile", "value": "yolo"}).status_code == 400

    # DELETE 复位
    r = client.delete("/api/config/verdict.concentration_lens_enabled")
    assert r.status_code == 200
    cl = {it["key"]: it for it in r.json()["items"]}["verdict.concentration_lens_enabled"]
    assert cl["value"] is False and cl["overridden"] is False  # ADR-020: default OFF
    # 非白名单 delete → 404
    assert client.delete("/api/config/verdict.alloc_cny_ceiling").status_code == 404
    reset_config()
