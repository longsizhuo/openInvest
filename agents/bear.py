"""看空 agent (Bear)

立场固定：寻找减仓/观望理由。跟 Bull 对辩，由 Judge 裁决。
"""
from typing import Any, Dict


def build_bear_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    asset_name = asset.get("display_name", asset.get("symbol"))
    asset_type = asset.get("type", "equity_etf")

    type_specific = ""
    if asset_type == "metal":
        type_specific = """
- 黄金看空视角：美元走强 / 实际利率上升 / 风险偏好回升 / 央行减持 / 价格超买
- 关注 ^VIX 下行、DXY/USDCNY 走强、^TNX 上行
"""
    else:
        type_specific = """
- 股票看空视角：估值过高 / 盈利下修 / 流动性收紧 / 板块轮动外流 / 趋势破位
- 关注 RSI > 70、MA250 失守、宏观滞胀风险
"""

    if round_label == "opening":
        instruction = f"""
你是一名警觉的【看空】交易员，专注 {asset_name} ({asset['symbol']})。
**你的立场是 SELL 或 HOLD**。任务是用最有力的看空论据让买家三思。

请用 200 字以内给出 3 条最强论据：
1. **技术面**: 超买信号、关键支撑破位、量价背离
2. **宏观/驱动**: 政策风险 / 利率压力 / 地缘 / 估值 / 流动性收紧
3. **不对称风险**: 现在入场的最大下行可能多大？

最后一句给一个**离场触发线**（如 RSI < X 或价格跌破 Y 应止损）。

{type_specific}
"""
    else:  # rebuttal
        instruction = f"""
你是【看空】交易员，刚才看到了 Bull 的看多论据。
**坚守谨慎立场**，逐点反驳 Bull（不要泛泛而谈）。

要求：
1. 引用 Bull 的具体论点（"Bull 说 X，但...."）
2. 用数据/事实/历史先例反驳
3. 200 字以内
4. 不要重复你 opening 论据
"""
    return instruction.strip()


__all__ = ["build_bear_prompt"]
