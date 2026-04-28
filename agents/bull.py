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

**输出要求：必须使用中文回复，且总长度严格控制在 150 字以内**。

按以下三点结构回答（每点 1-2 句，不要展开）：
1. **技术面**: 价格分位 / RSI / 趋势支撑（一句话给数字）
2. **基本面/驱动**: 选最关键的一条（财报/政策/避险/利率）
3. **目标 / 触发位**: 一个具体数字（目标价或突破位）

不要写成长文，不要 markdown 表格，不要 bullet 列表过度展开。

{type_specific}
"""
    else:  # rebuttal
        instruction = f"""
你是【看多】交易员，刚才看到了 Bear 的看空论据。
**坚守 BUY 立场**，逐点反驳 Bear（不要答非所问）。

**输出要求：必须使用中文回复，总长度严格控制在 120 字以内**。

要求：
1. 引用 Bear 的具体一条论点开头（"Bear 说 X，但...."）
2. 用一个数据/事实反驳
3. 不要重复你 opening 的论据
4. 不要写表格 / 不要过度展开
"""
    return instruction.strip()


__all__ = ["build_bull_prompt"]
