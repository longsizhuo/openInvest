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
)


def _validate_or_raise(metadata: Dict[str, Any]) -> None:
    try:
        validate_strategy(metadata)
    except Exception as e:  # ValidationError 或派生
        raise ValueError(f"strategy schema validation failed: {e}") from e


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
    target_allocation_stock: float,
    target_allocation_cash: float,
    *,
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """改资产配置目标（stock/cash 比例）。两者之和必须 ≈ 1（schema 强约束）。"""
    with _store(store).transaction("strategy") as tx:
        tx["target_allocation_stock"] = target_allocation_stock
        tx["target_allocation_cash"] = target_allocation_cash
        _validate_or_raise(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _response(
        final_meta,
        f"目标配置已更新: 股 {target_allocation_stock:.0%} / 现 {target_allocation_cash:.0%}",
    )


def add_target_asset(
    asset: Dict[str, Any], *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """新增 target_asset（dict 至少含 symbol + max_single_invest_cny）。
    symbol 已存在 → StrategyConflict。"""
    symbol = str(asset.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("asset 必须含非空 symbol")
    with _store(store).transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if any(a.get("symbol") == symbol for a in existing):
            raise StrategyConflict(f"symbol {symbol} 已存在，请用 update 或先 remove")
        existing.append(dict(asset))
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata))
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
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            raise StrategyNotFound(f"symbol {symbol} 不存在")
        target.update(patch)
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"{symbol} 已更新: {list(patch.keys())}")


def remove_target_asset(
    symbol: str, *, store: Optional[MemoryStore] = None
) -> Dict[str, Any]:
    """删除 target_asset。schema 要求至少剩 1 个（删最后一个会 ValueError 回滚）。
    symbol 不存在 → StrategyNotFound。"""
    with _store(store).transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if not any(a.get("symbol") == symbol for a in existing):
            raise StrategyNotFound(f"symbol {symbol} 不存在")
        tx["target_assets"] = [a for a in existing if a.get("symbol") != symbol]
        _validate_or_raise(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _response(final_meta, f"已删除 {symbol}")


def upsert_target_asset(
    symbol: str,
    fields: Dict[str, Any],
    *,
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """track 语义（agent 友好的幂等入口）：symbol 已存在 → 只更新传入字段；
    不存在 → 新建（此时 schema 要求 max_single_invest_cny 必填）。

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
    with _store(store).transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            existing.append({"symbol": symbol, **fields})
            msg = f"已新增资产 {symbol}"
        elif fields:
            target.update(fields)
            msg = f"{symbol} 已更新: {list(fields.keys())}"
        else:
            msg = f"{symbol} 已在跟踪列表（无字段变更）"
        tx["target_assets"] = existing
        _validate_or_raise(dict(tx.metadata))
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
