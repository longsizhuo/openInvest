"""看多 agent (Bull)

立场固定：寻找加仓理由。任务不是"客观分析"而是"用尽全力论证 buy 的最强 case"，
让 Bear 来反驳。这种 adversarial 设计降低 LLM 的 yes-man 倾向。
"""
from typing import Any, Dict


def build_bull_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    asset_name = asset.get("display_name", asset.get("symbol"))
    asset_type = asset.get("type", "equity_etf")

    type_specific = ""
    if asset_type == "metal":
        type_specific = """
- 黄金看多视角：避险升温 / 美元走弱 / 实际利率下降 / 央行购金 / 通胀粘性
- 关注 ^VIX 走高、DXY/USDCNY 走弱、^TNX 下行
"""
    else:
        type_specific = """
- 股票看多视角：基本面改善 / 估值合理 / 趋势线突破 / 政策利好 / 资金流入
- 关注 RSI 回升至 50+、价格站稳 MA250、宏观流动性宽松
"""

    if round_label == "opening":
        instruction = f"""
你是一名坚定的【看多】交易员，专注 {asset_name} ({asset['symbol']})。
**你的立场是 BUY**。任务是给出最有力的看多论据，让市场上的人看完想买入。

请用 200 字以内给出 3 条最强论据：
1. **技术面**: 价格分位、RSI、趋势支撑
2. **基本面/驱动**: 财报/政策/资金流/避险/利率（根据资产类型选最相关的）
3. **不对称机会**: 当前下行风险 vs 上行空间的对比

最后一句给一个**目标价位**或**关键观察指标**（如 RSI 阈值、价格突破点）。

{type_specific}
"""
    else:  # rebuttal
        instruction = f"""
你是【看多】交易员，刚才看到了 Bear 的看空论据。
**坚守 BUY 立场**，逐点反驳 Bear 的核心观点（不要答非所问）。

要求：
1. 引用 Bear 的具体论点（"Bear 说 X，但...."）
2. 用数据/历史/事实反驳，而非情绪
3. 200 字以内，锐利不啰嗦
4. 不要重复你 opening round 的论据
"""
    return instruction.strip()


__all__ = ["build_bull_prompt"]
