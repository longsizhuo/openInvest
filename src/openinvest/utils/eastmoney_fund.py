"""东方财富场外公募基金单位净值适配器。

openInvest 的交易所资产继续走 yfinance；中国场外公募基金使用独立的
``FUND:<六位代码>`` symbol，避免把基金代码误标成 ``.SS`` / ``.SZ``。

当前只提供最新已确认单位净值（不是盘中估值），足够用于组合估值与 P&L。
接口失败时返回 ``None``，由统一 quote 层按缺价策略降级。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_SEARCH_URL = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
_FUND_CODE_RE = re.compile(r"^(?:FUND:)?(\d{6})(?:\.(?:OF|SS|SZ))?$", re.IGNORECASE)


@dataclass(frozen=True)
class FundNavSnapshot:
    code: str
    name: str
    nav: float
    nav_date: str
    is_stale: bool


def extract_fund_code(symbol: str) -> Optional[str]:
    """从 ``FUND:162201`` / ``162201`` / 历史误标 ``162201.SZ`` 提取代码。"""
    match = _FUND_CODE_RE.fullmatch(str(symbol or "").strip())
    return match.group(1) if match else None


def canonical_fund_symbol(symbol: str) -> str:
    """返回场外基金 canonical symbol；无法识别时保留原值。"""
    code = extract_fund_code(symbol)
    return f"FUND:{code}" if code else str(symbol or "").strip()


def _parse_nav_date(raw: Any) -> tuple[str, bool]:
    nav_date = str(raw or "").strip()
    try:
        parsed = datetime.strptime(nav_date, "%Y-%m-%d").date()
    except ValueError:
        return nav_date, True
    # QDII 净值正常会滞后数日；超过 10 个自然日才标 stale。
    return nav_date, (date.today() - parsed).days > 10


@lru_cache(maxsize=256)
def fetch_fund_nav(symbol: str, *, timeout: float = 8.0) -> Optional[FundNavSnapshot]:
    """获取最新已确认单位净值；网络/结构异常时返回 ``None``。"""
    code = extract_fund_code(symbol)
    if not code:
        return None
    try:
        response = requests.get(
            _SEARCH_URL,
            params={"m": "1", "key": code},
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "openInvest/0.34 (+https://github.com/longsizhuo/openInvest)",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("Datas") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return None
        exact = next(
            (item for item in entries if str(item.get("CODE") or "") == code),
            None,
        )
        if not isinstance(exact, dict):
            return None
        base = exact.get("FundBaseInfo") or {}
        nav = float(base.get("DWJZ") or 0)
        if nav <= 0:
            return None
        nav_date, is_stale = _parse_nav_date(base.get("FSRQ"))
        return FundNavSnapshot(
            code=code,
            name=str(base.get("SHORTNAME") or exact.get("NAME") or code),
            nav=nav,
            nav_date=nav_date,
            is_stale=is_stale,
        )
    except (requests.RequestException, TypeError, ValueError) as exc:
        log.warning("东方财富基金净值获取失败 %s: %s", code, exc)
        return None
