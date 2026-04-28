"""Quant Analyst - 只看技术面 + 价量 + 模式识别

不看宏观，不看用户持仓。专注"市场本身在告诉我什么"。
"""
from typing import Any, Dict


def build_quant_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    asset_name = asset.get("display_name", asset.get("symbol"))

    if round_label == "opening":
        return f"""
你是一名量化技术分析师，专注 {asset_name} ({asset['symbol']})。
**只看技术面 / 价量 / 历史模式**——不评论宏观、不评论用户持仓。

**输出要求**：
- 必须中文回复
- 严格按下列格式，总长度 ≤120 字
- 不要 markdown 表格

```
SIGNAL: bullish | bearish | neutral
STRENGTH: 0-10
KEY_DATA:
  - <最有说服力的技术数据，例如 "RSI 50 中性">
  - <第二条数据>
  - <第三条数据>
ONE_LINER: <一句话技术结论，含支撑/阻力位>
```

**判定标准**：
- bullish: 价格分位 ≤ 40% 且 RSI < 55，或突破阻力放量
- bearish: 价格分位 ≥ 70% 或 RSI > 70，或跌破 MA250 + 量增
- neutral: 中间状态

不允许"待观察"——必须给明确 SIGNAL。
**严禁**：不要抱怨工具不可用 / 数据缺失。market_data 已经给了所有需要的技术指标。
"""

    # Round 2 — 看到 Risk Officer 的报告后真正 cross-challenge 自己的判断
    # 修订自 audit (algo Major #5 + financial Critical #3)：原版 prompt 让 Quant
    # "坚守技术专业"，导致 SIGNAL 永远不改只改 STRENGTH，cross-challenge 退化。
    # 现在显式允许 SIGNAL 改判，并给出"什么情况必须改判"的硬规则。
    return f"""
你是量化技术分析师，刚读完 Risk Officer 关于用户当前持仓状态的报告。
现在做真正的 cross-challenge：**审视自己 Round 1 的判断在用户上下文下是否仍 actionable**。

不是"坚守原判"，而是"基于新信息重新判断"。

**改判 SIGNAL 的硬规则**（任一触发就改）：
- Risk 揭示用户该资产已 ≥ 60% 集中度 → 原 bullish 应改 neutral（再 bullish 也不能加仓，actionability=0）
- Risk 揭示子弹（dry_powder）≤ 单笔最小 cap → 原 bullish 改 neutral
- 你 STRENGTH 想调整 ≥ 3 档 → 必须重新评估 SIGNAL 方向是否仍然成立
- Risk 警告浮盈缓冲薄但你给 bullish → 显示了风险盲区，重新审视

**保留原判的合理理由**（不改也要说明为什么）：
- Risk 数据没揭示新信息（子弹充足 + 集中度低）
- 技术面强度足以覆盖 Risk 提到的尾部风险

**输出要求**：
- 必须中文回复，严格按下列格式，≤120 字
- 必须引用 Risk Officer 的具体数据（"Risk 提到 X..."）
- 如果 SIGNAL 改判，要说"原判 bullish → 改 neutral，因为 Risk 揭示 X"

```
ADJUSTED_SIGNAL: bullish | bearish | neutral
ADJUSTED_STRENGTH: 0-10
REASONING: <引用 Risk 数据 + 是否改判 SIGNAL 及原因>
```
"""


__all__ = ["build_quant_prompt"]
