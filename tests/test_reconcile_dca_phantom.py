"""scripts/reconcile_dca_phantom.py：幻影 / 漏记识别 + --apply 修账幂等

场景（合成数据）：周五 9/18 正常记、周六 9/19 幻影（无 bar）、周一 9/21 漏记（有 bar 没记）、
周二 9/22 正常记；周一 9/14 处于 auto_dca_disabled 停用期 → 不算漏记。
"""
from __future__ import annotations

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from scripts import reconcile_dca_phantom as rec

SYM = "510300.SS"
BARS = {"2026-09-14": 4.0, "2026-09-18": 5.0, "2026-09-21": 4.0, "2026-09-22": 5.0}
RUNS = [("2026-09-12", True), ("2026-09-18", False)]  # 9/12 跑时停用 → 9/14 周一在停用期


def _dca_row(date, units, price):
    return {"ts_origin": f"{date}T15:00:00+08:00", "action": "buy", "symbol": SYM,
            "units": units, "price": price, "currency": "CNY", "source": "dca_daily",
            "funding_source": "external_funding"}


@pytest.fixture
def pm(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write("user", "user", {"display_name": "T"}, "")
    s.write("strategy", "strategy", {"target_allocation_stock": 0.7,
                                      "target_allocation_cash": 0.3, "target_assets": []}, "")
    s.write("portfolio", "state", {
        "cash": {"CNY": 1000.0}, "schema_version": 2,
        "holdings": [{"symbol": SYM, "kind": "etf", "units": 260.0, "avg_cost": 5.0,
                      "unit_label": "股", "cost_currency": "CNY", "proxy_kind": "direct"}],
    }, "")
    for row in (_dca_row("2026-09-18", 20.0, 5.0), _dca_row("2026-09-19", 20.0, 5.0),
                _dca_row("2026-09-22", 20.0, 5.0)):
        s.append_history(row)
    return PortfolioManager(s)


def _plan(pm):
    return rec.plan(pm.store.read_history(), BARS, RUNS, SYM, "2026-09-10", "2026-09-22",
                    today="2026-09-23")


def test_plan_finds_phantom_and_missing(pm):
    p = _plan(pm)
    assert [d for d, _ in p["phantom"]] == ["2026-09-19"]
    assert p["missing"] == [("2026-09-21", 4.0)]
    assert [d for d, _ in p["disabled"]] == ["2026-09-14"]


def test_apply_reconciles_once(pm):
    assert rec.apply(pm, SYM, _plan(pm), amount_cny=100.0) == 2
    h = PortfolioManager(pm.store).find_holding(SYM)
    # 260 - 20（幻影 @5）+ 25（9/21 ¥100 @4）= 265；成本 1300 - 100 + 100 = 1300
    assert h["units"] == pytest.approx(265.0)
    assert h["avg_cost"] == pytest.approx(1300.0 / 265.0, abs=1e-6)
    assert PortfolioManager(pm.store).cash_amount("CNY") == pytest.approx(1000.0)  # 子弹池不动
    # 漏记日占用 dca_daily 同一把幂等键（互斥）
    assert f"2026-09-21:{SYM}" in pm.store.state_get("dca_applied", [])

    # 修完复跑：plan 为空、apply 0 笔、持仓不再变
    p2 = _plan(pm)
    assert p2["phantom"] == [] and p2["missing"] == []
    assert rec.apply(pm, SYM, _plan(pm), amount_cny=100.0) == 0
    assert PortfolioManager(pm.store).find_holding(SYM)["units"] == pytest.approx(265.0)


def test_apply_failure_unclaims(pm):
    """持仓不存在 → 事务抛错，所有 claim 回滚（下次能重试），账本不动。"""
    p = _plan(pm)
    with pm.with_portfolio_tx() as doc:
        doc["holdings"] = []
    with pytest.raises(ValueError):
        rec.apply(pm, SYM, p, amount_cny=100.0)
    assert pm.store.state_get("dca_applied", []) == []
    assert pm.store.state_get("dca_reconcile_reversed", []) == []


def test_rebuild_floor_clamps_default_window(pm):
    """持仓 delete_holding 后重建过 → 默认窗口从删除次日（北京日期）起，不重复修正之前的差异"""
    assert rec.rebuild_floor(pm.store.read_history(), SYM) is None
    pm.store.append_history({"ts_origin": "2026-09-19T20:00:00+00:00", "action": "delete_holding",
                             "symbol": SYM, "source": "skill_cli"})  # 北京 9/20 凌晨
    floor = rec.rebuild_floor(pm.store.read_history(), SYM)
    assert floor == "2026-09-21"
    p = rec.plan(pm.store.read_history(), BARS, RUNS, SYM, floor, "2026-09-22", today="2026-09-23")
    assert p["phantom"] == []                      # 9/19 幻影在重建前，已被吸收
    assert p["missing"] == [("2026-09-21", 4.0)]
