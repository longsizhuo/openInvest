"""cmd_prepare_committee 契约守门 — coordinator 路径的确定性事实块产出。

2026-06-11 漂移修复：coordinator 路径此前不产 sentiment_brief / valuation_brief /
reentry_reference —— CIO 看不到 VALUATION/MARKET SENTIMENT（INDEP_DEFENSE_FLAG
不进 transcript → save_committee 防御降级永不触发）、EXPECTED_PATH 凭空编。
本测试守：prepare 输出三个新 key + instructions 指导粘贴位置。
"""
from __future__ import annotations

import argparse
import json

import pandas as pd
import pytest


class _FakeHoldings:
    def all(self):
        return []

    def __iter__(self):
        return iter([])

    def find(self, symbol):
        return None


class _FakePM:
    def __init__(self):
        self.holdings = _FakeHoldings()
        self.strategy = {"target_assets": [
            {"symbol": "TEST.AX", "display_name": "Test ETF", "type": "etf"},
        ]}
        self.user = {}


SENT = "SENTIMENT_PREP_SENTINEL\nINDEP_DEFENSE_FLAG: off"
VAL = "VALUATION_PREP_SENTINEL"
REENTRY = "REENTRY_PREP_SENTINEL"


@pytest.fixture()
def _mock_world(monkeypatch):
    import openinvest.core.portfolio_manager as pm_mod
    monkeypatch.setattr(pm_mod, "PortfolioManager", _FakePM)

    import openinvest.utils.exchange_fee as ef
    fake_df = pd.DataFrame(
        {"Close": [100.0 + i for i in range(200)]},
        index=pd.date_range("2024-01-01", periods=200),
    )
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **k: fake_df)
    monkeypatch.setattr(ef, "analyze_multi_timeframe", lambda *a, **k: "MOCK_MARKET")
    monkeypatch.setattr(ef, "get_macro_data", lambda: "MOCK_MACRO")

    import openinvest.utils.gold_price as gp
    monkeypatch.setattr(gp, "get_gold_snapshot", lambda **k: None)

    # 确定性事实块 loaders（与 direct 路径同源）锚定 SENTINEL
    import openinvest.core.runner.coordinator as cr
    monkeypatch.setattr(cr, "load_sentiment_brief", lambda *a, **k: SENT)
    monkeypatch.setattr(cr, "load_valuation_brief", lambda *a, **k: VAL)
    monkeypatch.setattr(cr, "load_prior_insights", lambda *a, **k: "")

    import openinvest.core.regime_probability as rp
    monkeypatch.setattr(rp, "get_regime_forward_summary", lambda *a, **k: None)
    # coordinator 改用 build_reentry_reference（取回结构化 profile，与 session 路径对齐）
    monkeypatch.setattr(rp, "build_reentry_reference", lambda *a, **k: (REENTRY, None))

    import openinvest.jobs.daily_report_builder as drb
    monkeypatch.setattr(drb, "portfolio_summary_text", lambda *a, **k: "MOCK_PORTFOLIO")
    import openinvest.utils.fx as fx
    monkeypatch.setattr(fx, "total_portfolio_value_cny", lambda *a, **k: (0.0, {}))


def test_prepare_committee_emits_deterministic_blocks(_mock_world, capfd):
    """prepare 输出必须含 sentiment/valuation/reentry 三个 key，且 instructions
    指导 coordinator 把它们粘进 Quant/CIO prompt（缺了防御链在该路径失效）。

    用 capfd 而非 capsys：_print_json 直写 sys.__stdout__（fd 级）绕过 capsys。
    """
    from openinvest.cli import cmd_prepare_committee

    cmd_prepare_committee(argparse.Namespace(symbol="TEST.AX"))
    raw = capfd.readouterr().out
    # stdout 可能有 JSON 之前的杂项打印（行情刷新提示等）→ 从第一个 { 起解析
    out = json.loads(raw[raw.index("{"):])

    assert out["sentiment_brief"] == SENT, "sentiment_brief 没产出"
    assert out["valuation_brief"] == VAL, "valuation_brief 没产出"
    assert out["reentry_reference"] == REENTRY, "reentry_reference 没产出"

    ins = out["instructions"]
    assert "sentiment_brief" in ins and "valuation_brief" in ins, (
        "instructions 没指导粘贴确定性事实块"
    )
    assert "reentry_reference" in ins
    assert "INDEP_DEFENSE_FLAG" in ins, (
        "instructions 必须强调防御哨兵行要原样进 transcript（save_committee 后处理依赖）"
    )
    # regime_brief 仍在（老契约不回归）
    assert out["regime_brief"]
    assert "MARKET SENTIMENT" in ins and "VALUATION" in ins


class _FundHoldings(_FakeHoldings):
    ITEMS = [
        {"symbol": "TEST.AX", "kind": "etf", "units": 10, "avg_cost": 90.0, "cost_currency": "AUD"},
        {"symbol": "FUND:123456", "kind": "fund", "units": 100, "avg_cost": 1.5,
         "cost_currency": "CNY", "proxy_kind": "eastmoney_fund"},
    ]

    def all(self):
        return list(self.ITEMS)

    def __iter__(self):
        return iter(self.ITEMS)


def test_prepare_committee_values_fund_holding_with_eastmoney_nav(_mock_world, monkeypatch):
    """场外基金 yfinance 没有：coordinator 估值必须走 quote 层的东方财富净值（与 status 同口径），
    不能拿 FUND:xxxxxx 去问 yfinance。"""
    import openinvest.core.portfolio_manager as pm_mod
    import openinvest.utils.fx as fx
    import openinvest.utils.quotes as quotes
    from openinvest.core.runner.coordinator import prepare_committee_brief
    from openinvest.utils.eastmoney_fund import FundNavSnapshot

    class _FundPM(_FakePM):
        def __init__(self):
            super().__init__()
            self.holdings = _FundHoldings()

    monkeypatch.setattr(pm_mod, "PortfolioManager", _FundPM)
    monkeypatch.setattr(quotes, "fetch_fund_nav", lambda symbol: FundNavSnapshot(
        code="123456", nav=2.0, nav_date="2026-01-05", is_stale=False,
    ))
    seen = {}

    def fake_total(pm, current_prices, base="CNY", **kw):
        seen.update(current_prices)
        return 0.0, {}

    monkeypatch.setattr(fx, "total_portfolio_value_cny", fake_total)
    prepare_committee_brief("TEST.AX")
    assert seen["FUND:123456"] == 2.0
    assert seen["TEST.AX"] == 299.0   # 非基金仍走 yfinance close（fake_df 末值）
