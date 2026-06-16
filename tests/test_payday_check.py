"""tests/test_payday_check.py — 月度入账幂等（finding #4）

payday_check.run() 是 cron + NapCat `/payday` 双触发。旧实现只有 last_payday 同月
粗检查（check-then-act，非原子）：add_income 改 cash 与 update_fields 改 last_payday
是两次分离的写，并发两路都能过同月检查 → 月度净收入重复入账。修复加原子
state_claim(YYYY-MM) 闸。这里守这两条：

- 顺序重跑：第一次入账，第二次同月被粗检查跳过，不重复
- 原子闸：last_payday 是旧月（粗检查放行）但本月 key 已被另一路 claim → 拦截，不重复
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
import jobs.payday_check as payday


def _make_pm(tmp_path, *, last_payday="1970-01-01", income=10000.0, expense=3000.0):
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {
        "display_name": "T", "risk_tolerance": "Balanced",
        "monthly_income_cny": income, "monthly_expenses_cny": expense,
        "last_payday": last_payday,
    }, "# user")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "NDQ.AX", "display_name": "NDQ"}],
    }, "# strategy")
    store.write("portfolio", "state", {
        "cash": {"CNY": 5000.0}, "holdings": [], "schema_version": 2,
    }, "# portfolio")
    return PortfolioManager(store)


class TestPaydayIdempotency:
    def test_first_run_credits_then_sequential_skips(self, tmp_path, monkeypatch):
        pm = _make_pm(tmp_path)
        monkeypatch.setattr(payday, "PortfolioManager", lambda: pm)

        r1 = payday.run()
        assert r1["status"] == "success"
        assert r1["net_income_cny"] == pytest.approx(7000.0)
        assert PortfolioManager(pm.store).cash_amount("CNY") == pytest.approx(12000.0)

        # 第二次（同月）：粗检查 last_payday 命中 → 跳过，不重复入账
        r2 = payday.run()
        assert r2["status"] == "skipped"
        assert PortfolioManager(pm.store).cash_amount("CNY") == pytest.approx(12000.0)

    def test_atomic_claim_blocks_concurrent_double_credit(self, tmp_path, monkeypatch):
        # last_payday 是旧月（粗检查放行），但本月 key 已被"先到的一路"claim
        pm = _make_pm(tmp_path, last_payday="1970-01-01")
        month_key = datetime.now().strftime("%Y-%m")
        assert pm.store.state_claim("paydays_applied", month_key) is True  # 模拟并发先到一路

        monkeypatch.setattr(payday, "PortfolioManager", lambda: pm)
        r = payday.run()
        assert r["status"] == "skipped"
        assert r["reason"] == "already_paid_this_month"
        # 原子闸拦住，cash 不变（无重复入账）
        assert PortfolioManager(pm.store).cash_amount("CNY") == pytest.approx(5000.0)
