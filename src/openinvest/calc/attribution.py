"""attribution.py — 持仓当日涨跌归因纯计算（calc 层 T0，ADR-026：禁 IO / 禁时钟）

日报"今日持仓归因"段的数字层：每个持仓"最近两根收盘 + 对应两个时点的汇率 + 持仓量"
→ 每持仓涨跌 / 汇率影响 / 对组合贡献，加组合合计与基准对比。IO 壳（取价、取汇率、
找最近两个交易日）在 ``services/holdings_digest.py``。

口径（组合 = 持仓合计，**不含现金**——要拿来和基准比的是投资部分；含现金的 NAV 口径
是"账户业绩"段的 TWR）：

- 每个持仓用**它自己**最近两根收盘（休市 / 不同交易所日历不同，不强行对齐到同一天）。
- ``V0 = units × prev_close × fx_prev``，``V1 = units × close × fx``（基准币种；fx = 持仓
  计价币种 → 基准币种，同币种 1.0）。
- ``local_return = close/prev_close − 1``（本币涨跌），``day_return = V1/V0 − 1``（基准币种涨跌）。
- ``fx_contribution = day_return − local_return``（汇率对该持仓基准币种收益的贡献，含汇率 ×
  价格交叉项，所以 ``day_return = local_return + fx_contribution`` 严格成立）。
- ``contribution = (V1 − V0) / ΣV0``（= 昨日权重 × 当日收益，单位：百分点 pp），
  各持仓 contribution 之和 = 组合日收益，严格相加。
- 基准：``close/prev_close − 1``（基准自己的价格收益，不做汇率换算），
  ``excess = 持仓 − 基准``（百分点）。

输入缺价 / 缺汇率 / 非正数 → 该持仓进 ``skipped``（带 reason），不参与权重分母。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

TOP_N = 3
_ROUND = 4  # 内部多留两位小数，展示层再 .2f（避免逐项取整后合计对不上）


def _ok(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _r(x: float) -> float:
    return round(x, _ROUND)


def _bench_return_pct(benchmark: Optional[Dict[str, Any]]) -> Optional[float]:
    if not benchmark or not (_ok(benchmark.get("prev_close")) and _ok(benchmark.get("close"))):
        return None
    return (benchmark["close"] / benchmark["prev_close"] - 1) * 100


def attribute(
    holdings: List[Dict[str, Any]],
    benchmark: Optional[Dict[str, Any]] = None,
    *,
    top_n: int = TOP_N,
) -> Dict[str, Any]:
    """持仓当日涨跌归因。

    Args:
        holdings: 每项 ``{symbol, units, prev_close, close, fx_prev, fx, ...}``；
            多余字段（name / currency / date …）原样带进输出行。
        benchmark: ``{prev_close, close, ...}`` 或 None。
        top_n: top_contributors / top_detractors 各取几个。

    Returns:
        ``{holdings, skipped, portfolio, benchmark_return_pct, excess_pct,
        top_contributors, top_detractors}``。holdings 按 contribution 降序（同值按 symbol）；
        portfolio 为 None = 没有任何可计算的持仓。
    """
    rows: List[Dict[str, Any]] = []   # (原输入, 未取整的计算字段)
    skipped: List[Dict[str, str]] = []
    for h in holdings:
        p0, p1, f0, f1, u = (h.get(k) for k in ("prev_close", "close", "fx_prev", "fx", "units"))
        reason = ("missing_price" if not (_ok(p0) and _ok(p1))
                  else "missing_fx" if not (_ok(f0) and _ok(f1))
                  else "no_units" if not _ok(u) else None)
        if reason:
            skipped.append({"symbol": str(h.get("symbol")), "reason": reason})
            continue
        v0, v1 = u * p0 * f0, u * p1 * f1
        local, day = p1 / p0 - 1, v1 / v0 - 1
        rows.append({"in": h, "v0": v0, "v1": v1, "local": local * 100, "fx_ret": (f1 / f0 - 1) * 100,
                     "day": day * 100, "fx_contrib": (day - local) * 100})

    bench = _bench_return_pct(benchmark)
    bench_out = None if bench is None else _r(bench)
    if not rows:
        return {"holdings": [], "skipped": skipped, "portfolio": None, "benchmark_return_pct": bench_out,
                "excess_pct": None, "top_contributors": [], "top_detractors": []}

    total0, total1 = sum(r["v0"] for r in rows), sum(r["v1"] for r in rows)
    port_ret = (total1 / total0 - 1) * 100
    out_rows: List[Dict[str, Any]] = []
    port_fx = 0.0
    for r in rows:
        weight = r["v0"] / total0
        port_fx += weight * r["fx_contrib"]
        out_rows.append({
            **r["in"],
            "value_prev_base": _r(r["v0"]), "value_base": _r(r["v1"]), "weight_pct": _r(weight * 100),
            "local_return_pct": _r(r["local"]), "fx_return_pct": _r(r["fx_ret"]),
            "day_return_pct": _r(r["day"]), "fx_contribution_pct": _r(r["fx_contrib"]),
            "contribution_pct": _r((r["v1"] - r["v0"]) / total0 * 100),
            "excess_vs_benchmark_pct": None if bench is None else _r(r["day"] - bench),
        })
    out_rows.sort(key=lambda r: (-r["contribution_pct"], str(r.get("symbol"))))
    return {
        "holdings": out_rows,
        "skipped": skipped,
        "portfolio": {"return_pct": _r(port_ret), "fx_contribution_pct": _r(port_fx),
                      "value_prev_base": _r(total0), "value_base": _r(total1)},
        "benchmark_return_pct": bench_out,
        "excess_pct": None if bench is None else _r(port_ret - bench),
        "top_contributors": [r["symbol"] for r in out_rows if r["contribution_pct"] > 0][:top_n],
        "top_detractors": [r["symbol"] for r in reversed(out_rows) if r["contribution_pct"] < 0][:top_n],
    }


__all__ = ["attribute", "TOP_N"]
