"""portfolio_manager.py 完整独立测试（PM-Debt 一号红线）

覆盖：
- cash_amount("CNY"|"AUD"|"USD") happy + 空值
- holdings property 遍历（空 holdings / 单条 / 多条多币种）
- find_holding(symbol) 命中/未命中
- get_user_status() 完整逻辑：折算/is_tracking_only/current_prices=None/portfolio_value/dry_powder
- with_portfolio_tx() RMW 并发场景（multiprocessing 2 个 worker 各 deposit 100，+200 不丢）
"""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

# 确保 repo root 在 sys.path（conftest.py 已做，这里只是保险）
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import HoldingsView, PortfolioManager, _render_portfolio_body_v2


# ============ Fixture 工厂 ============

def _make_store(tmp_path: Path) -> MemoryStore:
    """返回一个以临时目录为 root 的 MemoryStore，避免污染真实 memory/"""
    return MemoryStore(tmp_path / "memory")


def _seed_docs(
    store: MemoryStore,
    *,
    cash: Optional[Dict[str, float]] = None,
    holdings: Optional[list] = None,
    risk_tolerance: str = "Balanced",
    target_assets: Optional[list] = None,
    max_single_invest_cny: float = 10000.0,
) -> None:
    """往临时 memory 里写 user / strategy / portfolio 三份文档，只写最小必要字段"""
    cash = cash if cash is not None else {"CNY": 10000.0, "AUD": 500.0}
    holdings = holdings if holdings is not None else []
    target_assets = target_assets if target_assets is not None else [
        {
            "symbol": "NDQ.AX",
            "display_name": "BetaShares Nasdaq 100 ETF",
            "max_single_invest_cny": max_single_invest_cny,
        }
    ]

    # user.md
    store.write("user", "user", {
        "display_name": "TestUser",
        "risk_tolerance": risk_tolerance,
    }, "# user body")

    # strategy.md
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": target_assets,
    }, "# strategy body")

    # portfolio.md（v2 结构）
    store.write("portfolio", "state", {
        "cash": cash,
        "holdings": holdings,
        "schema_version": 2,
    }, "# portfolio body")


@pytest.fixture
def store(tmp_path):
    """最小配置的 MemoryStore，已 seed 默认文档"""
    s = _make_store(tmp_path)
    _seed_docs(s)
    return s


@pytest.fixture
def pm(store) -> PortfolioManager:
    """基于 store fixture 的 PortfolioManager"""
    return PortfolioManager(store)


# ============ 任务 1a：cash_amount ============

class TestCashAmount:
    """pm.cash_amount(currency) 各场景"""

    def test_cny_happy(self, pm):
        """CNY 正常返回"""
        assert pm.cash_amount("CNY") == 10000.0

    def test_aud_happy(self, pm):
        """AUD 正常返回"""
        assert pm.cash_amount("AUD") == 500.0

    def test_usd_missing_returns_zero(self, pm):
        """不存在的货币返回 0.0，不抛异常"""
        assert pm.cash_amount("USD") == 0.0

    def test_lowercase_key_normalized(self, tmp_path):
        """小写 currency 参数应被 upper() 后正确查找"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 8888.0})
        pm2 = PortfolioManager(s)
        assert pm2.cash_amount("cny") == 8888.0

    def test_empty_cash_dict_returns_zero(self, tmp_path):
        """cash 为空 dict 时所有 currency 返回 0"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={})
        pm2 = PortfolioManager(s)
        assert pm2.cash_amount("CNY") == 0.0
        assert pm2.cash_amount("AUD") == 0.0

    def test_negative_balance_preserved(self, tmp_path):
        """负数余额（如 AUD 购买后欠款）应原值返回"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 5000.0, "AUD": -6894.42})
        pm2 = PortfolioManager(s)
        assert pm2.cash_amount("AUD") == pytest.approx(-6894.42)


# ============ 任务 1b：holdings property ============

class TestHoldingsProperty:
    """pm.holdings 遍历 + HoldingsView 功能"""

    def test_empty_holdings_returns_empty_view(self, tmp_path):
        """持仓为空时 HoldingsView 长度为 0"""
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=[])
        pm2 = PortfolioManager(s)
        assert len(pm2.holdings) == 0

    def test_single_holding_iterable(self, tmp_path):
        """单条 holding 能迭代，字段完整"""
        h = {"symbol": "NDQ.AX", "kind": "etf", "units": 256.0,
             "unit_label": "股", "avg_cost": 53.86, "cost_currency": "AUD",
             "channel": "CommSec", "display_name": "BetaShares Nasdaq 100 ETF",
             "proxy_kind": "direct"}
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=[h])
        pm2 = PortfolioManager(s)
        items = list(pm2.holdings)
        assert len(items) == 1
        assert items[0]["symbol"] == "NDQ.AX"
        assert items[0]["units"] == 256.0
        assert items[0]["avg_cost"] == 53.86

    def test_multiple_holdings_multi_currency(self, tmp_path):
        """多条多币种持仓全部返回"""
        holdings = [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
             "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD"},
            {"symbol": "GC=F", "kind": "metal", "units": 50.0,
             "unit_label": "克", "avg_cost": 1000.0, "cost_currency": "CNY",
             "proxy_kind": "gold_cny_per_gram"},
            {"symbol": "AAPL", "kind": "equity", "units": 10.0,
             "unit_label": "股", "avg_cost": 180.0, "cost_currency": "USD"},
        ]
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=holdings)
        pm2 = PortfolioManager(s)
        symbols = {h["symbol"] for h in pm2.holdings}
        assert symbols == {"NDQ.AX", "GC=F", "AAPL"}

    def test_holdings_len_matches_list(self, tmp_path):
        """HoldingsView.__len__ 与实际条数一致"""
        holdings = [
            {"symbol": "A", "kind": "equity", "units": 1.0, "cost_currency": "CNY"},
            {"symbol": "B", "kind": "equity", "units": 2.0, "cost_currency": "CNY"},
        ]
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=holdings)
        pm2 = PortfolioManager(s)
        assert len(pm2.holdings) == 2

    def test_holdings_tracking_only_included_in_view(self, tmp_path):
        """is_tracking_only=True 的 holding 在 pm.holdings 里可见（业务层自己过滤）"""
        holdings = [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 0.0, "cost_currency": "AUD",
             "is_tracking_only": True},
        ]
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=holdings)
        pm2 = PortfolioManager(s)
        assert len(pm2.holdings) == 1
        assert pm2.holdings[0]["is_tracking_only"] is True


# ============ 任务 1c：find_holding ============

class TestFindHolding:
    """pm.find_holding(symbol) 命中/未命中"""

    def test_find_existing_symbol(self, tmp_path):
        """能命中已有 holding"""
        h = {"symbol": "NDQ.AX", "kind": "etf", "units": 128.0, "cost_currency": "AUD"}
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=[h])
        pm2 = PortfolioManager(s)
        found = pm2.find_holding("NDQ.AX")
        assert found is not None
        assert found["units"] == 128.0

    def test_find_missing_symbol_returns_none(self, pm):
        """不存在的 symbol 返回 None 而不抛异常"""
        assert pm.find_holding("TSLA") is None

    def test_find_in_multi_holding(self, tmp_path):
        """多 holding 里精确查找到目标 symbol"""
        holdings = [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 50.0, "cost_currency": "AUD"},
            {"symbol": "GC=F", "kind": "metal", "units": 100.0, "cost_currency": "CNY",
             "proxy_kind": "gold_cny_per_gram"},
        ]
        s = _make_store(tmp_path)
        _seed_docs(s, holdings=holdings)
        pm2 = PortfolioManager(s)
        gold = pm2.find_holding("GC=F")
        assert gold is not None
        assert gold["units"] == 100.0
        assert pm2.find_holding("NDQ.AX")["units"] == 50.0


# ============ 任务 1d：get_user_status ============

class TestGetUserStatus:
    """get_user_status() 全路径：折算/追踪仓/缺价/portfolio_value/dry_powder"""

    def _make_pm_with_holdings(
        self, tmp_path, cash, holdings,
        max_single_invest_cny=10000.0
    ) -> PortfolioManager:
        s = _make_store(tmp_path)
        _seed_docs(s, cash=cash, holdings=holdings,
                   max_single_invest_cny=max_single_invest_cny)
        return PortfolioManager(s)

    def test_cash_only_portfolio(self, tmp_path):
        """无持仓时 portfolio_value = cash_cny + cash_aud * rate"""
        pm2 = self._make_pm_with_holdings(tmp_path,
                                           cash={"CNY": 10000.0, "AUD": 100.0},
                                           holdings=[])
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        assert status.cash_cny == 10000.0
        assert status.cash_aud == 100.0
        assert status.portfolio_value == pytest.approx(10000.0 + 100.0 * 4.8)

    def test_aud_holding_folded_at_exchange_rate(self, tmp_path):
        """AUD cost_currency 持仓按 exchange_rate 折算进 portfolio_value"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 0.0},
            holdings=[{"symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
                        "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
                        "proxy_kind": "direct"}]
        )
        # current_prices 有 NDQ.AX → 用 current_price 算
        status = pm2.get_user_status(current_prices={"NDQ.AX": 55.0}, exchange_rate=5.0)
        # 100 * 55 * 5 = 27500；CNY cash = 0
        assert status.portfolio_value == pytest.approx(27500.0)

    def test_cny_holding_no_exchange_rate(self, tmp_path):
        """CNY cost_currency 持仓直接加，不乘 exchange_rate"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 5000.0},
            holdings=[{"symbol": "GC=F", "kind": "metal", "units": 10.0,
                        "unit_label": "克", "avg_cost": 600.0, "cost_currency": "CNY",
                        "proxy_kind": "gold_cny_per_gram"}]
        )
        status = pm2.get_user_status(current_prices={"GC=F": 650.0}, exchange_rate=4.8)
        # 5000 + 10 * 650 = 11500
        assert status.portfolio_value == pytest.approx(11500.0)

    def test_is_tracking_only_excluded_from_portfolio_value(self, tmp_path):
        """追踪仓不计入 portfolio_value 和 holdings_count"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 1000.0},
            holdings=[
                {"symbol": "NDQ.AX", "kind": "etf", "units": 50.0,
                 "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
                 "proxy_kind": "direct"},
                {"symbol": "TSLA", "kind": "equity", "units": 5.0,
                 "unit_label": "股", "avg_cost": 200.0, "cost_currency": "USD",
                 "is_tracking_only": True},
            ]
        )
        status = pm2.get_user_status(current_prices={"NDQ.AX": 60.0, "TSLA": 300.0},
                                      exchange_rate=4.8)
        # 追踪仓 TSLA 应被忽略；只算 CNY cash + NDQ.AX
        expected = 1000.0 + 50.0 * 60.0 * 4.8
        assert status.portfolio_value == pytest.approx(expected)
        assert status.holdings_count == 1  # 只算非追踪仓

    def test_current_prices_none_uses_avg_cost(self, tmp_path):
        """current_prices=None 时退化为用 avg_cost 估值"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 0.0},
            holdings=[{"symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
                        "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
                        "proxy_kind": "direct"}]
        )
        status = pm2.get_user_status(current_prices=None, exchange_rate=5.0)
        # avg_cost 兜底：100 * 50 * 5 = 25000
        assert status.portfolio_value == pytest.approx(25000.0)

    def test_explicit_none_price_excludes_holding(self, tmp_path):
        """current_prices 里 symbol 对应的值是 None → 该 holding 剔除（不用 0 兜底）"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 5000.0},
            holdings=[{"symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
                        "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
                        "proxy_kind": "direct"}]
        )
        # 传 None 表示"拉不到价，剔除" → portfolio_value 只算现金
        status = pm2.get_user_status(current_prices={"NDQ.AX": None}, exchange_rate=5.0)
        assert status.portfolio_value == pytest.approx(5000.0)

    def test_dry_powder_equals_cash_capped_by_max_single(self, tmp_path):
        """disposable_for_invest = min(cash_cny, max_single)"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 30000.0},
            holdings=[],
            max_single_invest_cny=15000.0
        )
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        # available = 30000；min(30000, 15000) = 15000
        assert status.disposable_for_invest == pytest.approx(15000.0)

    def test_dry_powder_capped_by_max_single(self, tmp_path):
        """cash 远大于上限时，disposable 封顶在 max_single_invest_cny"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 100000.0},
            holdings=[],
            max_single_invest_cny=8000.0
        )
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        assert status.disposable_for_invest == pytest.approx(8000.0)

    def test_dry_powder_uncapped_when_no_positive_caps(self, tmp_path):
        """单次上限 0/缺失 = 不设限（2026-06-12 语义）：disposable = cash"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 100000.0},
            holdings=[],
            max_single_invest_cny=0.0,   # 0 = 不设限，不再回落 10000
        )
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        assert status.disposable_for_invest == pytest.approx(100000.0)
        assert status.max_single_invest_cny == 0.0  # 哨兵：不设限

    def test_risk_level_and_target_asset(self, tmp_path):
        """risk_level 和 target_asset 从文档正确读取"""
        s = _make_store(tmp_path)
        _seed_docs(s, risk_tolerance="Aggressive", target_assets=[
            {"symbol": "BTC-USD", "max_single_invest_cny": 5000.0}
        ])
        pm2 = PortfolioManager(s)
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        assert status.risk_level == "Aggressive"
        assert status.target_asset == "BTC-USD"

    def test_holdings_count_excludes_tracking_only(self, tmp_path):
        """holdings_count 只统计 is_tracking_only=False 的持仓"""
        pm2 = self._make_pm_with_holdings(
            tmp_path,
            cash={"CNY": 1000.0},
            holdings=[
                {"symbol": "NDQ.AX", "kind": "etf", "units": 10.0, "cost_currency": "AUD"},
                {"symbol": "GC=F", "kind": "metal", "units": 5.0, "cost_currency": "CNY",
                 "proxy_kind": "gold_cny_per_gram"},
                {"symbol": "TSLA", "kind": "equity", "units": 0.0, "cost_currency": "USD",
                 "is_tracking_only": True},
            ]
        )
        status = pm2.get_user_status(current_prices={}, exchange_rate=4.8)
        assert status.holdings_count == 2  # NDQ.AX + GC=F，TSLA 追踪仓不算


# ============ 任务 1e：with_portfolio_tx 并发场景 ============

def _deposit_worker(root_str: str, amount: float) -> None:
    """子进程里执行一次 deposit，用 with_portfolio_tx 保证 RMW 原子"""
    from pathlib import Path
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.portfolio_manager import PortfolioManager

    store = MemoryStore(Path(root_str))
    # PortfolioManager 构造会读 memory，但我们只需要 with_portfolio_tx
    with store.transaction("portfolio") as p:
        cash = dict(p.get("cash") or {})
        cash["CNY"] = float(cash.get("CNY", 0) or 0) + amount
        p["cash"] = cash
        p["schema_version"] = 2


class TestWithPortfolioTx:
    """with_portfolio_tx() RMW 闭包并发安全性"""

    def test_concurrent_deposit_no_lost_update(self, tmp_path):
        """2 个 worker 各 deposit 100 → cash_cny 最终 +200，不丢更新

        用 multiprocessing（而不是 threading）真正模拟多进程并发，
        与 fcntl 文件锁的跨进程场景一致。
        """
        root = tmp_path / "memory"
        store = MemoryStore(root)
        # 初始化文档
        store.write("portfolio", "state", {
            "cash": {"CNY": 1000.0},
            "holdings": [],
            "schema_version": 2,
        }, "# body")

        # 用两个 worker 各存 100 CNY
        p1 = multiprocessing.Process(target=_deposit_worker, args=(str(root), 100.0))
        p2 = multiprocessing.Process(target=_deposit_worker, args=(str(root), 100.0))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        assert p1.exitcode == 0, "Worker 1 未正常退出"
        assert p2.exitcode == 0, "Worker 2 未正常退出"

        final = MemoryStore(root).read("portfolio")
        final_cny = float(final.get("cash", {}).get("CNY", 0))
        # 初始 1000 + 两次 +100 = 1200，不允许丢 update
        assert final_cny == pytest.approx(1200.0), (
            f"丢失并发更新：期望 1200.0，实际 {final_cny}"
        )

    def test_tx_commit_on_success(self, tmp_path):
        """正常 with 块退出 → 数据持久化"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 5000.0})
        pm2 = PortfolioManager(s)

        with pm2.with_portfolio_tx() as p:
            cash = dict(p.get("cash") or {})
            cash["CNY"] = float(cash.get("CNY", 0)) + 1000.0
            p["cash"] = cash
            p["schema_version"] = 2

        pm2._reload()
        assert pm2.cash_amount("CNY") == pytest.approx(6000.0)

    def test_tx_rollback_on_exception(self, tmp_path):
        """with 块内抛异常 → 数据不落盘（commit-on-success 语义）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 5000.0})
        pm2 = PortfolioManager(s)

        with pytest.raises(RuntimeError):
            with pm2.with_portfolio_tx() as p:
                cash = dict(p.get("cash") or {})
                cash["CNY"] = 99999.0  # 想写但要被 rollback
                p["cash"] = cash
                raise RuntimeError("模拟 tx 中途失败")

        # 重新读：值应该没变
        pm2._reload()
        assert pm2.cash_amount("CNY") == pytest.approx(5000.0)

    def test_v2_v1_fields_cleaned_on_commit(self, tmp_path):
        """with_portfolio_tx 退出时会清除 v1 旧字段（cash_cny 等）"""
        s = _make_store(tmp_path)
        # 故意写入混有 v1 旧字段的 portfolio
        s.write("portfolio", "state", {
            "cash_cny": 9999.0,  # v1 残留
            "aud_cash": 1000.0,  # v1 残留
            "cash": {"CNY": 1000.0},
            "holdings": [],
            "schema_version": 2,
        }, "# body")
        store.write = s.write  # 仅用 s

        # 写 user 和 strategy 才能构造 PM
        s.write("user", "user", {"display_name": "T",
                                   "risk_tolerance": "Balanced"}, "")
        s.write("strategy", "strategy", {
            "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
            "target_assets": [{"symbol": "NDQ.AX", "max_single_invest_cny": 5000.0}]
        }, "")

        pm2 = PortfolioManager(s)
        with pm2.with_portfolio_tx() as p:
            p["schema_version"] = 2  # 不改内容，只是触发 commit

        pm2._reload()
        # v1 旧字段必须被清除
        doc = pm2.portfolio
        assert doc.get("cash_cny") is None
        assert doc.get("aud_cash") is None


# ============ HoldingsView 单元测试 ============

class TestHoldingsView:
    """HoldingsView 的 find / upsert / remove 方法"""

    def _make_view(self, items=None) -> HoldingsView:
        return HoldingsView(list(items or []))

    def test_find_existing(self):
        view = self._make_view([{"symbol": "A"}, {"symbol": "B"}])
        assert view.find("A")["symbol"] == "A"

    def test_find_missing_returns_none(self):
        view = self._make_view([{"symbol": "A"}])
        assert view.find("Z") is None

    def test_upsert_inserts_new(self):
        view = self._make_view()
        view.upsert("NEW", units=10.0, cost_currency="CNY")
        assert len(view) == 1
        assert view.find("NEW")["units"] == 10.0

    def test_upsert_updates_existing(self):
        view = self._make_view([{"symbol": "A", "units": 5.0}])
        view.upsert("A", units=15.0)
        assert view.find("A")["units"] == 15.0
        assert len(view) == 1  # 没有新增

    def test_remove_existing(self):
        view = self._make_view([{"symbol": "A"}, {"symbol": "B"}])
        removed = view.remove("A")
        assert removed is True
        assert len(view) == 1
        assert view.find("A") is None

    def test_remove_missing_returns_false(self):
        view = self._make_view([{"symbol": "A"}])
        removed = view.remove("NOTEXIST")
        assert removed is False
        assert len(view) == 1

    def test_all_returns_copy(self):
        """all() 返回副本，修改副本不影响原 view"""
        view = self._make_view([{"symbol": "A"}])
        snap = view.all()
        snap.append({"symbol": "X"})
        assert len(view) == 1  # 原 view 不受影响


# ============ PortfolioManager 构造失败场景 ============

class TestPortfolioManagerInitErrors:
    """缺文件时给出明确错误"""

    def test_missing_all_docs_raises(self, tmp_path):
        """未初始化 memory 时 PortfolioManager 构造应 FileNotFoundError"""
        empty_store = MemoryStore(tmp_path / "empty_memory")
        with pytest.raises(FileNotFoundError):
            PortfolioManager(empty_store)

    def test_missing_portfolio_raises(self, tmp_path):
        """只缺 portfolio.md → FileNotFoundError"""
        s = _make_store(tmp_path)
        s.write("user", "user", {"display_name": "T",
                                   "risk_tolerance": "Balanced"}, "")
        s.write("strategy", "strategy", {
            "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
            "target_assets": [{"symbol": "NDQ.AX", "max_single_invest_cny": 5000.0}]
        }, "")
        with pytest.raises(FileNotFoundError):
            PortfolioManager(s)


# ============ 任务 2：buy() 资金来源（external_funding 不扣现金）============

class TestBuyFundingSource:
    """buy() 的资金来源语义（子弹池模型基础）

    - source_type="cash_deduct"（默认）：买入扣减对应币种现金 = 现有行为，不变
    - source_type="external_funding"：外部新钱建仓，portfolio cash 不动
      （日定投走京东/工资银行卡，钱不来自子弹池现金 → 不该扣 cash，
       否则 ¥30k 子弹池会被日定投错误消耗，10 个月归零）
    """

    def test_default_buy_deducts_cash(self, tmp_path):
        """默认（不传 source_type）：买入扣现金，现有行为保持不变（回归守卫）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 10000.0}, holdings=[])
        pm2 = PortfolioManager(s)
        pm2.buy("510300.SS", units=100.0, price=5.0, currency="CNY", kind="etf")
        pm2._reload()
        assert pm2.cash_amount("CNY") == pytest.approx(9500.0)  # 10000 - 100*5
        assert pm2.find_holding("510300.SS")["units"] == 100.0

    def test_external_funding_does_not_deduct_cash(self, tmp_path):
        """source_type=external_funding：现金不动，持仓照常建立（子弹池语义）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 10000.0}, holdings=[])
        pm2 = PortfolioManager(s)
        pm2.buy("510300.SS", units=100.0, price=5.0, currency="CNY", kind="etf",
                source_type="external_funding")
        pm2._reload()
        assert pm2.cash_amount("CNY") == pytest.approx(10000.0)  # 现金不动
        h = pm2.find_holding("510300.SS")
        assert h is not None and h["units"] == 100.0 and h["avg_cost"] == 5.0

    def test_external_funding_weighted_avg_on_existing(self, tmp_path):
        """external_funding 对已有持仓仍走加权均价，只是不扣现金"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 10000.0}, holdings=[
            {"symbol": "510300.SS", "kind": "etf", "units": 100.0, "avg_cost": 5.0,
             "unit_label": "股", "cost_currency": "CNY", "proxy_kind": "direct"}
        ])
        pm2 = PortfolioManager(s)
        pm2.buy("510300.SS", units=100.0, price=6.0, currency="CNY", kind="etf",
                source_type="external_funding")
        pm2._reload()
        h = pm2.find_holding("510300.SS")
        assert h["units"] == 200.0
        assert h["avg_cost"] == pytest.approx(5.5)  # (100*5+100*6)/200
        assert pm2.cash_amount("CNY") == pytest.approx(10000.0)

    def test_external_funding_recorded_in_history(self, tmp_path):
        """external_funding 在 history 里留 funding_source 审计字段（账本可追溯）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 10000.0}, holdings=[])
        pm2 = PortfolioManager(s)
        pm2.buy("510300.SS", units=100.0, price=5.0, currency="CNY", kind="etf",
                source_type="external_funding")
        last = s.read_history()[-1]
        assert last["action"] == "buy"
        assert last["funding_source"] == "external_funding"

    def test_invalid_source_type_rejected(self, tmp_path):
        """非法 source_type 抛 ValueError（防手滑传错字符串静默扣/不扣）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 10000.0}, holdings=[])
        pm2 = PortfolioManager(s)
        with pytest.raises(ValueError):
            pm2.buy("510300.SS", units=100.0, price=5.0, currency="CNY",
                    kind="etf", source_type="nonsense")

    def test_cash_deduct_insufficient_rejected(self, tmp_path):
        """#108 防穿透：cash_deduct 现金不足抛 ValueError，且不落盘（holdings/cash 都不变）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 400.0}, holdings=[])
        pm2 = PortfolioManager(s)
        with pytest.raises(ValueError):
            pm2.buy("NDQ.AX", units=10.0, price=130.0, currency="CNY", kind="etf")
        pm2._reload()
        # with_portfolio_tx commit-on-success：抛异常 → 整笔不落盘
        assert pm2.cash_amount("CNY") == pytest.approx(400.0)
        assert pm2.find_holding("NDQ.AX") is None

    def test_external_funding_bypasses_cash_check(self, tmp_path):
        """external_funding 不扣 cash → 即便现金为 0 也能建仓（防穿透只管 cash_deduct）"""
        s = _make_store(tmp_path)
        _seed_docs(s, cash={"CNY": 0.0}, holdings=[])
        pm2 = PortfolioManager(s)
        pm2.buy("NDQ.AX", units=10.0, price=130.0, currency="CNY", kind="etf",
                source_type="external_funding")
        pm2._reload()
        assert pm2.find_holding("NDQ.AX")["units"] == 10.0
