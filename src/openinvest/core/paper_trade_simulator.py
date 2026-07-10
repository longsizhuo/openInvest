"""paper_trade_simulator.py — 模拟"每次都听 AI"的虚拟账户

核心思路（用户的 insight）：
单 verdict 命中率噪音大，50 个 verdict 就是 50 个独立噪音点。但如果**模拟用户
每次都按 verdict 执行**，跑完一整段历史，得到的是 **1 条完整 P&L 曲线**——
这才是用户真实关心的"我听 AI 一年下来赚了还是亏了"。

策略 vs verdict 评估的根本差异：
- verdict-level：每个 verdict 独立打分 → N 个独立样本（噪音 1:10）
- strategy-level：连续决策 + 累积复利 + mark-to-market → 1 条曲线（信号清晰）

设计：
- 起始 ¥100,000 CNY（标准 baseline，所有实验可比）
- 多币种 cash dict（CNY/AUD/USD/...）
- 多资产 holdings dict（symbol → units）
- 每个 decision_date 执行 verdict 时按当日实际市价 + 实际汇率成交
- 每天 mark-to-market 得到 portfolio_value_cny 时序

不做：
- 杠杆 / 期权 / 卖空
- 实际手续费（只算黄金 sell_fee_pct，股票忽略）
- 滑点（按当日 close 价成交，无 bid/ask spread）
- 这些简化让 backtest 是"理想化上限"，实盘会因摩擦成本略差
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openinvest.utils.exchange_fee import get_history_data
from openinvest.utils.fx import get_fx_rate

log = logging.getLogger(__name__)


# ============ 数据类 ============

@dataclass
class Transaction:
    """单笔模拟成交"""
    decision_date: str          # ISO date
    asset: str                  # symbol（如 NDQ.AX）
    action: str                 # BUY / SELL / SKIP
    units: float                # 成交数量
    price: float                # 成交单价（asset cost_currency）
    cost_currency: str          # asset 计价币种
    cny_value: float            # 成交总额折 CNY（审计用）
    fx_rate: float              # 当时 cost_currency→CNY 汇率
    note: str = ""              # SKIP 时填原因


@dataclass
class PaperAccount:
    """虚拟账户状态"""
    cash: Dict[str, float] = field(default_factory=dict)        # {ccy: amount}
    holdings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # holdings: {symbol: {"units": float, "avg_cost": float, "ccy": str}}
    transactions: List[Transaction] = field(default_factory=list)
    daily_values: List[Tuple[str, float]] = field(default_factory=list)  # (date, cny_value)


# ============ Asset metadata（手写映射，避免每次拉网络）============

ASSET_CURRENCY: Dict[str, str] = {
    "NDQ.AX": "AUD",
    "AAPL": "USD",
    "TSLA": "USD",
    "BTC-USD": "USD",
    "ETH-USD": "USD",
    "510300.SS": "CNY",
    "510500.SS": "CNY",
    "510050.SS": "CNY",
    "159949.SZ": "CNY",
    "0700.HK": "HKD",
    # 黄金：cost_currency 已经是 CNY/克（GC=F 是 USD/oz 但 strategy 里换算成 CNY/g）
    "GC=F": "CNY",
}


def get_asset_currency(symbol: str) -> str:
    """asset → 计价币种。未知 symbol 默认 USD（美股最常见）"""
    return ASSET_CURRENCY.get(symbol.upper(), "USD")


# ============ Simulator ============

class PaperTradeSimulator:
    """walk-forward 虚拟账户模拟器

    用法：
        sim = PaperTradeSimulator(start_date="2024-01-02")
        for d in decision_dates:
            for asset in assets:
                verdict = run_committee_at(d, asset)  # 跑 backtest 委员会
                sim.execute_verdict(d, asset, verdict)

        # 沿时间线每天 mark-to-market
        for date in trading_dates(start, end):
            sim.account.daily_values.append((date, sim.mark_to_market(date)))

        metrics = evaluate_strategy(sim.account)
    """

    def __init__(
        self,
        start_date: str,
        initial_cash_cny: float = 100_000.0,
    ):
        self.start_date = start_date
        self.account = PaperAccount(
            cash={"CNY": initial_cash_cny, "AUD": 0.0, "USD": 0.0, "HKD": 0.0},
            holdings={},
            transactions=[],
            daily_values=[(start_date, initial_cash_cny)],
        )

    # ---------- 执行 verdict ----------

    def execute_verdict(
        self, decision_date: str, asset: str, verdict: Dict[str, Any],
    ) -> Transaction:
        """根据 verdict 模拟交易，返回 Transaction（含 SKIP 原因）

        verdict 结构（来自 core/committee.py:parse_cio_memo）：
            {"verdict": "BUY"|"ACCUMULATE"|"HOLD"|"TRIM"|"SELL",
             "confidence": float, "alloc_cny": int, "dominant_view": str}

        BUY/ACCUMULATE：用 alloc_cny 等值的 cost_currency 现金买入
        SELL/TRIM：卖出 alloc_cny 等值的持仓（如果不够则卖光）
        HOLD：不动
        """
        direction = str(verdict.get("verdict", "HOLD")).upper()
        # TRIM/SELL 的 alloc_cny 是负数(表示减仓方向),BUY/ACCUMULATE 是正数。统一取量级
        # qty_cny,方向由 direction 决定。⚠ 旧代码 `alloc_cny <= 0` 把负的减仓 alloc 全吞成
        # HOLD → SELL/TRIM 从不成交(回测里委员会"减仓"全是 no-op,曾被误判成"会 de-risk")。
        qty_cny = abs(float(verdict.get("alloc_cny", 0) or 0))

        if direction == "HOLD" or qty_cny == 0:
            tx = Transaction(
                decision_date=decision_date, asset=asset, action="HOLD",
                units=0, price=0, cost_currency="", cny_value=0, fx_rate=0,
                note="HOLD or alloc=0",
            )
            self.account.transactions.append(tx)
            return tx

        ccy = get_asset_currency(asset)
        price = self._get_price(asset, decision_date)
        fx = get_fx_rate(ccy, "CNY", as_of_date=decision_date) or self._fallback_fx(ccy)

        if price is None or price <= 0:
            tx = Transaction(
                decision_date=decision_date, asset=asset, action="SKIP",
                units=0, price=0, cost_currency=ccy, cny_value=0, fx_rate=fx,
                note=f"price unavailable @ {decision_date}",
            )
            self.account.transactions.append(tx)
            return tx

        if direction in ("BUY", "ACCUMULATE"):
            return self._execute_buy(decision_date, asset, qty_cny, price, ccy, fx)
        elif direction in ("SELL", "TRIM"):
            return self._execute_sell(decision_date, asset, qty_cny, price, ccy, fx)
        else:
            # 未知 verdict，当 HOLD
            tx = Transaction(
                decision_date=decision_date, asset=asset, action="SKIP",
                units=0, price=0, cost_currency=ccy, cny_value=0, fx_rate=fx,
                note=f"unknown verdict: {direction}",
            )
            self.account.transactions.append(tx)
            return tx

    def _execute_buy(self, date, asset, alloc_cny, price, ccy, fx):
        """alloc_cny 等值的 ccy 现金 → 持仓"""
        # 需要的 ccy 金额
        need_ccy = alloc_cny / fx
        available_ccy = self.account.cash.get(ccy, 0)

        # 现金不够：先看 CNY 能不能换汇
        if available_ccy < need_ccy and ccy != "CNY":
            shortfall_ccy = need_ccy - available_ccy
            shortfall_cny = shortfall_ccy * fx
            cny_cash = self.account.cash.get("CNY", 0)
            if cny_cash >= shortfall_cny:
                # 模拟换汇（无手续费简化）
                self.account.cash["CNY"] = cny_cash - shortfall_cny
                self.account.cash[ccy] = available_ccy + shortfall_ccy
                available_ccy = need_ccy

        if available_ccy < need_ccy:
            # 还是不够，按可用现金买
            actual_ccy = available_ccy
            if actual_ccy <= 0:
                tx = Transaction(
                    decision_date=date, asset=asset, action="SKIP",
                    units=0, price=price, cost_currency=ccy, cny_value=0, fx_rate=fx,
                    note=f"现金不足（need {need_ccy:.2f} {ccy}, have {available_ccy:.2f}）",
                )
                self.account.transactions.append(tx)
                return tx
        else:
            actual_ccy = need_ccy

        # 成交
        units = actual_ccy / price
        self.account.cash[ccy] = self.account.cash.get(ccy, 0) - actual_ccy

        # 更新持仓（加权均价）
        h = self.account.holdings.get(asset, {"units": 0, "avg_cost": 0, "ccy": ccy})
        old_units = h["units"]
        old_avg = h["avg_cost"]
        new_units = old_units + units
        new_avg = ((old_avg * old_units) + (price * units)) / new_units if new_units > 0 else price
        self.account.holdings[asset] = {"units": new_units, "avg_cost": new_avg, "ccy": ccy}

        tx = Transaction(
            decision_date=date, asset=asset, action="BUY",
            units=units, price=price, cost_currency=ccy,
            cny_value=actual_ccy * fx, fx_rate=fx,
        )
        self.account.transactions.append(tx)
        return tx

    def _execute_sell(self, date, asset, alloc_cny, price, ccy, fx):
        """卖出 alloc_cny 等值的持仓"""
        h = self.account.holdings.get(asset)
        if not h or h["units"] <= 0:
            tx = Transaction(
                decision_date=date, asset=asset, action="SKIP",
                units=0, price=price, cost_currency=ccy, cny_value=0, fx_rate=fx,
                note="无持仓可卖",
            )
            self.account.transactions.append(tx)
            return tx

        # alloc_cny 换算成卖出的 units
        want_units = (alloc_cny / fx) / price
        actual_units = min(want_units, h["units"])

        gross_ccy = actual_units * price
        # 黄金的 sell_fee_pct = 0.38%；其他暂忽略
        fee_ccy = gross_ccy * 0.0038 if asset.upper() == "GC=F" else 0
        net_ccy = gross_ccy - fee_ccy

        # 更新现金 + 持仓
        self.account.cash[ccy] = self.account.cash.get(ccy, 0) + net_ccy
        new_units = h["units"] - actual_units
        if new_units <= 1e-8:
            del self.account.holdings[asset]
        else:
            h["units"] = new_units  # avg_cost 不变（卖出不影响成本基础）

        tx = Transaction(
            decision_date=date, asset=asset, action="SELL",
            units=actual_units, price=price, cost_currency=ccy,
            cny_value=net_ccy * fx, fx_rate=fx,
        )
        self.account.transactions.append(tx)
        return tx

    # ---------- mark-to-market ----------

    def mark_to_market(self, date: str) -> float:
        """计算 date 当日账户总市值（CNY）"""
        # cash 部分
        total_cny = 0.0
        for ccy, amt in self.account.cash.items():
            if amt == 0:
                continue
            fx = get_fx_rate(ccy, "CNY", as_of_date=date) or self._fallback_fx(ccy)
            total_cny += amt * fx

        # holdings 部分
        for sym, h in self.account.holdings.items():
            if h["units"] <= 0:
                continue
            price = self._get_price(sym, date)
            if price is None:
                # 拉不到价：用 avg_cost 兜底
                price = h["avg_cost"]
            ccy = h["ccy"]
            fx = get_fx_rate(ccy, "CNY", as_of_date=date) or self._fallback_fx(ccy)
            total_cny += h["units"] * price * fx

        return round(total_cny, 2)

    # ---------- helpers ----------

    def _get_price(self, symbol: str, date: str) -> Optional[float]:
        """date 当日 close（用 as_of_date 防穿越）。

        黄金代理特例：GC=F 原生报价是 USD/oz，但本模拟器把它的计价币种标为
        CNY（用户持的是积存金 CNY/克，见 ASSET_CURRENCY）——必须补 USDCNY 腿，
        否则买/卖/估值全程 fx=1，窗口收益漏掉人民币汇率漂移，系统性偏置黄金腿
        （与 jobs/verdict_review._window_return 的 GC=F×USDCNY 口径对齐，
        issue #179 P1-A①）。oz→克的常数因子在收益比值里消掉，无需换算。
        """
        df = get_history_data(symbol, "5d", as_of_date=date)
        if df is None or df.empty:
            return None
        try:
            price = float(df["Close"].iloc[-1])
        except Exception:  # noqa: BLE001
            return None
        if symbol.upper() == "GC=F":
            fx = get_fx_rate("USD", "CNY", as_of_date=date)
            if fx is None:
                fx = self._fallback_fx("USD")
                # 固定 7.1 兜底会重新引入本修复要消除的汇率误差——留痕供事后复盘
                log.warning(
                    "GC=F USDCNY 腿 %s 拉不到汇率，回落固定 %.1f（2024 均值）", date, fx
                )
            price *= fx
        return price

    @staticmethod
    def _fallback_fx(ccy: str) -> float:
        """汇率拉不到的兜底（按 2024 中间均值）"""
        return {"CNY": 1.0, "USD": 7.1, "AUD": 4.7, "HKD": 0.91}.get(ccy.upper(), 1.0)

    # ---------- 序列化 ----------

    def serialize(self) -> Dict[str, Any]:
        """JSON-friendly 输出，给 strategy_metrics + render 用"""
        return {
            "start_date": self.start_date,
            "final_cash": dict(self.account.cash),
            "final_holdings": {
                sym: {"units": h["units"], "avg_cost": h["avg_cost"], "ccy": h["ccy"]}
                for sym, h in self.account.holdings.items()
            },
            "transactions": [
                {
                    "decision_date": t.decision_date, "asset": t.asset, "action": t.action,
                    "units": t.units, "price": t.price, "cost_currency": t.cost_currency,
                    "cny_value": t.cny_value, "fx_rate": t.fx_rate, "note": t.note,
                }
                for t in self.account.transactions
            ],
            "daily_values": list(self.account.daily_values),
        }
