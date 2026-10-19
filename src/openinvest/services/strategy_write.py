"""strategy.md 写操作服务层 —— REST / CLI / MCP 三个入口的单一可信源。

issue #179 遗留项：写逻辑原 inline 在 connectors/web_api/routers/strategy_write.py
（deprecated REST 面独占），CLI/MCP 只读——违反"agent 必须拥有全部功能"哲学，
且卡住 web_api 退役。抽到这里后 REST 退化为薄壳，CLI/MCP 直调。

一致性与校验：
- 所有写走 store.transaction("strategy") 单锁 RMW（commit-on-success：校验失败
  异常退出自动 rollback，不写半截）
- 提交前跑 core.schemas.validate_strategy（StrategyData：配比 ge0 le1 且和≈1、
  target_assets ≥1 且 symbol 不重复、单资产字段范围）——字段约束的单一闸门，
  本层不重复造范围检查
- 业务错误用类型化异常：StrategyConflict（symbol 已存在）/ StrategyNotFound
  （symbol 不存在）/ ValueError（schema 或参数错）。REST 映射 409/404/400。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from openinvest.core.memory_store import MemoryStore
from openinvest.core.schemas import validate_strategy


class StrategyConflict(ValueError):
    """symbol 已在 target_assets 中（add 语义冲突）"""


class StrategyNotFound(ValueError):
    """symbol 不在 target_assets 中（patch/remove 找不到目标）"""


# TargetAssetCreate/Patch 的可写字段面（symbol 之外）。与 core.schemas.TargetAsset
# 对齐；extra="allow" 的长尾字段（currency/market/note 等）走 extra dict。
ASSET_FIELDS = (
    "display_name",
    "channel",
    "max_single_invest_cny",
    "price_offset_pct",
    "sell_fee_pct",
    "target_pct",
    "alerts",
)


def _targets(meta: Dict[str, Any]) -> tuple:
    """(股票目标, {symbol: 原始 target_pct})——写前写后各取一份，给 _validate_or_raise 比。"""
    return (
        float(meta.get("target_allocation_stock", 0) or 0),
        {str(a.get("symbol")): a["target_pct"]
         for a in meta.get("target_assets", []) or [] if a.get("target_pct") is not None},
    )


def _valid_pct(v: Any) -> Optional[float]:
    """合法的 0~1 小数 → float；其余（30 / "30%" / 负数）→ None。与 rebalance_plan._target_pct 同口径：它忽略的值这里也不计入合计。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


def _validate_or_raise(metadata: Dict[str, Any], before: Optional[tuple] = None) -> None:
    """整份 schema 校验 + target_pct 规则。

    target_pct 规则只拦「这次写入新带来 / 加重的问题」，不惩罚以前手改 strategy.md 留下的旧问题——
    否则一份写着 `target_pct: 30` 的老文件，连 untrack 一个无关标的都被拒（#457 评审第 1 条）：
    - 本次新改的 target_pct 必须是 0~1 的小数（没改的旧值不管）；
    - 各标的合计 ≤ 股票目标，但只在「超出量比写前更大」时拒绝（调高某个权重 / 降低股票目标会，
      无关写入、调低权重、删标的不会）。before=None（没有写前快照）当作一份干净文件，全量检查。
    浮点容差 1e-6（0.3+0.2+0.2 ≠ 0.7）。
    """
    try:
        validate_strategy(metadata)
    except Exception as e:  # ValidationError 或派生
        raise ValueError(f"strategy schema validation failed: {e}") from e
    cap, pcts = _targets(metadata)
    old_cap, old_pcts = before if before is not None else (cap, {})
    bad = [f"{sym}={raw!r}" for sym, raw in pcts.items() if old_pcts.get(sym) != raw and _valid_pct(raw) is None]
    if bad:
        raise ValueError(f"target_pct 必须是 0~1 的小数（占总资产，0.3 = 30%），收到 {', '.join(bad)}")

    def excess(c: float, p: Dict[str, Any]) -> float:
        return max(sum(v for v in map(_valid_pct, p.values()) if v is not None) - c, 0.0)

    if excess(cap, pcts) > excess(old_cap, old_pcts) + 1e-6:
        total = sum(v for v in map(_valid_pct, pcts.values()) if v is not None)
        detail = " + ".join(f"{s} {v:.1%}" for s, v in ((s, _valid_pct(r)) for s, r in pcts.items()) if v is not None)
        raise ValueError(
            f"各标的 target_pct 之和 {total:.1%}（{detail}）超过股票目标 target_allocation_stock {cap:.1%}；"
            "调低某些标的的 target_pct，或先用 set_allocations 提高股票目标（可同次传 target_pcts 一起改）"
        )


def _merge_alerts(asset: Dict[str, Any], spec: Optional[Dict[str, Any]]) -> None:
    """把 alerts 改动并进 asset（C8 每标的报警线）。spec 里有值的键覆盖、null 的键清掉、没提到的键不动；
    spec=None 清空该标的全部 alerts；清到空就删 alerts 键（strategy.md 不留 `alerts: {}`）。
    键名/取值合法性不在这查——整份 schema 校验（core.schemas.SymbolAlerts，extra=forbid）统一拦。"""
    if spec is None:
        asset.pop("alerts", None)
        return
    cur = dict(asset.get("alerts") or {})
    for k, v in spec.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    if cur:
        asset["alerts"] = cur
    else:
        asset.pop("alerts", None)


def _response(metadata: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "target_allocation_stock": float(metadata.get("target_allocation_stock", 0)),
        "target_allocation_cash": float(metadata.get("target_allocation_cash", 0)),
        "target_assets": list(metadata.get("target_assets", [])),
        "message": message,
    }


def _store(store: Optional[MemoryStore]) -> MemoryStore:
    return store if store is not None else MemoryStore()


def set_allocations(
    target_allocation_stock: Optional[float] = None,
    target_allocation_cash: Optional[float] = None,
    *,
    target_pcts: Optional[Dict[str, Optional[float]]] = None,
    alerts: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """改资产配置目标：stock/cash 比例（两者之和必须 ≈ 1，要传就一起传）和/或按标的的 target_pct / alerts。

    target_pcts：{symbol: 0~1 小数 | None}，symbol 必须已在 target_assets（先 track_asset），None = 清掉该标的的
    target_pct（回到平分）。和 stock/cash 同一次事务校验——降低股票目标时可以同次把各标的权重一起调低，
    分两次写的话中间态会因 Σtarget_pct > 股票目标被拒。
    alerts：{symbol: {price_above/price_below/day_drop_pct/day_rise_pct/drawdown_from_cost_pct: 值 | None} | None}，
    合并语义同 track_asset（值覆盖、null 清键、没提的键不动），整个 symbol 写 None = 清空它的全部报警线。
    """
    if (target_allocation_stock is None) != (target_allocation_cash is None):
        raise ValueError("target_allocation_stock 与 target_allocation_cash 要么都传要么都不传（两者之和必须 ≈ 1）")
    if target_allocation_stock is None and not target_pcts and not alerts:
        raise ValueError("至少传 stock + cash 一组配比，或 target_pcts（按标的权重），或 alerts（按标的报警线）")
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        if target_allocation_stock is not None:
            tx["target_allocation_stock"] = target_allocation_stock
            tx["target_allocation_cash"] = target_allocation_cash
        if target_pcts or alerts:
            assets: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])

            def _asset(sym: str, what: str) -> Dict[str, Any]:
                a = next((x for x in assets if x.get("symbol") == sym), None)
                if a is None:
                    raise StrategyNotFound(f"symbol {sym} 不在 target_assets（先 track_asset 跟踪它再设 {what}）")
                return a

            for sym, pct in (target_pcts or {}).items():
                a = _asset(sym, "target_pct")
                if pct is None:
                    a.pop("target_pct", None)
                else:
                    a["target_pct"] = pct
            for sym, spec in (alerts or {}).items():
                _merge_alerts(_asset(sym, "alerts"), spec)
            tx["target_assets"] = assets
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    parts = []
    if target_allocation_stock is not None:
        parts.append(f"股 {target_allocation_stock:.0%} / 现 {target_allocation_cash:.0%}")
    if target_pcts:
        parts.append("target_pct " + ", ".join(
            f"{k}={'清除' if v is None else format(v, '.0%')}" for k, v in target_pcts.items()))
    if alerts:
        parts.append("alerts " + ", ".join(f"{k}={'清空' if v is None else sorted(v)}" for k, v in alerts.items()))
    return _response(final_meta, "目标配置已更新: " + "；".join(parts))


def add_target_asset(
    asset: Dict[str, Any], *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """新增 target_asset（dict 至少含 symbol + max_single_invest_cny）。
    symbol 已存在 → StrategyConflict。"""
    symbol = str(asset.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("asset 必须含非空 symbol")
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if any(a.get("symbol") == symbol for a in existing):
            raise StrategyConflict(f"symbol {symbol} 已存在，请用 update 或先 remove")
        existing.append(dict(asset))
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"已新增资产 {symbol}")


def patch_target_asset(
    symbol: str, patch: Dict[str, Any], *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """更新单个 target_asset 的部分字段（仅改传入的键）。
    symbol 不存在 → StrategyNotFound；空 patch → ValueError。"""
    if not patch:
        raise ValueError("patch 必须至少含一个字段")
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            raise StrategyNotFound(f"symbol {symbol} 不存在")
        target.update(patch)
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"{symbol} 已更新: {list(patch.keys())}")


def remove_target_asset(
    symbol: str, *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """删除 target_asset。schema 要求至少剩 1 个（删最后一个会 ValueError 回滚）。
    symbol 不存在 → StrategyNotFound。"""
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if not any(a.get("symbol") == symbol for a in existing):
            raise StrategyNotFound(f"symbol {symbol} 不存在")
        tx["target_assets"] = [a for a in existing if a.get("symbol") != symbol]
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"已删除 {symbol}")


def replace_target_assets(
    assets: List[Dict[str, Any]], *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """整表替换 target_assets（一个 transaction，schema 校验不过整体回滚）。
    给 jobs/ibkr_autopilot 的每周选股用：逐个 add/remove 会在中途撞「至少剩 1 个」。"""
    if not assets:
        raise ValueError("target_assets 不能为空")
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        tx["target_assets"] = [dict(a) for a in assets]
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"target_assets → {', '.join(a['symbol'] for a in assets)}")


def upsert_target_asset(
    symbol: str,
    fields: Dict[str, Any],
    *,
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """track 语义（agent 友好的幂等入口）：symbol 已存在 → 只更新传入字段；
    不存在 → 新建（schema 要求 max_single_invest_cny；没传则沿用策略顶层的单次入场上限）。

    与 REST 的 POST(409)/PUT(404) 分离语义并存：REST 面保持原契约不动，
    CLI/MCP 的 "track AAPL" 重复调用不该报错。

    并发正确性：exists 判断和 patch/add 分支在**同一个** transaction（同一把
    fcntl 锁）内完成——拆成"先 patch 失败再 add"两次拿锁的话，两个并发 track
    同一新 symbol 会让输家吃到 StrategyConflict，幂等承诺失效（Sonnet review
    用双线程 65% 复现过）。ADR-016 同款教训：check-then-act 必须原子。
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol 不能为空")
    fields = {k: v for k, v in fields.items() if v is not None}
    alerts = fields.pop("alerts", None)  # 嵌套对象，走 _merge_alerts（合并 / null 清键），不能整块 update 覆盖
    with _store(store).transaction("strategy") as tx:
        before = _targets(tx.metadata)
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            msg = f"已新增资产 {symbol}"
            if "max_single_invest_cny" not in fields:
                # 新建必须有单笔上限（schema 必填）。没传就沿用策略顶层的单次入场上限——
                # agent 走 MCP 时本来就是这么手动补的；顶层也没有则给可读报错，
                # 不把 pydantic 原文甩给 CLI 用户（2026-10-11 实测）
                default_cap = tx.get("max_single_invest_cny")
                if default_cap is None:
                    raise ValueError(
                        f"新建跟踪 {symbol} 需要单笔上限：CLI 加 --max-single-invest-cny <CNY>，"
                        "MCP 传 max_single_invest_cny（策略里也没有顶层单次入场上限可沿用）"
                    )
                fields["max_single_invest_cny"] = default_cap
                msg += f"（单笔上限沿用策略的单次入场上限 {float(default_cap):,.0f} CNY）"
            target = {"symbol": symbol, **fields}
            existing.append(target)
            if alerts:
                _merge_alerts(target, alerts)
        elif fields or alerts:
            target.update(fields)
            if alerts:
                _merge_alerts(target, alerts)
            msg = f"{symbol} 已更新: {[*fields, *(['alerts'] if alerts else [])]}"
        else:
            msg = f"{symbol} 已在跟踪列表（无字段变更）"
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata), before)
        final_meta = dict(tx.metadata)
    return _response(final_meta, msg)


__all__ = [
    "ASSET_FIELDS",
    "StrategyConflict",
    "StrategyNotFound",
    "set_allocations",
    "add_target_asset",
    "patch_target_asset",
    "remove_target_asset",
    "upsert_target_asset",
]
