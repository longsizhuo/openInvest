"""交易摩擦成本纯计算（calc 层，ADR-026）

从 utils/exchange_fee.py 拆出的纯计算核：换汇/券商摩擦成本模型与报告文本。
行情拉取（get_history_data / get_cost_snapshot 的 spot_rate 兜底）留在
utils/exchange_fee.py（IO shell）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ==========================================
# 0. 数据结构定义
# ==========================================
@dataclass
class ForexFriction:
    input_cny: float
    net_aud: float
    spot_rate: float
    effective_rate: float
    friction_pct: float
    total_fee_cny: float
    break_even_pct: float
    is_viable: bool


@dataclass
class StockFriction:
    input_aud: float
    fee_aud: float
    friction_pct: float


@dataclass
class CostSnapshot:
    invest_cny: float
    spot_rate: float
    forex: ForexFriction
    trade_aud: float
    stock: StockFriction
    combined_fee_cny: Optional[float]
    combined_friction_pct: Optional[float]


class TransactionCostCalculator:
    def __init__(self):
        self.cn_cable_fee = 150.0
        self.cn_commission_rate = 0.001
        self.cn_commission_min = 50.0
        self.cn_commission_max = 260.0
        self.au_inward_fee = 15.0
        self.commsec_tier_1 = 5.0
        self.commsec_tier_2 = 10.0
        self.commsec_tier_3 = 19.95
        self.commsec_rate_high = 0.0012

    def calculate_forex_friction(self, invest_cny: float, spot_rate: float) -> ForexFriction:
        if invest_cny <= 0 or spot_rate <= 0:
            return ForexFriction(0, 0, 0, 0, 0, 0, 0, False)

        commission = max(self.cn_commission_min, min(invest_cny * self.cn_commission_rate, self.cn_commission_max))
        cn_total_fee = self.cn_cable_fee + commission
        remaining_cny = invest_cny - cn_total_fee

        if remaining_cny <= 0:
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, cn_total_fee, float('inf'), False)

        gross_aud = remaining_cny / spot_rate
        net_aud = gross_aud - self.au_inward_fee

        if net_aud <= 0:
            total_fee_cny_equiv = cn_total_fee + (gross_aud * spot_rate)
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, total_fee_cny_equiv, float('inf'), False)

        effective_rate = invest_cny / net_aud
        value_loss_cny = invest_cny - (net_aud * spot_rate)
        friction_pct = (value_loss_cny / invest_cny) * 100
        break_even_pct = (1 / (1 - friction_pct / 100) - 1) * 100 if friction_pct < 100 else float('inf')

        return ForexFriction(
            input_cny=invest_cny,
            net_aud=net_aud,
            spot_rate=spot_rate,
            effective_rate=effective_rate,
            friction_pct=friction_pct,
            total_fee_cny=value_loss_cny,
            break_even_pct=break_even_pct,
            is_viable=True
        )

    def calculate_stock_friction(self, amount_aud: float) -> StockFriction:
        if amount_aud <= 0:
            return StockFriction(0, 0, 0)

        if amount_aud <= 1000:
            fee = self.commsec_tier_1
        elif amount_aud <= 10000:
            fee = self.commsec_tier_2
        elif amount_aud <= 25000:
            fee = self.commsec_tier_3
        else:
            fee = amount_aud * self.commsec_rate_high

        friction_pct = (fee / amount_aud) * 100
        return StockFriction(input_aud=amount_aud, fee_aud=fee, friction_pct=friction_pct)


def format_cost_report(snapshot: CostSnapshot) -> str:
    fx = snapshot.forex
    stock = snapshot.stock

    lines = [
        "--- FRICTION COST REPORT (Pre-calculated) ---",
        f"Input CNY: ¥{snapshot.invest_cny:.2f}",
        f"Spot Rate (AUD/CNY): {snapshot.spot_rate:.4f}",
        "",
        "[Scenario 1: Forex Transfer (CNY -> AUD)]",
        f"- Net AUD Received: ${fx.net_aud:.2f}",
        f"- Effective Rate (after fees): {fx.effective_rate:.4f}",
        f"- Total Friction Loss: {fx.friction_pct:.2f}% (¥{fx.total_fee_cny:.2f})",
        f"- Break-even Requirement: AUD must appreciate {fx.break_even_pct:.2f}%",
    ]
    if not fx.is_viable:
        lines.append("- Status: Not viable (fees exceed principal or inbound fees)")

    lines.extend([
        "",
        "[Scenario 2: Stock Trading (AUD -> NDQ)]",
        f"- Trade AUD: ${snapshot.trade_aud:.2f}",
        f"- Brokerage Fee: ${stock.fee_aud:.2f}",
        f"- Friction Loss: {stock.friction_pct:.2f}%",
    ])

    if snapshot.combined_fee_cny is not None:
        lines.extend([
            "",
            "[Scenario 3: Combined (FX + Brokerage)]",
            f"- Total Friction Loss: {snapshot.combined_friction_pct:.2f}% (¥{snapshot.combined_fee_cny:.2f})"
        ])

    return "\n".join(lines)


__all__ = [
    "ForexFriction",
    "StockFriction",
    "CostSnapshot",
    "TransactionCostCalculator",
    "format_cost_report",
]
