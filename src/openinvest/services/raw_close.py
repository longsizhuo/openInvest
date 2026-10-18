"""backfill_raw_close：给 ``daily_prices`` 已有的行回填原始价 ``close_raw``（ADR-027 PR2）

``close`` 是 yfinance 复权价（Adj Close：拆股 + 分红都复权），regime / 技术指标 / forward_return 一路读它，
语义不变。``close_raw`` 是 yfinance 的 ``Close``：只按拆股复权、不含分红复权，账户估值读它——账本里的分红
现金是真钱，历史价再复权一遍就把同一笔分红算了两次。新抓取（get_history_data / scripts.backfill_history）
已经两列一起写；本命令补**历史上**已经入库、当时没有 close_raw 的行（``period="max"``，只 UPDATE 已有行，
不新增行、不动 close / OHLCV / source，幂等，可重复跑）。

默认标的 = 当前持仓 + strategy.target_assets（跳过追踪仓外的无关标的），**不回填**：

- 场外基金（``FUND:<六位代码>``）：东方财富只给前复权净值（分红再投资口径），没有未复权序列可取——保持现状，
  估值读复权净值（``price_basis=adjusted``）。
- 黄金克价代理（``proxy_kind=gold_cny_per_gram``，GC=F × USDCNY=X）：期货 / 外汇没有分红，Adj Close == Close，
  复权价和原始价本来就是同一个数；克价本身又是两个序列合成的，没有「原始克价」可言——保持现状。

某只回填后仍有 close_raw 为 NULL 的行（库里有、yfinance 没有的日子，例如 betashares NAV 兜底写进去的日子）
会在返回值里报 ``null_rows``；估值层的规则是「所需日期范围内缺一天就整只回退复权价」，所以不会混用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yfinance as yf

from openinvest.db.market_store import MarketStore
from openinvest.utils.exchange_fee import history_with_raw
from openinvest.utils.quotes import is_eastmoney_fund

log = logging.getLogger(__name__)

GOLD_PROXY_KIND = "gold_cny_per_gram"
SKIP_FUND = "eastmoney_fund: 只有前复权净值，没有未复权序列"
SKIP_GOLD = "gold_proxy: 期货 / 外汇没有分红，复权价 == 原始价"


def default_symbols(pm) -> Tuple[List[str], Dict[str, str]]:
    """(要回填的行情库 symbol 列表, {跳过的 symbol: 原因})：持仓 + target_assets，去重保序。
    行情库里的 symbol = ``yfinance_proxy or symbol``（同 account_performance._price_series 的取价口径）。"""
    wanted: List[str] = []
    skipped: Dict[str, str] = {}
    rows = [dict(h) for h in pm.holdings if not h.get("is_tracking_only")]
    try:
        rows += [dict(a) for a in (pm.strategy.get("target_assets") or [])]
    except Exception:  # noqa: BLE001  strategy 读不到不挡持仓的回填
        pass
    for h in rows:
        sym = str(h.get("symbol") or "").strip()
        if not sym or sym in skipped:   # 持仓行先判过的（黄金 / 基金）：同名的 target_assets 行没有 proxy_kind，别又把它加回来
            continue
        if str(h.get("proxy_kind") or "") == GOLD_PROXY_KIND:
            skipped[sym] = SKIP_GOLD
        elif is_eastmoney_fund(h) or sym.upper().startswith("FUND:"):
            skipped[sym] = SKIP_FUND
        else:
            proxy = str(h.get("yfinance_proxy") or sym)
            if proxy not in wanted:
                wanted.append(proxy)
    return wanted, skipped


def _fetch_raw(symbol: str) -> List[Tuple[str, float]]:
    """yfinance period=max 的 [(date, 原始价)]；NaN / 非正的日子丢掉。"""
    df = history_with_raw(yf.Ticker(symbol), period="max")
    if df is None or df.empty or "CloseRaw" not in df.columns:
        return []
    out = []
    for idx, v in df["CloseRaw"].items():
        if v == v and v > 0:   # NaN != NaN
            out.append((idx.strftime("%Y-%m-%d"), float(v)))
    return out


def backfill_raw_close(symbols: Optional[Iterable[str]] = None, *, pm=None,
                       store: Optional[MarketStore] = None) -> Dict[str, Any]:
    """回填 close_raw。``symbols`` 缺省 = 持仓 + target_assets（见模块 docstring）。
    返回 {"status", "symbols": {sym: {...}}, "skipped": {sym: 原因}}；单只失败不挡其他只（status=partial）。"""
    skipped: Dict[str, str] = {}
    if symbols is None:
        if pm is None:
            from openinvest.core.portfolio_manager import PortfolioManager
            pm = PortfolioManager()
        todo, skipped = default_symbols(pm)
    else:
        todo = []
        for s in symbols:
            s = str(s).strip()
            if not s:
                continue
            if s.upper().startswith("FUND:"):
                skipped[s] = SKIP_FUND
            elif s not in todo:
                todo.append(s)
    store = store or MarketStore()
    report: Dict[str, Dict[str, Any]] = {}
    failed = 0
    for sym in todo:
        try:
            rows = _fetch_raw(sym)
            if not rows:
                report[sym] = {"status": "no_data", "hint": "yfinance 没返回数据（检查 symbol / 网络）"}
                failed += 1
                continue
            updated, missing = store.set_close_raw(sym, rows)
            df = store.get_close_pair_df(sym)
            report[sym] = {
                "status": "ok", "db_rows": int(len(df)), "updated": updated,
                "yfinance_days_not_in_db": missing,
                "null_rows": int(df["CloseRaw"].isna().sum()) if len(df) else 0,
            }
        except Exception as e:  # noqa: BLE001  一只失败不挡其他只
            log.warning("backfill_raw_close(%s) 失败: %s", sym, e)
            report[sym] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            failed += 1
    return {"status": "ok" if not failed else "partial", "symbols": report, "skipped": skipped}


__all__ = ["backfill_raw_close", "default_symbols"]
