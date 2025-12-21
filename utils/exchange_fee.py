import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

CACHE_DIR = "cache_data"


# ==========================================
# 1. 交易摩擦成本计算器 (纯数学逻辑)
# ==========================================
@dataclass
class ForexFriction:
    input_cny: float
    net_aud: float
    spot_rate: float
    effective_rate: float  # 真实汇率 (含手续费)
    friction_pct: float  # 损耗百分比
    total_fee_cny: float
    break_even_pct: float  # 盈亏平衡点(%)
    is_viable: bool  # 基于硬性数学阈值(如损耗>50%)的可用性标记，非投资建议


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
    """
    负责计算跨境投资的客观摩擦成本。
    不做任何投资建议，只返回 int/float 数据。
    """

    def __init__(self):
        # 1. 中国银行 (CN) 汇出费率
        self.cn_cable_fee = 150.0  # 电报费 (固定)
        self.cn_commission_rate = 0.001  # 0.1%
        self.cn_commission_min = 50.0  # 最低 50 CNY
        self.cn_commission_max = 260.0  # 最高 260 CNY

        # 2. 澳洲银行 (AU) 接收费率
        self.au_inward_fee = 15.0  # 预估入账费 (AUD)

        # 3. CommSec (CDIA) 交易佣金
        self.commsec_tier_1 = 5.0  # < 1000
        self.commsec_tier_2 = 10.0  # 1000 - 10000
        self.commsec_tier_3 = 19.95  # 10000 - 25000
        self.commsec_rate_high = 0.0012  # > 25000

    def calculate_forex_friction(self, invest_cny: float, spot_rate: float) -> ForexFriction:
        """
        计算换汇环节的真实数学损耗
        """
        if invest_cny <= 0 or spot_rate <= 0:
            return ForexFriction(0, 0, 0, 0, 0, 0, 0, False)

        # Step 1: 国内扣费
        commission = max(self.cn_commission_min, min(invest_cny * self.cn_commission_rate, self.cn_commission_max))
        cn_total_fee = self.cn_cable_fee + commission

        remaining_cny = invest_cny - cn_total_fee

        # 极端情况：本金不够付手续费
        if remaining_cny <= 0:
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, cn_total_fee, float('inf'), False)

        # Step 2: 澳洲入账
        gross_aud = remaining_cny / spot_rate
        net_aud = gross_aud - self.au_inward_fee

        if net_aud <= 0:
            # 钱在澳洲入账时扣光了
            total_fee_cny_equiv = cn_total_fee + (gross_aud * spot_rate)
            return ForexFriction(invest_cny, 0, spot_rate, float('inf'), 100.0, total_fee_cny_equiv, float('inf'),
                                 False)

        # Step 3: 指标计算
        # 真实汇率 = 投入CNY / 到手AUD
        effective_rate = invest_cny / net_aud

        # 损耗金额 (CNY) = 投入 - (到手AUD * 市场汇率)
        value_loss_cny = invest_cny - (net_aud * spot_rate)
        friction_pct = (value_loss_cny / invest_cny) * 100

        # 回本需求：(1 / (1 - loss%)) - 1
        # 例如损耗 20%，剩余 0.8。0.8 * (1+x) = 1 => x = 25%
        if friction_pct >= 100:
            break_even_pct = float('inf')
        else:
            break_even_pct = (1 / (1 - friction_pct / 100) - 1) * 100

        # is_viable 仅作为数学上的可行性标记 (例如损耗是否导致本金归零)
        is_viable = net_aud > 0

        return ForexFriction(
            input_cny=invest_cny,
            net_aud=net_aud,
            spot_rate=spot_rate,
            effective_rate=effective_rate,
            friction_pct=friction_pct,
            total_fee_cny=value_loss_cny,
            break_even_pct=break_even_pct,
            is_viable=is_viable
        )

    def calculate_stock_friction(self, amount_aud: float) -> StockFriction:
        """
        计算交易环节的真实数学损耗
        """
        if amount_aud <= 0:
            return StockFriction(0, 0, 0)

        fee = 0.0
        if amount_aud <= 1000:
            fee = self.commsec_tier_1
        elif amount_aud <= 10000:
            fee = self.commsec_tier_2
        elif amount_aud <= 25000:
            fee = self.commsec_tier_3
        else:
            fee = amount_aud * self.commsec_rate_high

        friction_pct = (fee / amount_aud) * 100

        return StockFriction(
            input_aud=amount_aud,
            fee_aud=fee,
            friction_pct=friction_pct
        )


# -----------------------------
# 1. 通用数据获取 (Generic Fetcher)
# -----------------------------
def get_history_data(symbol: str, period: str = "2y") -> pd.DataFrame:
    """
    通用获取函数：既可以抓股票(如 NDQ.AX)，也可以抓汇率(AUDCNY=X)。
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    # 处理一下文件名，避免 symbol 里有特殊字符导致路径错误
    safe_symbol = symbol.replace("=", "").replace(".", "_")
    csv_path = os.path.join(CACHE_DIR, f"{safe_symbol}_{period}.csv")

    # [Hit]
    if os.path.exists(csv_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(csv_path))
        if file_time.date() == datetime.now().date():
            return pd.read_csv(csv_path, index_col=0, parse_dates=True)

    # [Miss]
    print(f"🔄 [API Update] 正在更新数据: {symbol}...")
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if not hist.empty:
            hist.to_csv(csv_path)
            return hist
    except Exception as e:
        print(f"⚠️ 获取数据失败 {symbol}: {e}")

    return pd.DataFrame()


# -----------------------------
# 2. 数学工具 (保持不变)
# -----------------------------
def _calc_change(start: float, end: float) -> float:
    if start == 0: return 0.0
    return (end - start) / start


def _calc_max_drawdown(series: pd.Series) -> float:
    if series.empty: return 0.0
    roll_max = series.cummax()
    drawdown = (series - roll_max) / roll_max
    return drawdown.min()


def _calc_volatility(series: pd.Series) -> float:
    if len(series) < 2: return 0.0
    return series.pct_change().std() * np.sqrt(252)


def _calc_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1: return 50.0
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    if loss.iloc[-1] == 0: return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def _analyze_slice(df_slice: pd.DataFrame, label: str, current_price: float) -> str:
    if df_slice.empty:
        return f"- **{label}**: No Data"
    start_price = df_slice['Close'].iloc[0]
    change = _calc_change(start_price, current_price)
    mdd = _calc_max_drawdown(df_slice['Close'])
    vol_str = ""
    if len(df_slice) > 20:
        vol = _calc_volatility(df_slice['Close'])
        vol_str = f", Vol: {vol:.2%}"
    return f"- **{label}**: Ret: {change:.2%}, MaxDD: {mdd:.2%}{vol_str}"


# -----------------------------
# 3. 分析逻辑 (支持传入自定义标题)
# -----------------------------
def analyze_multi_timeframe(hist: pd.DataFrame, title: str) -> str:
    """
    通用分析器：传入 Dataframe 和 标题(如 'NDQ' 或 'AUD/CNY')
    """
    if hist.empty:
        return f"数据缺失: {title}"

    current_price = hist['Close'].iloc[-1]
    ma_20 = hist['Close'].rolling(window=20).mean().iloc[-1]
    ma_120 = hist['Close'].rolling(window=120).mean().iloc[-1]
    ma_250 = hist['Close'].rolling(window=250).mean().iloc[-1]
    rsi_14 = _calc_rsi(hist['Close'])

    slices = {
        "1-Week": hist.tail(5),
        "1-Month": hist.tail(21),
        "6-Months": hist.tail(126),
        "1-Year": hist.tail(252),
        "2-Years": hist
    }

    report_lines = [f"--- {title} ANALYSIS ---", f"Current Price: {current_price:.4f} | RSI(14): {rsi_14:.2f}"]

    # 判断当前价格位置 (Percentile)
    high_2y = hist['Close'].max()
    low_2y = hist['Close'].min()
    pos = (current_price - low_2y) / (high_2y - low_2y)
    report_lines.append(f"Price Rank (2y): {pos:.0%} (0%=Low, 100%=High)")

    report_lines.append("**Timeframe Performance:**")
    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("**Key Levels:**")
    report_lines.append(f"- MA120 (Trend): {ma_120:.4f}")
    report_lines.append(f"- MA250 (Base): {ma_250:.4f}")
    if pd.notna(ma_250):
        bias = (current_price / ma_250 - 1)
        report_lines.append(f"- MA250 Deviation: {bias:.2%}")

    return "\n".join(report_lines)


# -----------------------------
# 4. 对外接口：获取整合报告
# -----------------------------
def get_macro_data() -> str:
    """
    获取宏观关键指标：10年期美债收益率 (^TNX) 和 恐慌指数 (^VIX)
    """
    try:
        tnx = get_history_data("^TNX", "1mo")  # 10-Year Treasury Yield
        vix = get_history_data("^VIX", "1mo")  # CBOE Volatility Index

        tnx_last = tnx['Close'].iloc[-1] if not tnx.empty else 0.0
        tnx_change = _calc_change(tnx['Close'].iloc[0], tnx_last)

        vix_last = vix['Close'].iloc[-1] if not vix.empty else 0.0
        vix_change = _calc_change(vix['Close'].iloc[0], vix_last)

        return f"""
--- MACRO INDICATORS (Reference) ---
1. US 10Y Treasury Yield (^TNX): {tnx_last:.2f}% (MoM: {tnx_change:+.2%})
   *Note: Rising yields often hurt tech stock valuations.*

2. CBOE Volatility Index (^VIX): {vix_last:.2f} (MoM: {vix_change:+.2%})
   *Note: VIX > 20 indicates fear; VIX < 15 indicates complacency.*
"""
    except Exception as e:
        return f"Error fetching macro data: {e}"


def get_full_market_data(target_asset: str = "NDQ.AX") -> str:
    # 1. 获取目标资产数据
    df_asset = get_history_data(target_asset, "2y")
    report_asset = analyze_multi_timeframe(df_asset, f"TARGET ASSET ({target_asset})")

    # 2. 获取 澳元兑人民币 (AUDCNY=X) 数据
    # Yahoo Finance 代码: AUDCNY=X
    df_fx = get_history_data("AUDCNY=X", "2y")
    report_fx = analyze_multi_timeframe(df_fx, "CURRENCY RATE (AUD/CNY)")

    return f"""
{report_asset}

{report_fx}
"""


def get_cost_snapshot(
    invest_cny: float,
    amount_aud: Optional[float] = None,
    spot_rate: Optional[float] = None
) -> CostSnapshot:
    """
    计算并返回结构化摩擦成本，避免让 LLM 做数学运算。
    """
    calc = TransactionCostCalculator()

    if spot_rate is None:
        df_fx = get_history_data("AUDCNY=X", "1d")
        spot_rate = df_fx['Close'].iloc[-1] if not df_fx.empty else 0.0

    fx_data = calc.calculate_forex_friction(invest_cny, spot_rate)

    trade_from_fx = amount_aud is None
    if amount_aud is None:
        amount_aud = fx_data.net_aud if fx_data.is_viable else 0.0

    stock_data = calc.calculate_stock_friction(amount_aud)

    combined_fee_cny = None
    combined_friction_pct = None
    if trade_from_fx and invest_cny > 0 and spot_rate > 0 and amount_aud > 0:
        combined_fee_cny = fx_data.total_fee_cny + (stock_data.fee_aud * spot_rate)
        combined_friction_pct = (combined_fee_cny / invest_cny) * 100

    return CostSnapshot(
        invest_cny=invest_cny,
        spot_rate=spot_rate,
        forex=fx_data,
        trade_aud=amount_aud,
        stock=stock_data,
        combined_fee_cny=combined_fee_cny,
        combined_friction_pct=combined_friction_pct
    )


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


def get_cost_report(
    invest_cny: float,
    amount_aud: Optional[float] = None,
    spot_rate: Optional[float] = None
) -> str:
    """
    生成纯客观的成本数据报告，供 LLM 参考。
    不包含任何建议性文字。
    """
    snapshot = get_cost_snapshot(
        invest_cny=invest_cny,
        amount_aud=amount_aud,
        spot_rate=spot_rate
    )
    return format_cost_report(snapshot)


# --- 测试 ---
if __name__ == "__main__":
    print(get_full_market_data())
    print(get_cost_report(50000, 3000))
