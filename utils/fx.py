"""多币种汇率折算工具

用 yfinance 拉对手汇率（USDCNY=X / AUDCNY=X / EURCNY=X 等），
20 分钟内存 cache 避免 cron 频繁触发时打挂 yfinance。

约定：base 是目标币种（默认 CNY），quote 是源币种。
  to_base("USD", 100, base="CNY") = 100 * USDCNY
  to_base("CNY", 100, base="CNY") = 100（恒等）
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from utils.exchange_fee import get_history_data

log = logging.getLogger(__name__)

# 内存 cache：{(quote, base): (rate, ts)}
_RATE_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_CACHE_TTL_SEC = 20 * 60   # 20 分钟


def _cache_get(quote: str, base: str) -> Optional[float]:
    item = _RATE_CACHE.get((quote, base))
    if item is None:
        return None
    rate, ts = item
    if time.time() - ts > _CACHE_TTL_SEC:
        return None
    return rate


def _cache_set(quote: str, base: str, rate: float) -> None:
    _RATE_CACHE[(quote, base)] = (rate, time.time())


def get_fx_rate(quote: str, base: str = "CNY") -> Optional[float]:
    """拉 quote → base 汇率（1 单位 quote 等于多少单位 base）

    例：get_fx_rate("USD", "CNY") ≈ 7.1（1 美元 ≈ 7.1 人民币）
    例：get_fx_rate("AUD", "CNY") ≈ 4.9
    例：get_fx_rate("CNY", "CNY") = 1.0

    实现：
    - quote == base → 恒等返回 1.0
    - 否则尝试 quoteBASE=X yfinance symbol（如 USDCNY=X）
    - cache 20 min 命中即返
    - 失败返回 None（caller 决定是否用兜底值）
    """
    quote = quote.upper().strip()
    base = base.upper().strip()
    if quote == base:
        return 1.0

    cached = _cache_get(quote, base)
    if cached is not None:
        return cached

    symbol = f"{quote}{base}=X"
    try:
        df = get_history_data(symbol, "5d")
    except Exception as e:  # noqa: BLE001
        log.warning(f"fx {symbol} 拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None

    try:
        rate = float(df["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None

    _cache_set(quote, base, rate)
    return rate


def to_base(quote: str, amount: float, base: str = "CNY") -> Optional[float]:
    """把 amount（quote 计价）换成 base 计价金额"""
    rate = get_fx_rate(quote, base)
    if rate is None:
        return None
    return amount * rate


def cash_total_in_base(
    cash: dict[str, float], base: str = "CNY",
) -> tuple[float, dict[str, Optional[float]]]:
    """把多币种 cash dict 折算到 base 总额

    返回 (total, per_currency_rate_or_None)：
        total: 折算后的总金额（拉不到汇率的币种不计入）
        per_currency_rate_or_None: 每个币种的汇率（None 表示拉不到）

    例: cash_total_in_base({"CNY": 100, "USD": 50}) = (455.0, {"CNY": 1.0, "USD": 7.1})
    """
    total = 0.0
    rates: dict[str, Optional[float]] = {}
    for ccy, amt in cash.items():
        rate = get_fx_rate(ccy, base)
        rates[ccy] = rate
        if rate is not None:
            total += amt * rate
    return round(total, 2), rates
