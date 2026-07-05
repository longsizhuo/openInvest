"""tests/test_trades_portfolio_sync.py — 记账 → 持仓同步集成测试

覆盖：
- planned→executed BUY 后 portfolio.md 出现新 holding
- executed SELL 全部后 holding 被 remove
- 连续多笔 BUY 后 avg_cost 是加权平均（核心金融正确性）
- price=None 的市价单：仅 units 变化，avg_cost 不受影响
- SELL 但 portfolio 中无持仓：portfolio_synced=False，不崩溃
- intended_date 场景：记 intended_date=未来 → 后来 executed → portfolio 同步
- _sync_trade_to_portfolio 直接单元测试（不依赖 FastAPI 运行时）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# 确保 repo root 在 sys.path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.db.trades_db import TradesDB


# ============ 测试辅助：构建临时 MemoryStore + PortfolioManager ============

def _make_store(tmp_path: Path, holdings: Optional[list] = None) -> MemoryStore:
    """返回已 seed 的临时 MemoryStore（v2 格式）"""
    store = MemoryStore(tmp_path / "memory")
    holdings = holdings if holdings is not None else []

    store.write("user", "user", {
        "display_name": "TestUser",
        "risk_tolerance": "Balanced",
        "exchange_buffer_cny": 0.0,
    }, "# user body")

    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "NDQ.AX", "display_name": "NDQ ETF"}],
    }, "# strategy body")

    store.write("portfolio", "state", {
        "cash": {"CNY": 10000.0, "AUD": 5000.0},
        "holdings": holdings,
        "schema_version": 2,
    }, "# portfolio body")

    return store


def _make_pm(tmp_path: Path, holdings: Optional[list] = None) -> PortfolioManager:
    """返回指向临时目录的 PortfolioManager"""
    store = _make_store(tmp_path, holdings=holdings)
    return PortfolioManager(store)


# ============ _sync_trade_to_portfolio 的直接单元测试 ============
# 这些测试直接调用内部函数，不需要 FastAPI 运行时，速度快

def _call_sync(trade: Dict[str, Any], pm: PortfolioManager):
    """把 _sync_trade_to_portfolio 的逻辑抽出来，用真实 pm 跑"""
    # 复现 web_api 中的 _sync_trade_to_portfolio 逻辑，但注入真实 pm
    from openinvest.connectors.web_api import _sync_trade_to_portfolio as _web_sync
    # 通过 patch PortfolioManager() 构造函数，注入已准备好的 pm
    with patch("openinvest.connectors.web_api.routers.trades.PortfolioManager", return_value=pm):
        return _web_sync(trade)


class TestBuySync:
    """BUY 方向同步测试"""

    def test_buy_creates_new_holding(self, tmp_path):
        """BUY 一个新 symbol → portfolio.md 出现新 holding"""
        pm = _make_pm(tmp_path)

        trade = {
            "symbol": "NDQ.AX",
            "direction": "BUY",
            "units": 10.0,
            "price": 130.0,
            "cost_currency": "AUD",
        }
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding is not None
        assert holding["symbol"] == "NDQ.AX"
        assert holding["units"] == pytest.approx(10.0)
        assert holding["avg_cost"] == pytest.approx(130.0)

        # 验证 portfolio.md 真的写入了（重新读一个 pm）
        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h is not None
        assert h["units"] == pytest.approx(10.0)
        assert h["avg_cost"] == pytest.approx(130.0)

    def test_reload_failure_after_commit_still_reports_synced(self, tmp_path):
        """#127 code review 发现：with_portfolio_tx 已落盘后，pm._reload() 若抛异常，
        不能让 _sync_trade_to_portfolio 误报 synced=False——那会让调用方回退状态 +
        允许重试，对已经落盘一次的 delta 重放第二次（双花）。"""
        pm = _make_pm(tmp_path)

        trade = {
            "symbol": "NDQ.AX",
            "direction": "BUY",
            "units": 10.0,
            "price": 130.0,
            "cost_currency": "AUD",
        }
        with patch.object(PortfolioManager, "_reload", side_effect=RuntimeError("boom")):
            synced, holding = _call_sync(trade, pm)

        # 落盘已经发生（with_portfolio_tx 正常退出），_reload 失败不该让结果变 False
        assert synced is True
        assert holding is not None

        # 用一个全新的 pm（不共享内存态）验证写入真的落盘了，且只应用了一次
        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h is not None
        assert h["units"] == pytest.approx(10.0)

    def test_buy_deducts_cash_in_cost_currency(self, tmp_path):
        """金融视角红线：BUY 同步扣 cash[cost_currency]，避免账本失衡

        Fresh Claude 在端到端测试发现：之前 PATCH executed 只更新 holdings
        不动 cash → 总资产凭空多出股票。这里断言 BUY 1300 AUD 后 AUD 现金
        从 5000 → 3700，CNY 现金不动（5000 → 5000）。
        """
        pm = _make_pm(tmp_path)
        trade = {"symbol": "NDQ.AX", "direction": "BUY",
                 "units": 10.0, "price": 130.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)
        assert synced is True
        assert "_cash_delta" in holding
        assert "-1,300.00 AUD" in holding["_cash_delta"]

        pm2 = PortfolioManager(pm.store)
        assert pm2.cash_amount("AUD") == pytest.approx(3700.0)  # 5000 - 1300
        assert pm2.cash_amount("CNY") == pytest.approx(10000.0)  # 不动

    def test_sell_adds_cash_in_cost_currency(self, tmp_path):
        """金融视角对称：SELL 同步加 cash"""
        existing = [{"symbol": "NDQ.AX", "units": 20.0, "avg_cost": 100.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)
        trade = {"symbol": "NDQ.AX", "direction": "SELL",
                 "units": 5.0, "price": 130.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)
        assert synced is True
        assert "+650.00 AUD" in holding.get("_cash_delta", "")

        pm2 = PortfolioManager(pm.store)
        assert pm2.cash_amount("AUD") == pytest.approx(5650.0)  # 5000 + 650

    def test_buy_updates_existing_holding_units(self, tmp_path):
        """BUY 已有 symbol → units 累加"""
        existing = [{"symbol": "NDQ.AX", "units": 5.0, "avg_cost": 120.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)

        trade = {"symbol": "NDQ.AX", "direction": "BUY",
                 "units": 5.0, "price": 140.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding["units"] == pytest.approx(10.0)

        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h["units"] == pytest.approx(10.0)

    def test_buy_weighted_avg_cost(self, tmp_path):
        """连续多笔 BUY 后 avg_cost 是正确的加权平均

        第一笔：5 股 @ 120 AUD  → 成本 600，均价 = 600/5 = 120
        第二笔：5 股 @ 140 AUD  → 成本 700
        合计：10 股，均价 = (5*120 + 5*140) / 10 = 130.0 AUD
        """
        pm = _make_pm(tmp_path)

        # 第一笔
        trade1 = {"symbol": "NDQ.AX", "direction": "BUY",
                  "units": 5.0, "price": 120.0, "cost_currency": "AUD"}
        _call_sync(trade1, pm)
        # _reload 后 pm 已更新；第二次 _call_sync 会再实例化 pm（用同一 store）
        pm._reload()

        # 第二笔（store 已有第一笔的数据）
        trade2 = {"symbol": "NDQ.AX", "direction": "BUY",
                  "units": 5.0, "price": 140.0, "cost_currency": "AUD"}
        pm2 = PortfolioManager(pm.store)  # 读最新 portfolio
        _call_sync(trade2, pm2)

        # 验证加权均价
        pm3 = PortfolioManager(pm.store)
        h = pm3.find_holding("NDQ.AX")
        assert h is not None
        assert h["units"] == pytest.approx(10.0)
        # (5*120 + 5*140) / 10 = 130.0
        assert h["avg_cost"] == pytest.approx(130.0, rel=1e-4)

    def test_buy_price_none_only_updates_units(self, tmp_path):
        """price=None（市价单）BUY：units 增加，avg_cost 保持原值不变"""
        existing = [{"symbol": "NDQ.AX", "units": 5.0, "avg_cost": 120.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)

        trade = {"symbol": "NDQ.AX", "direction": "BUY",
                 "units": 3.0, "price": None, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding["units"] == pytest.approx(8.0)  # 5 + 3
        # avg_cost 不变（无价格，无法重算）
        assert holding["avg_cost"] == pytest.approx(120.0)

        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h["avg_cost"] == pytest.approx(120.0)


class TestSellSync:
    """SELL 方向同步测试"""

    def test_sell_partial_reduces_units(self, tmp_path):
        """部分卖出：units 减少，avg_cost 不变"""
        existing = [{"symbol": "NDQ.AX", "units": 10.0, "avg_cost": 130.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)

        trade = {"symbol": "NDQ.AX", "direction": "SELL",
                 "units": 3.0, "price": 150.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding["units"] == pytest.approx(7.0)
        # SELL 不改 avg_cost
        assert holding["avg_cost"] == pytest.approx(130.0)

        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h["units"] == pytest.approx(7.0)
        assert h["avg_cost"] == pytest.approx(130.0)

    def test_sell_all_removes_holding(self, tmp_path):
        """全部卖出（units 减到 0）→ holding 被 remove"""
        existing = [{"symbol": "NDQ.AX", "units": 5.0, "avg_cost": 130.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)

        trade = {"symbol": "NDQ.AX", "direction": "SELL",
                 "units": 5.0, "price": 150.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding["removed"] is True
        assert holding["units"] == pytest.approx(0.0)

        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h is None  # 已被移除

    def test_sell_no_holding_returns_not_synced(self, tmp_path):
        """SELL 一个 portfolio 中没有的 symbol → portfolio_synced=False，不崩溃"""
        pm = _make_pm(tmp_path)  # 空持仓

        trade = {"symbol": "NDQ.AX", "direction": "SELL",
                 "units": 5.0, "price": 150.0, "cost_currency": "AUD"}
        synced, holding = _call_sync(trade, pm)

        # 无法减仓，降级跳过
        assert synced is False

        # portfolio.md 不应新增任何 holding
        pm2 = PortfolioManager(pm.store)
        assert pm2.find_holding("NDQ.AX") is None


class TestIntendedDate:
    """intended_date 字段端到端测试"""

    def test_intended_date_stored_and_returned(self, tmp_path):
        """record 时给 intended_date=未来日期 → trade 能读回正确值"""
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX",
            direction="BUY",
            units=10.0,
            price=130.0,
            intended_date="2026-06-01",  # 计划下月执行
        )
        row = db.get_trade(trade_id)
        assert row is not None
        assert row["intended_date"] == "2026-06-01"
        assert row["status"] == "planned"

    def test_intended_date_future_then_executed_syncs_portfolio(self, tmp_path):
        """用户记 intended_date=未来 → 后来 PATCH executed → portfolio 正确同步"""
        # 1. 记一笔计划交易（intended_date=未来）
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX",
            direction="BUY",
            units=8.0,
            price=125.0,
            cost_currency="AUD",
            intended_date="2026-06-15",  # 计划成交日
        )

        # 2. 确认 status=planned，intended_date 已存
        trade = db.get_trade(trade_id)
        assert trade["status"] == "planned"
        assert trade["intended_date"] == "2026-06-15"

        # 3. 准备 portfolio（空持仓）
        pm = _make_pm(tmp_path)

        # 4. 模拟用户"执行了"→ 同步 portfolio
        synced, holding = _call_sync(trade, pm)

        assert synced is True
        assert holding is not None
        assert holding["units"] == pytest.approx(8.0)
        assert holding["avg_cost"] == pytest.approx(125.0)

        # 5. 真实 patch status（确认 db 写入）
        assert db.patch_status(trade_id, "executed") is True
        updated = db.get_trade(trade_id)
        assert updated["status"] == "executed"
        # intended_date 不被清零
        assert updated["intended_date"] == "2026-06-15"

        # 6. 验证 portfolio 确实写入
        pm2 = PortfolioManager(pm.store)
        h = pm2.find_holding("NDQ.AX")
        assert h is not None
        assert h["units"] == pytest.approx(8.0)


class TestPatchStatusIdempotency:
    """patch_trade_status 端点：重复 PATCH executed 不重复入账（finding #1 回归守卫）

    _sync_trade_to_portfolio 是累加的（BUY: cur_units + units），端点必须靠
    trade_before["status"] 判定是否真的发生 →executed 跃迁，否则双击 / 网络重试 /
    agent 重发会把持仓和现金重复计算。
    """

    def test_double_patch_executed_does_not_double_apply(self, tmp_path, monkeypatch):
        """同一笔 BUY 连续两次 PATCH executed → 持仓 + 现金只入账一次"""
        import asyncio

        from openinvest.connectors.web_api.routers import trades as trades_mod

        # 1. 临时 trades.db + 一笔 planned BUY（10 股 @ 130 AUD）
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
            cost_currency="AUD",
        )
        # _get_trades_db() 命中这个临时 db（已非 None）
        monkeypatch.setattr(trades_mod, "_trades_db", db)

        # 2. 临时 portfolio（空持仓，AUD 现金 5000）—— _sync 内 PortfolioManager() 返回它
        pm = _make_pm(tmp_path)
        monkeypatch.setattr(trades_mod, "PortfolioManager", lambda *a, **k: pm)

        # 3. 第一次 PATCH executed → 正确入账
        r1 = asyncio.run(trades_mod.patch_trade_status(trade_id, status="executed"))
        assert r1["ok"] is True
        assert r1["portfolio_synced"] is True

        pm1 = PortfolioManager(pm.store)
        assert pm1.find_holding("NDQ.AX")["units"] == pytest.approx(10.0)
        assert pm1.cash_amount("AUD") == pytest.approx(3700.0)  # 5000 - 10*130

        # 4. 第二次 PATCH executed（双击 / 重试 / agent 重发）→ 不能再入账
        r2 = asyncio.run(trades_mod.patch_trade_status(trade_id, status="executed"))
        assert r2["ok"] is True
        assert r2["portfolio_synced"] is False  # 幂等：本次未同步

        pm2 = PortfolioManager(pm.store)
        assert pm2.find_holding("NDQ.AX")["units"] == pytest.approx(10.0)  # 仍 10，非 20
        assert pm2.cash_amount("AUD") == pytest.approx(3700.0)  # 仍 3700，非 2400


class TestConcurrentClaim:
    """#109 并发重复入账：原子 CAS 跃迁保证只有一个请求赢得 planned→executed"""

    def test_concurrent_claim_exactly_one_winner(self, tmp_path):
        """16 个线程同时 claim executed → 恰好 1 个 True，其余 False（无锁三步会多个 True）"""
        import threading

        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
            cost_currency="AUD",
        )
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(16)

        def worker() -> None:
            barrier.wait()  # 尽量让 16 个 claim 同时打到 db
            won = db.claim_status_transition(trade_id, "executed")
            with results_lock:
                results.append(won)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1  # 只有一个赢家
        assert db.get_trade(trade_id)["status"] == "executed"

    def test_claim_after_executed_is_idempotent(self, tmp_path):
        """已 executed 再 claim → False（顺序重放：双击 / 超时重试 / agent 重发）"""
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
            cost_currency="AUD",
        )
        assert db.claim_status_transition(trade_id, "executed") is True
        assert db.claim_status_transition(trade_id, "executed") is False

    def test_release_claim_rolls_back_when_uncontested(self, tmp_path):
        """release_claim: 没人插手 → 正常回退到 from_status 指定的原状态"""
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
            cost_currency="AUD",
        )
        assert db.claim_status_transition(trade_id, "executed") is True
        assert db.release_claim(trade_id, from_status="executed", to_status="planned") is True
        assert db.get_trade(trade_id)["status"] == "planned"

    def test_release_claim_refuses_to_clobber_concurrent_write(self, tmp_path):
        """release_claim: A claim 到 executed 后，B 并发把行改成 cancelled；
        A 的 sync 失败要回退时 release_claim 必须发现行已不是 executed，拒绝覆盖
        B 的写入（#127 code review 发现：无条件 patch_status 回退会静默吞掉 B 的改动）"""
        db = TradesDB(db_path=str(tmp_path / "trades.db"))
        trade_id = db.record_trade(
            symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
            cost_currency="AUD",
        )
        assert db.claim_status_transition(trade_id, "executed") is True
        # 模拟并发请求 B：把行改成 cancelled（B 走的是非 executed 分支，直接 patch_status）
        assert db.patch_status(trade_id, "cancelled") is True
        # A 的同步失败，尝试回退到 "planned" —— 但行已经不是 A claim 到的 "executed" 了
        assert db.release_claim(trade_id, from_status="executed", to_status="planned") is False
        # B 的写入必须保持不变，没有被 A 的回退覆盖
        assert db.get_trade(trade_id)["status"] == "cancelled"


class TestEdgeCases:
    """边缘情况"""

    def test_sync_bad_trade_missing_symbol(self, tmp_path):
        """symbol 为空 → synced=False，不崩溃"""
        pm = _make_pm(tmp_path)
        trade = {"symbol": "", "direction": "BUY", "units": 5.0, "price": 100.0}
        synced, holding = _call_sync(trade, pm)
        assert synced is False

    def test_sync_bad_trade_zero_units(self, tmp_path):
        """units=0 → synced=False，不崩溃"""
        pm = _make_pm(tmp_path)
        trade = {"symbol": "NDQ.AX", "direction": "BUY", "units": 0.0, "price": 100.0}
        synced, holding = _call_sync(trade, pm)
        assert synced is False

    def test_portfolio_manager_init_fail_returns_not_synced(self, tmp_path):
        """PortfolioManager 初始化失败（无 memory 文件）→ synced=False，不崩溃"""
        from openinvest.connectors.web_api import _sync_trade_to_portfolio

        trade = {"symbol": "NDQ.AX", "direction": "BUY", "units": 5.0, "price": 100.0}
        # 让 PortfolioManager() 抛 FileNotFoundError
        with patch("openinvest.connectors.web_api.routers.trades.PortfolioManager",
                   side_effect=FileNotFoundError("memory missing")):
            synced, holding = _sync_trade_to_portfolio(trade)

        assert synced is False
        assert holding is None
