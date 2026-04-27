"""伦敦金 / 浙商积存金价格换算

公式：
    spot_cny_per_gram = (gold_usd_per_oz / 31.1035) * usdcny_rate
    bank_price = spot_cny_per_gram * (1 + offset_pct)

数据源：
- yfinance GC=F (COMEX 黄金期货 USD/oz) — XAUUSD=X 已被 Yahoo 下架
- yfinance USDCNY=X

Auto offset 推断：
- 每次用户在 NapCat 报当日浙商显示克价 → 反算 offset_pct 写回 strategy.md
- 这样不用手动维护点差，系统自动学习
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import yfinance as yf

GOLD_OZ_PER_GRAM = 31.1035


@dataclass
class GoldPriceSnapshot:
    gold_usd_per_oz: float
    usdcny_rate: float
    spot_cny_per_gram: float
    bank_cny_per_gram: float       # 浙商估算价 = spot * (1 + offset)
    offset_pct: float               # 当前使用的点差


def get_gold_snapshot(offset_pct: float = 0.015) -> Optional[GoldPriceSnapshot]:
    """拉一次实时黄金 + 美元人民币，算出克价

    offset_pct: 浙商积存金点差（默认 1.5%，可被 strategy.md 的 auto 推断值覆盖）
    """
    try:
        gold_df = yf.Ticker("GC=F").history(period="1d")
        usdcny_df = yf.Ticker("USDCNY=X").history(period="1d")
        if gold_df.empty or usdcny_df.empty:
            return None
        gold_usd = float(gold_df["Close"].iloc[-1])
        usdcny = float(usdcny_df["Close"].iloc[-1])
    except Exception as e:
        print(f"⚠️ 黄金数据拉取失败: {e}")
        return None

    spot = (gold_usd / GOLD_OZ_PER_GRAM) * usdcny
    bank = spot * (1 + offset_pct)
    return GoldPriceSnapshot(
        gold_usd_per_oz=gold_usd,
        usdcny_rate=usdcny,
        spot_cny_per_gram=spot,
        bank_cny_per_gram=bank,
        offset_pct=offset_pct,
    )


def infer_offset_pct(reported_bank_price_cny_per_gram: float) -> Optional[float]:
    """用户在 NapCat 报"今天浙商克价 1050"时，反推当下点差

    返回的 offset_pct 应该被写回 memory/strategy.md 的 target_assets[gold].price_offset_pct
    """
    snap = get_gold_snapshot(offset_pct=0.0)  # 拿现货价
    if snap is None or snap.spot_cny_per_gram <= 0:
        return None
    return reported_bank_price_cny_per_gram / snap.spot_cny_per_gram - 1.0


def format_gold_report(snap: GoldPriceSnapshot) -> str:
    """给 daily_report 邮件 / NapCat 用的展示文本"""
    return (
        f"--- GOLD PRICE SNAPSHOT ---\n"
        f"伦敦金现货 (GC=F): ${snap.gold_usd_per_oz:.2f}/oz\n"
        f"USD/CNY: {snap.usdcny_rate:.4f}\n"
        f"现货克价: ¥{snap.spot_cny_per_gram:.2f}/g\n"
        f"浙商积存金估价 (offset {snap.offset_pct:.2%}): ¥{snap.bank_cny_per_gram:.2f}/g"
    )


if __name__ == "__main__":
    snap = get_gold_snapshot()
    if snap:
        print(format_gold_report(snap))
    else:
        print("⚠️ 无法获取黄金数据")
