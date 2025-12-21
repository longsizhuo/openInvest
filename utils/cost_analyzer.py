from dataclasses import dataclass

@dataclass
class CostReport:
    initial_cny: float
    net_invested_aud: float
    total_fees_cny: float
    friction_rate: float  # 百分比，如 15.5 表示 15.5%
    breakdown: str
    is_prohibitive: bool  # 是否因为成本过高而应该禁止交易

class TransactionCostCalculator:
    def __init__(self):
        # --- 费率配置 (基于用户提供的真实数据) ---
        
        # 1. 中国银行 (CN)
        self.boc_cn_telegraph_fee = 150.0  # 电报费 (CNY)
        self.boc_cn_commission_rate = 0.001 # 0.1%
        self.boc_cn_min_commission = 50.0   # 最低 50 CNY
        self.boc_cn_max_commission = 260.0
        
        # 2. 澳洲银行 (AU) 接收
        self.au_inbound_fee_aud = 15.0      # 预估入账费
        
        # 3. CommSec (CDIA) 佣金阶梯
        self.commsec_tiers = [
            (1000, 5.00),    # 0-1000 -> $5
            (10000, 10.00),  # 1000-10000 -> $10
            (25000, 19.95),  # 10000-25000 -> $19.95
        ]
        self.commsec_high_rate = 0.0012 # >25000 -> 0.12%

    def calculate(self, amount_cny: float, exchange_rate_audcny: float) -> CostReport:
        """
        计算从 CNY 到 最终买入股票 的全链路损耗。
        """
        if amount_cny <= 0:
            return CostReport(0, 0, 0, 0, "No funds", False)

        # --- 步骤 1: 中国端费用 ---
        commission = max(self.boc_cn_min_commission, min(amount_cny * self.boc_cn_commission_rate, self.boc_cn_max_commission))
        cn_fees = self.boc_cn_telegraph_fee + commission
        
        # 剩余用于换汇的 CNY
        remitted_cny = amount_cny - cn_fees
        
        if remitted_cny <= 0:
            return CostReport(amount_cny, 0, amount_cny, 100.0, "Fees exceed principal", True)

        # --- 步骤 2: 换汇 (CNY -> AUD) ---
        # 汇率通常已经包含了银行点差，这里直接用传入的 market rate 近似
        # 如果要更严谨，可以给 rate 乘一个点差系数 (如 1.005)
        arrived_aud = remitted_cny / exchange_rate_audcny

        # --- 步骤 3: 澳洲端接收费用 ---
        net_cash_aud = arrived_aud - self.au_inbound_fee_aud
        
        if net_cash_aud <= 0:
            return CostReport(amount_cny, 0, amount_cny, 100.0, "AU fees exceed transferred amount", True)

        # --- 步骤 4: CommSec 交易佣金 ---
        brokerage = 0.0
        # 简单的阶梯判断
        if net_cash_aud <= 1000:
            brokerage = 5.00
        elif net_cash_aud <= 10000:
                    brokerage = 10.00
        elif net_cash_aud <= 25000:
                    brokerage = 19.95
        else:
                    brokerage = net_cash_aud * self.commsec_high_rate

        final_invested_aud = net_cash_aud - brokerage
        
        # --- 汇总统计 ---
        # 把所有 AUD 费用折算回 CNY 方便统计总损耗
        total_fees_aud = self.au_inbound_fee_aud + brokerage
        total_fees_cny_equiv = cn_fees + (total_fees_aud * exchange_rate_audcny)
        
        friction_rate = (total_fees_cny_equiv / amount_cny) * 100
        
        # 阻断逻辑：如果摩擦成本 > 5%，则认为不划算
        is_prohibitive = friction_rate > 5.0

        breakdown = (
            f"1. BOC CN Fees: ¥{cn_fees:.1f} (Cable ¥150 + Comm ¥{commission:.1f})\n"
            f"2. AU Inbound: ${self.au_inbound_fee_aud} AUD\n"
            f"3. CommSec Brokerage: ${brokerage:.2f} AUD\n"
            f"--------------------------------------------------\n"
            f"Total Loss: {friction_rate:.1f}% (¥{total_fees_cny_equiv:.0f})"
        )

        return CostReport(
            initial_cny=amount_cny,
            net_invested_aud=final_invested_aud,
            total_fees_cny=total_fees_cny_equiv,
            friction_rate=friction_rate,
            breakdown=breakdown,
            is_prohibitive=is_prohibitive
        )
