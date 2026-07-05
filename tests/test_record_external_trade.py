"""tests/test_record_external_trade.py — CommSec 外部成交回报 → 账本应用测试

该路径（PortfolioManager.record_external_trade）此前零测试，两份 trade→portfolio
同步实现各自演化，靠这组测试钉住行为：

幂等性（fix [2]）：
- 同 email_id 重复应用只记一次（成功后重试不双重记账）
- apply 失败时 claim 回滚 → 下次能重试，不静默丢单

设计行为（[1]，**有意为之，非 bug**）：
- sold 无持仓仍记 cash —— CommSec 是真实已结算成交，钱真的到账
- 超卖 units 夹到 0 但按全额计 proceeds
与 web 手工路径 _sync_trade_to_portfolio（SELL 无持仓 _SkipSync）故意语义不同，
这组测试同时守住"两条路径不该被合并"。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager


def _make_pm(tmp_path: Path, holdings: Optional[list] = None) -> PortfolioManager:
    """临时目录里 seed 一个 v2 portfolio（cash CNY 10000 / AUD 5000）。"""
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {
        "display_name": "TestUser", "risk_tolerance": "Balanced",
        "exchange_buffer_cny": 0.0,
    }, "# user body")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "NDQ.AX", "display_name": "NDQ ETF"}],
    }, "# strategy body")
    store.write("portfolio", "state", {
        "cash": {"CNY": 10000.0, "AUD": 5000.0},
        "holdings": holdings if holdings is not None else [],
        "schema_version": 2,
    }, "# portfolio body")
    return PortfolioManager(store)


def _trade(**over) -> dict:
    base = {
        "symbol": "NDQ.AX", "action": "bought", "units": 10.0,
        "total_amount": 1000.0, "currency": "AUD", "email_id": "E1",
    }
    base.update(over)
    return base


# ============ 基本应用 ============

class TestApply:
    def test_bought_creates_holding_and_debits_cash(self, tmp_path):
        pm = _make_pm(tmp_path)
        pm.record_external_trade(_trade())
        h = pm.find_holding("NDQ.AX")
        assert h is not None
        assert h["units"] == pytest.approx(10.0)
        assert h["avg_cost"] == pytest.approx(100.0)  # 1000 / 10
        assert pm.cash_amount("AUD") == pytest.approx(4000.0)  # 5000 - 1000

    def test_sold_normal_reduces_units_credits_cash(self, tmp_path):
        """正常 sold（持仓够）：持有 10 卖 5 → 剩 5，cash 加 proceeds，holding 不删。"""
        existing = [{"symbol": "NDQ.AX", "units": 10.0, "avg_cost": 100.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)
        pm.record_external_trade(_trade(symbol="NDQ.AX", action="sold",
                                        units=5.0, total_amount=600.0, email_id="N1"))
        h = pm.find_holding("NDQ.AX")
        assert h is not None and h["units"] == pytest.approx(5.0)  # 剩 5，未删
        assert pm.cash_amount("AUD") == pytest.approx(5600.0)      # 5000 + 600


# ============ 幂等性（fix [2]）============

class TestIdempotency:
    def test_same_email_id_applied_once(self, tmp_path):
        """成功后重试同一封邮件 → 不二次记账（units/cash 不翻倍）。"""
        pm = _make_pm(tmp_path)
        pm.record_external_trade(_trade(email_id="DUP"))
        pm.record_external_trade(_trade(email_id="DUP"))  # 重复

        fresh = PortfolioManager(pm.store)  # 从盘重载，排除内存视图假象
        assert fresh.find_holding("NDQ.AX")["units"] == pytest.approx(10.0)
        assert fresh.cash_amount("AUD") == pytest.approx(4000.0)
        assert fresh.get_processed_emails().count("DUP") == 1

    def test_distinct_email_ids_both_applied(self, tmp_path):
        """不同 email_id 是不同成交 → 各记一次。"""
        pm = _make_pm(tmp_path)
        pm.record_external_trade(_trade(email_id="A", units=10.0, total_amount=1000.0))
        pm.record_external_trade(_trade(email_id="B", units=5.0, total_amount=600.0))
        assert pm.find_holding("NDQ.AX")["units"] == pytest.approx(15.0)
        assert pm.cash_amount("AUD") == pytest.approx(5000.0 - 1000.0 - 600.0)

    def test_no_email_id_still_applies(self, tmp_path):
        """没有 email_id（手工/脚本导入）→ 跳过幂等闸，正常应用。"""
        pm = _make_pm(tmp_path)
        t = _trade()
        t.pop("email_id")
        pm.record_external_trade(t)
        assert pm.find_holding("NDQ.AX")["units"] == pytest.approx(10.0)

    def test_apply_failure_rolls_back_claim(self, tmp_path):
        """apply 抛异常 → email_id 被 unclaim → 修复后重试能正常应用，不静默丢单。"""
        pm = _make_pm(tmp_path)
        with patch.object(pm, "with_portfolio_tx", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                pm.record_external_trade(_trade(email_id="ROLL"))
        # 失败后该邮件不应残留在 processed（否则下次被永久跳过 = 丢单）
        assert "ROLL" not in pm.get_processed_emails()

        # 故障恢复后重试 → 这次真正落账
        pm.record_external_trade(_trade(email_id="ROLL"))
        assert pm.find_holding("NDQ.AX")["units"] == pytest.approx(10.0)
        assert pm.cash_amount("AUD") == pytest.approx(4000.0)


# ============ 设计行为：CommSec = 真实结算（[1]，非 bug）============

class TestRealSettlementSemantics:
    def test_sell_no_holding_credits_cash_no_phantom_holding(self, tmp_path):
        """SELL 一个没记录过的 symbol：不凭空建仓，但记 cash（真实到账的钱）。

        与 _sync_trade_to_portfolio 的 _SkipSync 故意不同——勿合并两条路径。
        """
        pm = _make_pm(tmp_path)  # 空持仓
        pm.record_external_trade(_trade(symbol="XYZ.AX", action="sold",
                                        units=5.0, total_amount=500.0, email_id="S1"))
        assert pm.find_holding("XYZ.AX") is None       # 没凭空建仓
        assert pm.cash_amount("AUD") == pytest.approx(5500.0)  # 真实结算 → 记 cash

    def test_oversell_clamps_units_credits_full_proceeds(self, tmp_path):
        """超卖（卖出 > 持有）：units 夹到 0，但按真实成交全额计 proceeds。"""
        existing = [{"symbol": "NDQ.AX", "units": 3.0, "avg_cost": 100.0,
                     "cost_currency": "AUD", "kind": "equity"}]
        pm = _make_pm(tmp_path, holdings=existing)
        pm.record_external_trade(_trade(symbol="NDQ.AX", action="sold",
                                        units=5.0, total_amount=750.0, email_id="O1"))
        assert pm.find_holding("NDQ.AX")["units"] == pytest.approx(0.0)  # 夹到 0
        assert pm.cash_amount("AUD") == pytest.approx(5750.0)            # 全额计
