"""win_rate_per_trade 前视回归（CR 命中：卖出后的 BUY 被当成本基）。"""
from types import SimpleNamespace

from openinvest.calc.strategy_metrics import win_rate_per_trade


def _t(action, asset, price):
    return SimpleNamespace(action=action, asset=asset, price=price)


def test_sell_matches_only_prior_buy():
    """BUY@100 → SELL@110（真赢）→ BUY@120：旧实现拿 120 当成本判亏。"""
    txs = [_t("BUY", "X", 100.0), _t("SELL", "X", 110.0), _t("BUY", "X", 120.0)]
    assert win_rate_per_trade(txs) == 100.0


def test_sell_without_prior_buy_not_win():
    txs = [_t("SELL", "X", 110.0), _t("BUY", "X", 100.0)]
    assert win_rate_per_trade(txs) == 0.0
