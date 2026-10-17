"""账本幂等键（ADR-016 "未决"段收口）：buy / sell / deposit / withdraw 同 idempotency_key 只入账一次。

远端 hub-and-spoke 模式下 HTTP 超时重发、agent 重跑同一笔，之前都是"累加语义 + 无去重键"→ 双记。
现在 key 记在 portfolio 文档自身（PortfolioManager._ledger_tx，与 synced_trade_ids 同款），check +
append 与 cash/holdings 在同一把锁、同一次 atomic write 落盘：并发 N 路同 key 恰好一个赢家，
失败 / 中断时 key 和账本一起不落盘（不会"key 烧掉了但账没记"）。并发用线程（仿 test_state_claim_concurrency 的多进程写法；同进程并发是 MCP
--http 模式下多个客户端同时打的真实形态）。不传 key 行为完全不变。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager


def _store(root: Path, cash: float = 10000.0) -> MemoryStore:
    s = MemoryStore(root)
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "# user")
    s.write("strategy", "strategy", {"target_assets": []}, "# strategy")
    s.write("portfolio", "state", {
        "schema_version": 2, "cash": {"CNY": cash},
        "holdings": [{"symbol": "GC=F", "kind": "metal", "units": 5.0, "avg_cost": 400.0,
                      "unit_label": "克", "cost_currency": "CNY", "proxy_kind": "direct"}],
    }, "# portfolio")
    return s


def _history_actions(store: MemoryStore) -> list:
    return [r["action"] for r in store.read_history()]


def test_same_key_buy_records_once(tmp_path):
    pm = PortfolioManager(_store(tmp_path))
    r1 = pm.buy("GC=F", 2, 500.0, idempotency_key="k-1")
    r2 = pm.buy("GC=F", 2, 500.0, idempotency_key="k-1")
    assert r1["status"] == "ok"
    assert r2 == {"status": "duplicate", "idempotency_key": "k-1", "note": r2["note"]}
    assert pm.holdings.find("GC=F")["units"] == 7.0          # 5 + 2，不是 9
    assert pm.cash_amount("CNY") == pytest.approx(9000.0)    # 只扣一次 1000
    assert _history_actions(pm.store) == ["buy"]              # history 也只有一行


def test_different_keys_record_twice(tmp_path):
    pm = PortfolioManager(_store(tmp_path))
    assert pm.buy("GC=F", 1, 100.0, idempotency_key="a")["status"] == "ok"
    assert pm.buy("GC=F", 1, 100.0, idempotency_key="b")["status"] == "ok"
    assert pm.holdings.find("GC=F")["units"] == 7.0


def test_no_key_behaviour_unchanged(tmp_path):
    """不传 key = 旧行为：每次都入账（本地单进程 CLI 一次点击 = 一次意图）。"""
    pm = PortfolioManager(_store(tmp_path))
    pm.buy("GC=F", 1, 100.0)
    pm.buy("GC=F", 1, 100.0)
    assert pm.holdings.find("GC=F")["units"] == 7.0
    assert "ledger_idempotency_keys" not in pm.store.read("portfolio").metadata


def test_sell_deposit_withdraw_same_key_once(tmp_path):
    pm = PortfolioManager(_store(tmp_path))
    assert pm.sell("GC=F", 1, 600.0, idempotency_key="s")["status"] == "ok"
    assert pm.sell("GC=F", 1, 600.0, idempotency_key="s")["status"] == "duplicate"
    assert pm.holdings.find("GC=F")["units"] == 4.0
    assert pm.cash_amount("CNY") == pytest.approx(10600.0)

    assert pm.deposit_cash("CNY", 100, idempotency_key="d")["status"] == "ok"
    assert pm.deposit_cash("CNY", 100, idempotency_key="d")["status"] == "duplicate"
    assert pm.cash_amount("CNY") == pytest.approx(10700.0)

    assert pm.withdraw_cash("CNY", 700, idempotency_key="w")["status"] == "ok"
    assert pm.withdraw_cash("CNY", 700, idempotency_key="w")["status"] == "duplicate"
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)
    assert _history_actions(pm.store) == ["sell", "deposit", "withdraw"]


def test_keys_are_shared_across_operations(tmp_path):
    """同一个 key 不分操作类型：一次意图 = 一个 key，换个动词也不能再入账。"""
    pm = PortfolioManager(_store(tmp_path))
    assert pm.deposit_cash("CNY", 1, idempotency_key="x")["status"] == "ok"
    assert pm.withdraw_cash("CNY", 1, idempotency_key="x")["status"] == "duplicate"


def test_failed_op_releases_key_for_retry(tmp_path):
    """业务失败（余额不足）不得烧掉 key——否则修好余额后重试同 key 会被当 duplicate 静默丢单。"""
    pm = PortfolioManager(_store(tmp_path, cash=50.0))
    with pytest.raises(ValueError):
        pm.withdraw_cash("CNY", 100, idempotency_key="w-retry")
    pm.deposit_cash("CNY", 100)
    assert pm.withdraw_cash("CNY", 100, idempotency_key="w-retry")["status"] == "ok"
    assert pm.cash_amount("CNY") == pytest.approx(50.0)


def test_commit_failure_does_not_burn_key(tmp_path, monkeypatch):
    """commit 阶段失败（schema 校验 / 落盘）→ key 与账本一起不落盘，同 key 重试仍能入账。
    （安全评审指出的"fail-open 丢单"：独立 .state 文件先 claim 再提交会在这里烧掉 key。）"""
    import openinvest.core.portfolio_manager as pmod

    pm = PortfolioManager(_store(tmp_path))
    real = pmod.validate_portfolio
    calls = {"n": 0}

    def flaky(meta):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk/validation hiccup")
        return real(meta)

    monkeypatch.setattr(pmod, "validate_portfolio", flaky)
    with pytest.raises(RuntimeError):
        pm.deposit_cash("CNY", 100, idempotency_key="flaky")
    assert pm.deposit_cash("CNY", 100, idempotency_key="flaky")["status"] == "ok"
    assert pm.cash_amount("CNY") == pytest.approx(10100.0)


def test_key_table_is_capped(tmp_path):
    pm = PortfolioManager(_store(tmp_path))
    for i in range(505):
        pm.deposit_cash("CNY", 1, idempotency_key=f"k{i}")
    keys = pm.store.read("portfolio").metadata["ledger_idempotency_keys"]
    assert len(keys) == 500 and keys[0] == "k5" and keys[-1] == "k504"


def test_existing_position_path_same_key_once(tmp_path):
    """record_existing_position（MCP）/ buy --existing-position（CLI）走 buy(source_type=
    external_funding)，同样受幂等闸保护；现金不动。"""
    pm = PortfolioManager(_store(tmp_path))
    for _ in range(2):
        pm.buy("AAPL", 3, 150.0, currency="USD", source="mcp:existing_position",
               source_type="external_funding", idempotency_key="onboard-aapl")
    assert pm.holdings.find("AAPL")["units"] == 3
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)


def test_concurrent_same_key_exactly_one_winner(tmp_path):
    """ADR-016 核心不变量：20 路并发同 key buy → 恰好 1 次入账、19 次 duplicate，账本只动一次。"""
    _store(tmp_path)
    n = 20

    def one(_i: int):
        return PortfolioManager(MemoryStore(tmp_path)).buy(
            "GC=F", 1, 100.0, idempotency_key="race")["status"]

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(one, range(n)))

    assert results.count("ok") == 1, results
    assert results.count("duplicate") == n - 1, results
    pm = PortfolioManager(MemoryStore(tmp_path))
    assert pm.holdings.find("GC=F")["units"] == 6.0           # 5 + 1，不是 25
    assert pm.cash_amount("CNY") == pytest.approx(9900.0)      # 只扣一次 100
    assert _history_actions(pm.store) == ["buy"]
    assert pm.store.read("portfolio").metadata["ledger_idempotency_keys"] == ["race"]


def test_mcp_tools_pass_idempotency_key(tmp_path, monkeypatch):
    """MCP 层透传：record_existing_position / buy / sell / deposit / withdraw 都接 idempotency_key。"""
    import openinvest.connectors.mcp_server as m

    pm = PortfolioManager(_store(tmp_path))
    monkeypatch.setattr(m, "_pm", lambda: pm)

    assert m.record_existing_position(symbol="AAPL", units=1, price=10.0, currency="USD",
                                      idempotency_key="r")["status"] == "ok"
    assert m.record_existing_position(symbol="AAPL", units=1, price=10.0, currency="USD",
                                      idempotency_key="r")["status"] == "duplicate"
    assert m.buy(symbol="GC=F", units=1, price=100.0, idempotency_key="b")["status"] == "ok"
    assert m.buy(symbol="GC=F", units=1, price=100.0, idempotency_key="b")["status"] == "duplicate"
    assert m.sell(symbol="GC=F", units=1, price=100.0, idempotency_key="s")["status"] == "ok"
    assert m.sell(symbol="GC=F", units=1, price=100.0, idempotency_key="s")["status"] == "duplicate"
    assert m.deposit(currency="CNY", amount=1, idempotency_key="d")["status"] == "ok"
    assert m.deposit(currency="CNY", amount=1, idempotency_key="d")["status"] == "duplicate"
    assert m.withdraw(currency="CNY", amount=1, idempotency_key="w")["status"] == "ok"
    assert m.withdraw(currency="CNY", amount=1, idempotency_key="w")["status"] == "duplicate"
    assert pm.holdings.find("AAPL")["units"] == 1
    assert pm.holdings.find("GC=F")["units"] == 5.0
