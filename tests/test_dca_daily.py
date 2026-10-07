"""jobs/dca_daily.py 测试：每日自动定投（子弹池 external_funding）+ 幂等

覆盖：
- auto_dca_enabled=False → 跳过，不动账本
- 启用后按 amount_cny 记一笔 external_funding 买入：持仓增加、**cash 不动**
- 同日重复跑不二次记账（state_claim 幂等闸，ADR-016）
- 拉不到价 → 跳过且 unclaim（同日可重试）
- 休市（最新 bar 不是今天：节假日/周末）→ skip(market_closed)、不动账本、unclaim 可重试
- 行情源没拉到（is_stale）→ skip(stale_quote)；同日 18:30 / 21:30 重跑补记且只记一次
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openinvest.core.config import reset_config
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.quotes import QuoteSnapshot
import openinvest.jobs.dca_daily as dca


def _seed(tmp_path: Path, cash=None, holdings=None) -> MemoryStore:
    """临时 memory 里 seed user/strategy/portfolio，避免污染真实 memory/"""
    s = MemoryStore(tmp_path / "memory")
    s.write("user", "user", {"display_name": "T", "risk_tolerance": "Aggressive",
                              "exchange_buffer_cny": 0.0}, "")
    s.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "510300.SS", "max_single_invest_cny": 6000.0}],
    }, "")
    s.write("portfolio", "state", {
        "cash": cash if cash is not None else {"CNY": 30000.0},
        "holdings": holdings if holdings is not None else [],
        "schema_version": 2,
    }, "")
    return s


@pytest.fixture(autouse=True)
def _reset_config_each():
    """env 改动需 reset config 缓存才生效；前后都 reset 防跨 test 污染"""
    reset_config()
    yield
    reset_config()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """tmp memory（已 seed 510300 300 股 @5.0 + ¥30k 现金）+ 把 PortfolioManager() 指过去"""
    s = _seed(tmp_path, cash={"CNY": 30000.0}, holdings=[
        {"symbol": "510300.SS", "kind": "etf", "units": 300.0, "avg_cost": 5.0,
         "unit_label": "股", "cost_currency": "CNY", "proxy_kind": "direct"},
    ])
    from openinvest.core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
    return s


def _fixed_quote(price=5.0, bar_date=None, is_stale=False):
    return lambda holding: QuoteSnapshot(
        symbol=str(holding.get("symbol")), price=price, currency="CNY", unit="股",
        last_updated=bar_date, is_stale=is_stale)


def _sh_date(days_ago=0) -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_disabled_skips(seeded, monkeypatch):
    """auto_dca_enabled 默认 False → 跳过，账本不动"""
    monkeypatch.delenv("INVEST_DCA_AUTO_DCA_ENABLED", raising=False)
    out = dca.run()
    assert out["status"] == "skipped"
    assert out["reason"] == "auto_dca_disabled"
    pm = PortfolioManager(seeded)
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)
    assert pm.find_holding("510300.SS")["units"] == 300.0


def test_enabled_no_symbols_skips(seeded, monkeypatch):
    """启用但没配 symbols → 跳过"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.delenv("INVEST_DCA_AUTO_DCA_SYMBOLS", raising=False)
    out = dca.run()
    assert out["status"] == "skipped"
    assert out["reason"] == "no_dca_symbols"


def test_enabled_records_external_funding_buy(seeded, monkeypatch):
    """启用后：按 ¥100/5.0=20 股记一笔 external_funding 买入，持仓 +20、cash 不动"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "100")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out = dca.run()
    assert out["status"] == "success"
    r = out["results"][0]
    assert r["status"] == "bought" and r["units"] == pytest.approx(20.0)

    pm = PortfolioManager(seeded)
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)         # 子弹池现金不动
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(320.0)  # 300 + 20
    # history 留 external_funding 审计
    assert pm.store.read_history()[-1]["funding_source"] == "external_funding"


def test_idempotent_same_day(seeded, monkeypatch):
    """同日跑两次：第二次该 symbol skipped，持仓只加一次"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    dca.run()
    out2 = dca.run()
    assert out2["results"][0]["status"] == "skipped"
    assert out2["results"][0]["reason"] == "already_dca_today"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(320.0)  # 只加一次


def test_unheld_symbol_skipped(seeded, monkeypatch):
    """未持有的 symbol → 跳过(not_tracked)：不猜币种（避免把 USD 价当 CNY 记错账）"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "AAPL")  # 未持有
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(200.0))  # 即便能取价也不该记
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "not_tracked"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("AAPL") is None
    assert pm.cash_amount("CNY") == pytest.approx(30000.0)


def test_units_too_small_skipped_and_unclaims(seeded, monkeypatch):
    """金额/价格使 units 舍入为 0 → 跳过(amount_too_small)且 unclaim（不调 buy 报错、不卡死）"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_AMOUNT_CNY", "0.000001")  # /5 → round6 = 0
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "amount_too_small"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0  # 没买


def test_quote_exception_isolated_not_aborting(seeded, monkeypatch):
    """取价抛异常 → 记 error 并继续（不 raise 中断整批）；unclaim 后同日可重试"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")

    def boom(h):
        raise RuntimeError("yf down")
    monkeypatch.setattr(dca, "get_quote", boom)
    out = dca.run()                       # 不应抛
    assert out["status"] == "success"
    assert out["results"][0]["status"] == "error"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0  # 没买

    # unclaim 后同日重试：有价 → 成交
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    assert dca.run()["results"][0]["status"] == "bought"


def test_no_price_skips_and_unclaims(seeded, monkeypatch):
    """拉不到价 → 跳过且 unclaim；同日换成有价能重试成交（幂等闸不卡死）"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", lambda h: None)
    out = dca.run()
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["reason"] == "no_price"

    # unclaim 后同日重试：这次有价 → 成交
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0))
    out2 = dca.run()
    assert out2["results"][0]["status"] == "bought"
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(320.0)


def test_market_closed_stale_bar_skips_without_ledger_write(seeded, monkeypatch):
    """2026-10 国庆事故：休市日 yfinance 最新 bar 是节前 → 不得按旧价记账。

    skip(market_closed)：持仓 / 流水 / 幂等闸全不动（三次运行都一样）。
    """
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date(days_ago=1)))
    history_before = len(PortfolioManager(seeded).store.read_history())

    for _ in range(3):
        out = dca.run()
        assert out["results"][0] == {"symbol": "510300.SS", "status": "skipped",
                                     "reason": "market_closed"}
    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == 300.0
    assert len(pm.store.read_history()) == history_before
    assert pm.store.state_get("dca_applied", []) == []   # 已 unclaim


def test_trading_day_retry_books_exactly_once(seeded, monkeypatch):
    """交易日 15:30 行情源抖动（旧 bar + is_stale）→ stale_quote 跳过；18:30 拿到今天的 bar
    → 记一次；21:30 → already_dca_today。整天只记一笔（ADR-016）。"""
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_ENABLED", "true")
    monkeypatch.setenv("INVEST_DCA_AUTO_DCA_SYMBOLS", "510300.SS")
    history_before = len(PortfolioManager(seeded).store.read_history())

    monkeypatch.setattr(dca, "get_quote",
                        _fixed_quote(5.0, bar_date=_sh_date(days_ago=1), is_stale=True))
    assert dca.run()["results"][0]["reason"] == "stale_quote"          # 15:30
    monkeypatch.setattr(dca, "get_quote", _fixed_quote(5.0, bar_date=_sh_date()))
    assert dca.run()["results"][0]["status"] == "bought"               # 18:30
    assert dca.run()["results"][0]["reason"] == "already_dca_today"    # 21:30

    pm = PortfolioManager(seeded)
    assert pm.find_holding("510300.SS")["units"] == pytest.approx(320.0)
    assert len(pm.store.read_history()) == history_before + 1


def test_schedule_runs_three_times_after_close():
    """yml 排程：平日北京 15:30 / 18:30 / 21:30（收盘后首跑 + 两次补记机会），周末不跑。"""
    import yaml
    from openinvest.scheduler.cron import crontab_trigger
    cfg = yaml.safe_load((Path(dca.__file__).with_suffix(".yml")).read_text(encoding="utf-8"))
    trig = crontab_trigger(cfg["schedule"], timezone=cfg["timezone"])
    now = datetime(2026, 10, 9, 0, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周五
    fires = []
    for _ in range(4):
        nxt = trig.get_next_fire_time(None, now)
        fires.append(nxt.strftime("%a %H:%M"))
        now = nxt + timedelta(seconds=1)
    assert fires == ["Fri 15:30", "Fri 18:30", "Fri 21:30", "Mon 15:30"]
