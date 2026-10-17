"""再平衡清单（纯计算，T0）：目标配置 + 现状 → 每个标的该买 / 卖多少。

只出清单，不下单——人执行。strategy.md 的 target_pct / target_allocation_* 此前只存不用，
这里把它变成可执行的数字。服务层（services/rebalance_plan.py）负责取持仓 / 现价 / 汇率，
本模块只吃算好的数。

口径：
- 占比 = 市值 / 总资产（基准币种，含现金）；漂移 = 当前 − 目标，单位 pp（百分点）。
- 带宽内（|漂移| ≤ band_pct）不动；超出带宽的回到**目标**（不是回到带边）。
- 三种模式（参照 Wealthfolio allocation targets）：
    cash_only         只用「高于目标现金比例的那部分现金」买低配，从不卖
    hybrid            先用上述现金买低配，不够才卖超配，只卖补足缺口所需的那部分
    sell_to_rebalance 超配一律卖回目标，卖出款 + 多余现金买低配
- 资金不足：按各低配标的的目标权重分配，单个不超过其缺口，封顶释放的额度再分给其余。
- 整手：买入 / 卖出一律向下取整到一手；不足一手记 NONE 并写原因。

ponytail: 取整后的零头不做二次分配（一手以内的钱留在现金里）。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

MODES = ("cash_only", "sell_to_rebalance", "hybrid")

_A_SHARE_SUFFIX = (".SS", ".SZ", ".BJ")
_STAR_BOARD = re.compile(r"^68\d{4}\.SS$")                       # 科创板
_CONVERTIBLE = re.compile(r"^(?:11\d{4}\.SS|12\d{4}\.SZ)$")      # 沪 11xxxx / 深 12xxxx 可转债
_EPS = 1e-9
_RANK = {"SELL": 0, "BUY": 1, "NONE": 2}


def infer_lot_size(symbol: str, kind: str = "", by_amount: bool = False) -> Tuple[Optional[int], str]:
    """(整手股数, 备注)。None = 按金额买卖，无整手（场外基金 / 加密 / 贵金属可小数）。

    A 股 100 股；可转债 10 张；港股每手因股而异（无数据 → 按 1 并注明）；其余 1。
    科创板实际 200 股起、之后 1 股递增，这里不细分，按 100 取整（偏保守）并注明。
    """
    s, k = (symbol or "").upper(), (kind or "").lower()
    if by_amount or k in ("crypto", "metal"):
        return None, ""
    if s.endswith(_A_SHARE_SUFFIX) or _CONVERTIBLE.match(s):
        if k == "bond" or _CONVERTIBLE.match(s):
            return 10, "可转债 10 张起，按 10 张取整"
        if _STAR_BOARD.match(s):
            return 100, "科创板实际 200 股起、之后 1 股递增，此处按 100 股取整（偏保守）"
        return 100, ""
    if s.endswith(".HK"):
        return 1, "手数未知：港股每手股数因股而异，按 1 股计；下单前按券商显示的每手股数向下取整"
    return 1, ""


def _round_down(amount: float, unit: float, lot: Optional[int]) -> Tuple[float, float]:
    """金额（基准币种）→ (向下取整后的数量, 实际金额)。lot=None 按整数金额取整。"""
    if lot is None:
        whole = math.floor(amount + _EPS)
        return round(whole / unit, 4), float(whole)
    units = math.floor(amount / (unit * lot) + _EPS) * lot
    return units, units * unit


def _fill(need: Dict[str, float], weight: Dict[str, float], budget: float) -> Dict[str, float]:
    """把 budget 按 weight 分给 need 里的标的，单个不超过其缺口；封顶的释放额度再分给其余。"""
    out: Dict[str, float] = {}
    left = dict(need)
    while left and budget > _EPS:
        wsum = sum(weight[s] for s in left)
        share = {s: budget * weight[s] / wsum for s in left}
        capped = [s for s in left if share[s] >= left[s]]
        if not capped:
            out.update(share)
            break
        for s in capped:
            out[s] = left.pop(s)
            budget -= out[s]
    return out


def _resolve_targets(positions: List[Dict[str, Any]], stock_pct: float) -> Tuple[Dict[str, float], Dict[str, str]]:
    """显式 target_pct 照用；缺的把「股票目标 − 已显式分配」平分，并标 equal_split。"""
    explicit = {p["symbol"]: float(p["target_pct"]) for p in positions if p.get("target_pct") is not None}
    missing = [p["symbol"] for p in positions if p["symbol"] not in explicit]
    each = max(stock_pct - sum(explicit.values()), 0.0) / len(missing) if missing else 0.0
    tgt = {p["symbol"]: explicit.get(p["symbol"], each) for p in positions}
    src = {p["symbol"]: "strategy" if p["symbol"] in explicit else "equal_split" for p in positions}
    return tgt, src


def plan_rebalance(
    positions: List[Dict[str, Any]],
    *,
    cash: float,
    total: float,
    target_stock_pct: float,
    target_cash_pct: float,
    band_pct: float = 5.0,
    mode: str = "hybrid",
    currency: str = "CNY",
) -> Dict[str, Any]:
    """算再平衡清单。金额一律基准币种，占比 / 目标 / 权重输入为小数（0.3 = 30%），输出为 pp。

    positions 每项：
        symbol, value（基准币种市值，未持有 0）, target_pct（小数，None = 缺省）,
        price（报价币种现价，None = 拉不到）, fx（报价币种 → 基准币种汇率，None = 拉不到）,
        lot_size（整手，None = 按金额）
    返回 {"rows": [...], "totals": {...}, "warnings": [...]}；rows 按 SELL → BUY → NONE 排序。
    """
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
    if not total > 0:
        raise ValueError("总资产必须 > 0")
    band = band_pct / 100.0
    fmt = lambda x: f"{x:,.0f} {currency}"  # noqa: E731

    tgt, src = _resolve_targets(positions, target_stock_pct)
    info: Dict[str, Dict[str, Any]] = {}
    over: Dict[str, float] = {}     # 超配额（基准币种）
    under: Dict[str, float] = {}    # 缺口（基准币种）
    for p in positions:
        s = p["symbol"]
        cur = p["value"] / total
        drift = cur - tgt[s]
        unit = (p.get("price") or 0.0) * (p.get("fx") or 0.0)   # 1 单位的基准币种价
        row = {"symbol": s, "current_pct": cur * 100, "target_pct": tgt[s] * 100, "drift_pct": drift * 100,
               "action": "NONE", "amount": 0.0, "units": 0, "lot_size": p.get("lot_size"),
               "price": p.get("price"), "target_source": src[s], "reason": ""}
        if abs(drift) <= band + _EPS:
            row["reason"] = f"漂移 {drift * 100:+.1f}pp 在 ±{band_pct:g}pp 带宽内，不动"
        elif not unit > 0:
            row["reason"] = f"漂移 {drift * 100:+.1f}pp 已超带宽，但拉不到现价或汇率，无法换算数量"
        elif drift > 0:
            over[s] = drift * total
        else:
            under[s] = -drift * total
        info[s] = {"row": row, "unit": unit, "lot": p.get("lot_size"), "value": p["value"]}

    avail = max(cash - target_cash_pct * total, 0.0)        # 高于目标现金比例的那部分现金
    need_total, excess_total = sum(under.values()), sum(over.values())
    if mode == "cash_only":
        k = 0.0
    elif mode == "sell_to_rebalance":
        k = 1.0
    else:   # hybrid：只卖补足「买入缺口 − 可用现金」所需的那部分
        deficit = need_total - avail
        k = min(1.0, deficit / excess_total) if deficit > 1e-6 and excess_total else 0.0   # 1e-6：浮点噪声不算缺口

    proceeds = 0.0
    for s, ex in over.items():
        i, row = info[s], info[s]["row"]
        d = f"超配 {row['drift_pct']:+.1f}pp（目标 {row['target_pct']:.1f}%）"
        if k <= 0:
            row["reason"] = d + ("，cash_only 模式不卖出" if mode == "cash_only"
                                 else "，hybrid 模式下现金已够买入或无需买入，不卖")
            continue
        units, amt = _round_down(min(ex * k, i["value"]), i["unit"], i["lot"])
        if units <= 0:
            row["reason"] = d + f"，但取整后不足一手（拟卖 {fmt(ex * k)}，一手约 {fmt(i['unit'] * (i['lot'] or 1))}）"
            continue
        row.update(action="SELL", units=units, amount=amt,
                   reason=d + ("，卖出回到目标" if k >= 1 - _EPS
                               else f"，卖出 {k:.0%} 的超配部分以补足买入资金"))
        proceeds += amt

    budget = avail if mode == "cash_only" else avail + proceeds
    weight = {s: tgt[s] for s in under}
    alloc = _fill(under, weight, budget)
    spent = 0.0
    for s, need in under.items():
        i, row = info[s], info[s]["row"]
        d = f"低配 {row['drift_pct']:+.1f}pp（目标 {row['target_pct']:.1f}%）"
        got = alloc.get(s, 0.0)
        if got <= _EPS:
            row["reason"] = d + f"，但可用资金为 0（现金已不高于目标现金比例 {target_cash_pct:.0%}" + \
                ("，该模式不卖出）" if mode == "cash_only" else "，且无可卖的超配）")
            continue
        units, amt = _round_down(got, i["unit"], i["lot"])
        if units <= 0:
            row["reason"] = d + f"，但取整后不足一手（拟买 {fmt(got)}，一手约 {fmt(i['unit'] * (i['lot'] or 1))}）"
            continue
        row.update(action="BUY", units=units, amount=amt,
                   reason=d + ("，买入回到目标" if got >= need - 1e-6
                               else f"，资金不足（缺口 {fmt(need)}，分到 {fmt(got)}），按目标权重分配"))
        spent += amt

    rows = []
    for i in info.values():
        r = i["row"]
        for f in ("current_pct", "target_pct", "drift_pct"):
            r[f] = round(r[f], 2)
        r["amount"] = round(r["amount"], 2)
        rows.append(r)
    rows.sort(key=lambda r: _RANK[r["action"]])

    cash_after = cash + proceeds - spent
    warnings: List[str] = []
    tsum = sum(tgt.values()) + target_cash_pct
    if abs(tsum - 1.0) > 0.02:
        warnings.append(f"目标合计 {tsum:.0%}（标的 {sum(tgt.values()):.0%} + 现金 {target_cash_pct:.0%}）≠ 100%，"
                        "请检查 strategy.md 的 target_pct / target_allocation_*")
    cash_gap = (cash_after / total - target_cash_pct) * 100
    if abs(cash_gap) > band_pct + _EPS:
        warnings.append(f"计划后现金 {cash_after / total:.1%}，与目标 {target_cash_pct:.0%} 相差 {cash_gap:+.1f}pp，超出带宽"
                        "（现金只当资金来源、不单独再平衡；想连现金一起拉回目标用 sell_to_rebalance）")
    n_eq = sum(1 for v in src.values() if v == "equal_split")
    if n_eq:
        warnings.append(f"{n_eq} 个标的没写 target_pct，已按股票目标 {target_stock_pct:.0%} 平分（target_source=equal_split）；"
                        "这是估的目标，先在 strategy.md 给每个标的写 target_pct 再当真")
    return {
        "rows": rows,
        "totals": {
            "cash_before": round(cash, 2), "cash_after": round(cash_after, 2),
            "cash_after_pct": round(cash_after / total * 100, 2), "target_cash_pct": round(target_cash_pct * 100, 2),
            "buy_total": round(spent, 2), "sell_total": round(proceeds, 2),
            "turnover": round(spent + proceeds, 2), "turnover_pct": round((spent + proceeds) / total * 100, 2),
            "n_trades": sum(1 for r in rows if r["action"] != "NONE"),
        },
        "warnings": warnings,
    }


# 完整历史导出面（calc 纪律：façade `import *` 的完备性依赖本列表）
__all__ = ["MODES", "infer_lot_size", "plan_rebalance"]
