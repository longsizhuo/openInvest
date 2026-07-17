"""多币种汇率折算工具

用 yfinance 拉对手汇率（USDCNY=X / AUDCNY=X / EURCNY=X 等），
20 分钟内存 cache 避免 cron 频繁触发时打挂 yfinance。

约定：base 是目标币种（默认 CNY），quote 是源币种。
  to_base("USD", 100, base="CNY") = 100 * USDCNY
  to_base("CNY", 100, base="CNY") = 100（恒等）
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from openinvest.utils.exchange_fee import get_history_data

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


def get_fx_rate(
    quote: str, base: str = "CNY", as_of_date: Optional[str] = None,
) -> Optional[float]:
    """拉 quote → base 汇率（1 单位 quote 等于多少单位 base）

    例：get_fx_rate("USD", "CNY") ≈ 7.1
    例：get_fx_rate("USD", "CNY", as_of_date="2024-03-15") = 2024-03-15 当天汇率
    例：get_fx_rate("CNY", "CNY") = 1.0

    Args:
        as_of_date: backtest / paper trading 模式必传。给定日期时，返回该日期
            当天 close 汇率（用 ISO 'YYYY-MM-DD'）。**会绕过 20 分钟缓存**
            （不同 as_of_date 的汇率不能复用 cache）。
    """
    quote = quote.upper().strip()
    base = base.upper().strip()
    if quote == base:
        return 1.0

    # 实盘模式才用 cache（backtest 每次 as_of_date 不同，cache 没意义）
    if as_of_date is None:
        cached = _cache_get(quote, base)
        if cached is not None:
            return cached

    symbol = f"{quote}{base}=X"
    try:
        df = get_history_data(symbol, "5d", as_of_date=as_of_date)
    except Exception as e:  # noqa: BLE001
        log.warning(f"fx {symbol} 拉取失败: {e}")
        return None
    if df is None or df.empty:
        return None

    try:
        rate = float(df["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None

    if as_of_date is None:
        _cache_set(quote, base, rate)
    return rate


def to_base(
    quote: str, amount: float, base: str = "CNY",
    *, as_of_date: Optional[str] = None,
) -> Optional[float]:
    """把 amount（quote 计价）换成 base 计价金额

    as_of_date: backtest / paper trading 必传，按当天 close 拉 FX（绕过实时缓存）；
        None=实盘。不透传会导致历史估值用今天的汇率 → 前视偏差。
    """
    rate = get_fx_rate(quote, base, as_of_date=as_of_date)
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
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like to_base (see PR#53 fix(fx)).
        rate = get_fx_rate(ccy, base)
        rates[ccy] = rate
        if rate is not None:
            total += amt * rate
    return round(total, 2), rates


def total_portfolio_value_cny(
    pm,  # PortfolioManager（避免循环 import 不写类型）
    current_prices: dict,
    base: str = "CNY",
    *,
    as_of_date: Optional[str] = None,
) -> tuple[float, dict]:
    """通用化总资产估算：所有 cash 多币种 + 所有 holdings × current_prices × FX 折算到 base

    替代 7 处 `if ccy == "AUD"` 风格硬编码。fork 用户持 USD/EUR/JPY/HKD 等任意币种
    都能正确折算；缺价 / 缺汇率单独标 status 不计入 total（避免 0 兜底误导 Risk）。

    Args:
        pm: PortfolioManager 实例（只读 pm.cash + pm.holdings）
        current_prices: {symbol: 当前价 in cost_currency} dict。任一资产价格
            为 None 或缺 key 都视为"价拉不到"，不计入 total
        base: 目标币种（默认 CNY）
        as_of_date: backtest / paper trading 用，按当天 close 拉 FX；None=实盘

    Returns:
        (total_in_base, status_dict)
            total_in_base: float, 折算后总额（拉不到价/汇率的 entry 不计入）
            status_dict: {key: "ok" | "missing_price" | "missing_fx" | "tracking_only" |
                              "zero_units"}
                key 是 holding symbol 或 cash currency；调用方可以 grep
                "missing_*" 给 LLM 兜底告警
    """
    status: dict = {}

    # 1) cash 部分：多币种循环
    cash_dict = dict(pm.cash) if hasattr(pm, "cash") else {}
    total = 0.0
    for ccy, amt in cash_dict.items():
        if not amt:
            status[ccy] = "ok"   # 0 余额视为 ok（不算 fx 漂移）
            continue
        converted = to_base(ccy, float(amt), base, as_of_date=as_of_date)
        # NaN 等同 None：绝不 total += NaN（NaN 会污染整个总资产 → round(NaN)=NaN）
        if converted is None or not math.isfinite(converted):
            status[ccy] = "missing_fx"
        else:
            total += converted
            status[ccy] = "ok"

    # 2) holdings 部分：units × current_prices[sym] × FX
    holdings_iter = pm.holdings if hasattr(pm, "holdings") else []
    for h in holdings_iter:
        sym = str(h.get("symbol", "")) if isinstance(h, dict) else ""
        if not sym:
            continue
        if h.get("is_tracking_only"):
            status[sym] = "tracking_only"
            continue
        units = float(h.get("units", 0) or 0)
        if units <= 0:
            status[sym] = "zero_units"
            continue
        ccy = str(h.get("cost_currency", "CNY"))
        price = current_prices.get(sym) if current_prices else None
        # NaN 价（如当日 close=NULL 被读成 float('nan')）等同缺价 → 走 cost 兜底。
        # 只判 `is None` 会让 NaN 穿透守卫污染 total（round(NaN)=NaN），是本类 bug 根因。
        # price <= 0 同样是缺价信号：多处 _safe_close（skill_views/_helpers/coordinator）
        # 拉取失败返回 0.0 哨兵，当有效价会把整个持仓估值成 0、total 静默缩水且 status
        # 仍报 "ok"（CR 三个 agent 同时命中的 0.0 污染类）——一并按缺价走 cost 兜底。
        if price is None or not math.isfinite(price) or price <= 0:
            # 缺价：用 cost 兜底（与 portfolio_manager.get_user_status 同口径）
            avg = float(h.get("avg_cost", 0) or 0)
            if avg <= 0:
                status[sym] = "missing_price"
                continue
            price = avg
        # cost 兜底后仍非有限（理论上 avg 已 >0 finite，这里 belt-and-suspenders）
        if not math.isfinite(price):
            status[sym] = "missing_price"
            continue
        local_value = units * float(price)
        value_in_base = to_base(ccy, local_value, base, as_of_date=as_of_date)
        # 绝不 total += NaN：NaN 价/汇率单独标 status 排除，total 永远 finite
        if value_in_base is None or not math.isfinite(value_in_base):
            status[sym] = "missing_fx" if math.isfinite(local_value) else "missing_price"
            continue
        total += value_in_base
        status[sym] = "ok"

    return round(total, 2), status
