"""黄金（GC=F）只能按 人民币/克 记账（#443）。

全仓估值把 GC=F 当 CNY/克（GC=F USD/oz ÷ 31.1035 × USDCNY）：
- 按 USD/oz 记（`buy GC=F -c USD`）→ 成本 4100 对现价 ~900 → 假浮亏 −78%，并流进委员会 CIO 输入；
- 按 CNY/克记却没带 proxy_kind（旧 buy 一律写 direct）→ 现价被当 USD/oz 拉 → 假浮盈 +375%。
入口 buy（record_existing_position / dca 也经它）拒收前者、给后者落 proxy_kind=gold_cny_per_gram。

全部不联网：get_quote / get_gold_snapshot / get_fx_rate / _safe_close patch 到源模块。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.utils.quotes import QuoteSnapshot

SPOT_PER_GRAM = 900.0     # 假金价 CNY/克
SPOT_PER_OZ = 4100.0      # 同一时刻 GC=F 原生报价 USD/oz


def _pm(cash=None, holdings=None) -> PortfolioManager:
    s = MemoryStore()
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "# user")
    s.write("strategy", "strategy", {"target_assets": []}, "# strategy")
    s.write("portfolio", "state", {"schema_version": 2, "cash": cash or {"CNY": 100000.0, "USD": 10000.0},
                                   "holdings": holdings or []}, "# portfolio")
    return PortfolioManager(s)


def _unchanged(pm: PortfolioManager) -> None:
    """被拒的买入不留任何痕迹：没建仓、没扣现金、没写 history。"""
    assert pm.holdings.find("GC=F") is None
    assert pm.cash_amount("CNY") == 100000.0 and pm.cash_amount("USD") == 10000.0
    assert pm.store.read_history() == []


def test_usd_oz_booking_rejected_with_correct_example():
    pm = _pm()
    with pytest.raises(ValueError) as e:
        pm.buy("GC=F", 1, 4100, currency="USD", kind="metal")
    msg = str(e.value)
    assert "USD" in msg and "-c CNY" in msg and "克" in msg and "31.1035" in msg   # 报错里带正确写法
    _unchanged(pm)


@pytest.mark.parametrize("label", ["oz", "盎司", "ounce"])
def test_non_gram_unit_rejected(label):
    pm = _pm()
    with pytest.raises(ValueError, match="单位"):
        pm.buy("GC=F", 1, 880, currency="CNY", kind="metal", unit_label=label)
    _unchanged(pm)


def test_cny_gram_booking_stamps_gold_proxy_even_with_default_kind_and_unit():
    """合法记法落 kind=metal / 克 / proxy_kind=gold_cny_per_gram；调用方没传 kind/unit（默认 equity/股）也纠正。"""
    pm = _pm()
    out = pm.buy("GC=F", 5, 880)       # 币种不传 = 新建仓 CNY；kind / unit_label 走默认
    assert out["status"] == "ok" and out["currency"] == "CNY"
    h = pm.holdings.find("GC=F")
    assert (h["kind"], h["unit_label"], h["cost_currency"], h["proxy_kind"], h["yfinance_proxy"]) == \
        ("metal", "克", "CNY", "gold_cny_per_gram", "GC=F")
    assert pm.cash_amount("CNY") == pytest.approx(100000.0 - 5 * 880)


def test_record_existing_position_same_guard():
    from openinvest.connectors import mcp_server as m
    _pm()
    bad = m.record_existing_position(symbol="GC=F", units=1, price=4100, currency="USD", kind="metal")
    assert bad["status"] == "error" and "CNY" in bad["error"]
    ok = m.record_existing_position(symbol="GC=F", units=5, price=880, currency="CNY",
                                    kind="metal", unit_label="克")
    assert ok["status"] == "ok"
    h = PortfolioManager().holdings.find("GC=F")
    assert h["proxy_kind"] == "gold_cny_per_gram"
    assert PortfolioManager().cash_amount("CNY") == 100000.0       # 补录不动现金


def test_legacy_usd_row_refuses_add_and_says_how_to_fix():
    """旧版本遗留的 USD/oz 黄金行：加仓既不能按 USD（口径错）也不能按 CNY（混币种平均成本）→ 报错指向删行重记。"""
    legacy = {"symbol": "GC=F", "kind": "metal", "units": 1.0, "avg_cost": 4100.0, "unit_label": "oz",
              "cost_currency": "USD", "proxy_kind": "direct"}
    for ccy in ("USD", "CNY"):
        pm = _pm(holdings=[dict(legacy)])
        with pytest.raises(ValueError, match="delete_holding"):
            pm.buy("GC=F", 1, 880, currency=ccy)
        assert pm.holdings.find("GC=F")["units"] == 1.0


def test_legacy_cny_row_stamped_direct_is_healed_on_add():
    """旧 buy 把 CNY/克黄金落成 proxy_kind=direct（status 会拿 USD/oz 现价比克价成本）→ 再加仓时修正。"""
    pm = _pm(holdings=[{"symbol": "GC=F", "kind": "metal", "units": 5.0, "avg_cost": 880.0,
                        "unit_label": "克", "cost_currency": "CNY", "proxy_kind": "direct"}])
    pm.buy("GC=F", 5, 900)
    h = pm.holdings.find("GC=F")
    assert h["proxy_kind"] == "gold_cny_per_gram" and h["units"] == 10.0 and h["avg_cost"] == 890.0


def test_non_gold_symbols_untouched():
    """护栏只管 GC=F：别的标的（含 USD 计价）行为不变。"""
    pm = _pm()
    pm.buy("AAPL", 2, 150.0, currency="USD", kind="equity")
    h = pm.holdings.find("AAPL")
    assert (h["cost_currency"], h["proxy_kind"], h["kind"]) == ("USD", "direct", "equity")


# ---------- 记完之后 status / what_if 同口径（不再 −78% / +375%）----------

def _fake_quote(h):
    """克价口径 → CNY/克；direct → GC=F 原生 USD/oz（真实 get_quote 的行为）。"""
    gram = h.get("proxy_kind") == "gold_cny_per_gram"
    return QuoteSnapshot(symbol=h["symbol"], price=SPOT_PER_GRAM if gram else SPOT_PER_OZ,
                         currency="CNY" if gram else "USD", unit="克" if gram else "oz",
                         last_updated="2026-10-09",
                         extra={"gold_usd_per_oz": SPOT_PER_OZ, "usdcny_rate": 7.2} if gram else {})


def test_status_and_what_if_value_booked_gold_per_gram(monkeypatch):
    from openinvest.services import skill_views
    pm = _pm()
    pm.buy("GC=F", 5, 880, currency="CNY", kind="metal", unit_label="克")

    monkeypatch.setattr("openinvest.utils.quotes.get_quote", _fake_quote)
    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm=None, **kw: {"status": "insufficient_history"})
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate", lambda a, b, **kw: 1.0 if a == b else 7.2)
    monkeypatch.setattr("openinvest.utils.gold_price.get_gold_snapshot",
                        MagicMock(return_value=MagicMock(spot_cny_per_gram=SPOT_PER_GRAM)))

    status = skill_views.build_status_view()
    h = next(x for x in status["all_holdings"] if x["symbol"] == "GC=F")
    assert h["current_price"] == SPOT_PER_GRAM
    assert h["pnl_pct"] == pytest.approx((SPOT_PER_GRAM / 880 - 1) * 100, abs=0.01)       # +2.27%，不是 +366% / −78%
    assert status["gold"]["now_cny_per_gram"] == SPOT_PER_GRAM and status["gold"]["grams"] == 5.0

    wi = skill_views.build_what_if_view(symbol="GC=F", pct=-10)
    b = wi["breakdown"]["GC=F"]
    assert b["cur_price"] == SPOT_PER_GRAM and b["cur_value_cny"] == 5 * SPOT_PER_GRAM
    assert b["delta_cny"] == pytest.approx(-0.1 * 5 * SPOT_PER_GRAM, abs=0.01)
