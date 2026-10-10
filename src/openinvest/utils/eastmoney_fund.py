"""东方财富场外公募基金单位净值适配器（仓库内东方财富基金取数的唯一入口）。

openInvest 的交易所资产继续走 yfinance；中国场外公募基金使用独立的
``FUND:<六位代码>`` symbol，避免把基金代码误标成 ``.SS`` / ``.SZ``。

两个取数函数：
- ``fetch_fund_nav``：最新已确认单位净值（不是盘中估值），组合估值与 P&L 用。
  走 f10 历史净值接口第一页（响应约 400B），进程内 TTL 缓存，只缓存成功结果。
  lsjz（api.fund.eastmoney.com）本机实测间歇超时，失败时退到 pingzhongdata 的最后一个点。
- ``fetch_fund_nav_history``：成立以来全部单位净值（pingzhongdata，几百 KB），
  ``core/benchmarks`` 的基金基准与后续历史序列需求共用。

接口失败时返回 ``None``，由统一 quote 层按缺价策略降级。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
# lsjz 校验 Referer，缺了直接返回 ErrCode=-999 空数据
_HEADERS = {
    "Referer": "https://fundf10.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}
_FUND_CODE_RE = re.compile(r"^(?:FUND:)?(\d{6})(?:\.(?:OF|SS|SZ))?$", re.IGNORECASE)
_PINGZHONG_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", re.DOTALL)
# 净值日期按北京时间算：pingzhongdata 的 x 是北京时间零点的毫秒时间戳，
# 按宿主机时区转换会在 UTC 机器上整体早一天
_CN_TZ = timezone(timedelta(hours=8))
# QDII 净值正常会滞后数日；超过 10 个自然日才标 stale
_STALE_DAYS = 10
# 净值一天只更新一次；常驻进程（MCP server / scheduler / web）过了 TTL 重新拉
_NAV_TTL_SECONDS = 30 * 60

_nav_cache: Dict[str, Tuple[float, "FundNavSnapshot"]] = {}
_nav_cache_lock = threading.Lock()


@dataclass(frozen=True)
class FundNavSnapshot:
    code: str
    nav: float
    nav_date: str
    is_stale: bool


def extract_fund_code(symbol: str) -> Optional[str]:
    """从 ``FUND:123456`` / ``123456`` / 历史误标 ``123456.SZ`` 提取代码。"""
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
    return nav_date, (datetime.now(_CN_TZ).date() - parsed).days > _STALE_DAYS


def clear_nav_cache() -> None:
    """清空最新净值缓存（测试用）。"""
    with _nav_cache_lock:
        _nav_cache.clear()


def fetch_fund_nav(symbol: str, *, timeout: float = 8.0) -> Optional[FundNavSnapshot]:
    """获取最新已确认单位净值；网络/结构异常时返回 ``None``。

    失败结果不缓存：一次网络抖动不会让该基金在常驻进程里一直缺价。
    """
    code = extract_fund_code(symbol)
    if not code:
        return None
    now = time.monotonic()
    with _nav_cache_lock:
        hit = _nav_cache.get(code)
    if hit is not None and now - hit[0] < _NAV_TTL_SECONDS:
        return hit[1]
    snap = _fetch_latest_nav(code, timeout) or _latest_nav_from_history(code, timeout)
    if snap is not None:
        with _nav_cache_lock:
            _nav_cache[code] = (now, snap)
    return snap


def _fetch_latest_nav(code: str, timeout: float) -> Optional[FundNavSnapshot]:
    try:
        response = requests.get(
            _LSJZ_URL,
            params={"fundCode": code, "pageIndex": 1, "pageSize": 1},
            headers=_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("Data") if isinstance(payload, dict) else None
        rows = data.get("LSJZList") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        row = rows[0]
        # 货币基金等没有单位净值（DWJZ 为空）→ 视为不支持
        nav = float(row.get("DWJZ") or 0)
        if not math.isfinite(nav) or nav <= 0:
            return None
        nav_date, is_stale = _parse_nav_date(row.get("FSRQ"))
        return FundNavSnapshot(code=code, nav=nav, nav_date=nav_date, is_stale=is_stale)
    except (requests.RequestException, TypeError, ValueError) as exc:
        log.warning("东方财富基金净值获取失败 %s: %s", code, exc)
        return None


def _latest_nav_from_history(code: str, timeout: float) -> Optional[FundNavSnapshot]:
    """lsjz 兜底：pingzhongdata（另一个域名）的最后一个点，stale 规则同 lsjz。"""
    history = fetch_fund_nav_history(code, timeout=timeout)
    if not history:
        return None
    nav_date, nav = history[-1]
    _, is_stale = _parse_nav_date(nav_date)
    return FundNavSnapshot(code=code, nav=nav, nav_date=nav_date, is_stale=is_stale)


def fetch_fund_nav_history(
    symbol: str, *, timeout: float = 10.0, adjusted: bool = False,
) -> Optional[List[Tuple[str, float]]]:
    """成立以来全部单位净值 ``[(YYYY-MM-DD, nav), ...]``（日期升序）；失败返回 ``None``。

    ``adjusted=True`` 返回前复权净值：最新一天等于真实单位净值，更早的值按日收益率
    往前回推（同 yfinance auto_adjust）。分红日单位净值会掉一截（实测一只主动权益
    基金历次分红单日 -2.7%~-11.6%），拿未复权序列算 ATR/回撤就是假暴跌。
    """
    code = extract_fund_code(symbol)
    if not code:
        return None
    try:
        response = requests.get(
            _PINGZHONG_URL.format(code=code), headers=_HEADERS, timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("东方财富基金历史净值获取失败 %s: %s", code, exc)
        return None
    match = _PINGZHONG_RE.search(response.text)
    if not match:
        log.warning("东方财富基金 %s: Data_netWorthTrend 字段未找到", code)
        return None
    try:
        items = json.loads(match.group(1))
    except ValueError as exc:
        log.warning("东方财富基金 %s 历史净值解析失败: %s", code, exc)
        return None
    if not isinstance(items, list):
        return None
    out: List[Tuple[str, float]] = []
    ratios: List[float] = []   # ratios[i] = 第 i 天相对前一天的复权收益比
    for item in items:
        try:
            ts_ms = int(item["x"])
            nav = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(nav) or nav <= 0:
            continue
        nav_date = datetime.fromtimestamp(ts_ms / 1000, tz=_CN_TZ).strftime("%Y-%m-%d")
        ratio = nav / out[-1][1] if out else 1.0
        if out and item.get("unitMoney"):
            # 分红/拆分日：净值比含除息缺口，改用官方日增长率 equityReturn（已含分红）；
            # 平日用净值比——equityReturn 只保留 2~4 位小数，逐日连乘会漂
            try:
                er = 1 + float(item.get("equityReturn")) / 100
                if math.isfinite(er) and er > 0:
                    ratio = er
            except (TypeError, ValueError):
                pass  # 缺日增长率 → 这一天不复权
        out.append((nav_date, nav))
        ratios.append(ratio)
    if not adjusted or not out:
        return out
    adj = [out[-1][1]]
    for ratio in reversed(ratios[1:]):
        adj.append(adj[-1] / ratio)
    return [(d, a) for (d, _), a in zip(out, reversed(adj))]
