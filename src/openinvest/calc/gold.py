"""黄金克价换算纯核（calc 层，ADR-026）

从 utils/gold_price.py 拆出的纯部分：快照数据类 + 常量 + 报告文本。
实时拉价 / DB 兜底 / offset 反推（内部拉现货价）留在 utils/gold_price.py（IO shell）。

公式：
    spot_cny_per_gram = (gold_usd_per_oz / 31.1035) * usdcny_rate
    bank_price = spot_cny_per_gram * (1 + offset_pct)
"""
from __future__ import annotations

from dataclasses import dataclass

GOLD_OZ_PER_GRAM = 31.1035


@dataclass
class GoldPriceSnapshot:
    gold_usd_per_oz: float
    usdcny_rate: float
    spot_cny_per_gram: float
    bank_cny_per_gram: float       # 渠道估算价 = spot * (1 + offset)
    offset_pct: float               # 当前使用的点差
    is_stale: bool = False          # 来自 DB 兜底（audit algo M7）


def format_gold_report(snap: GoldPriceSnapshot) -> str:
    """给 daily_report 邮件 / NapCat 用的展示文本"""
    return (
        f"--- GOLD PRICE SNAPSHOT ---\n"
        f"伦敦金现货 (GC=F): ${snap.gold_usd_per_oz:.2f}/oz\n"
        f"USD/CNY: {snap.usdcny_rate:.4f}\n"
        f"现货克价: ¥{snap.spot_cny_per_gram:.2f}/g\n"
        f"渠道估价 (offset {snap.offset_pct:.2%}): ¥{snap.bank_cny_per_gram:.2f}/g"
    )


__all__ = [
    "GOLD_OZ_PER_GRAM",
    "GoldPriceSnapshot",
    "format_gold_report",
]
