"""trades 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Query

from core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol

from connectors.web_api.models import RecordTradeRequest, TradeRecord, TradesListResponse

log = logging.getLogger("web_api")
router = APIRouter()


# ============================================================
# 一键记账（Trades）
# ============================================================
# 设计：
# - 用独立 db/trades.db（WAL），与 market_data.db 和 memory/ 完全隔离
# - 不动 portfolio.md / fcntl.flock / holdings
# - status 状态机：planned → executed → cancelled


from db.trades_db import TradesDB as _TradesDB

# 模块级单例 — 延迟初始化，避免 DB 文件损坏时 import 崩溃导致 crash-loop
# （模块级 _TradesDB() 在 import 时执行，DB 损坏 → import 失败 → 服务器无法启动 → 重启死循环）
_trades_db: Optional[_TradesDB] = None


def _get_trades_db() -> _TradesDB:
    """延迟初始化 TradesDB 单例，首次调用时创建连接。"""
    global _trades_db
    if _trades_db is None:
        _trades_db = _TradesDB()
    return _trades_db


@router.post("/api/trades/record", tags=["trades"])
async def record_trade(body: RecordTradeRequest = Body(...)) -> Dict[str, Any]:
    """记录一笔计划交易到本地账本（不连真实支付渠道）

    写入 db/trades.db，返回 {id, ok: true}。
    status 初始为 planned；跑完后用 PATCH /api/trades/{id}/status 改成 executed。
    """
    try:
        new_id = _get_trades_db().record_trade(
            symbol=body.symbol,
            direction=body.direction,
            units=body.units,
            price=body.price,
            cost_currency=body.cost_currency.upper(),
            verdict_id=body.verdict_id,
            note=body.note,
            intended_date=body.intended_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": new_id, "ok": True}


@router.get("/api/trades", response_model=TradesListResponse, tags=["trades"])
async def list_trades(limit: int = Query(20, ge=1, le=500,
                                         description="最近 N 笔，最多 500")) -> TradesListResponse:
    """按时间倒序返回最近 N 笔账本记录"""
    rows = _get_trades_db().list_trades(limit=limit)
    trades = [TradeRecord(**r) for r in rows]
    return TradesListResponse(count=len(trades), trades=trades)


def _sync_trade_to_portfolio(trade: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """把一笔 executed trade 同步写入 portfolio.md。

    仅在 status 改为 executed 时调用。用 PortfolioManager.with_portfolio_tx()
    保证 fcntl 锁安全（单进程内多线程共享同一把锁）。

    Returns:
        (synced: bool, holding_snapshot: dict | None)
        - synced=False 表示跳过同步（price 缺失/units=0 等边缘情况）
        - holding_snapshot 是同步后该 symbol 的 holding dict（供前端 toast）
    """
    symbol = trade.get("symbol", "")
    direction = str(trade.get("direction", "")).upper()
    units = float(trade.get("units") or 0)
    price = trade.get("price")  # 可能为 None（市价单）
    cost_currency = str(trade.get("cost_currency") or "CNY").upper()

    if not symbol or units <= 0:
        # 数据不完整，静默跳过，不阻断主流程
        log.warning(f"_sync_trade_to_portfolio: 数据不完整，跳过 trade={trade}")
        return False, None

    try:
        pm = PortfolioManager()
    except Exception as e:
        # portfolio.md 不存在时（单测环境等）不崩溃
        log.warning(f"_sync_trade_to_portfolio: PortfolioManager 初始化失败，跳过 — {e}")
        return False, None

    synced_holding: Optional[Dict[str, Any]] = None

    try:
        with pm.with_portfolio_tx() as p:
            holdings = list(p.get("holdings") or [])
            cash = dict(p.get("cash") or {})  # 同步扣/加 cash 用

            # 找现有 holding（symbol 大小写精确匹配，与 portfolio.md 保持一致）
            target = next((h for h in holdings if h.get("symbol") == symbol), None)

            # 金融视角：BUY 应同时扣 cash[cost_currency]，SELL 加 cash —— 之前
            # 只动 holdings 不动 cash 会让账本失衡（"凭空多了股票，cash 没动"）。
            # price=None（市价单）→ 用户后续手动改成交价时不会触发 sync，所以
            # 这种情况只动 units 不动 cash（一致性靠用户自己保证）
            cash_delta_currency = cost_currency
            cash_delta_amount = (price * units) if price is not None else 0.0

            if direction == "BUY":
                # ---- BUY：upsert holding，重新算加权均价 ----
                cur_units = float(target.get("units") or 0) if target else 0.0
                cur_avg = float(target.get("avg_cost") or 0) if target else 0.0

                new_units = cur_units + units

                if price is not None and new_units > 0:
                    # 加权均价：(旧均价 × 旧持仓 + 本次单价 × 本次数量) / 新持仓
                    new_avg = round(
                        (cur_avg * cur_units + price * units) / new_units, 4
                    )
                else:
                    # price=None（市价单）：仅更新 units，avg_cost 保持不变
                    new_avg = cur_avg

                if target is not None:
                    # 更新现有 holding（in-place 修改 list 里的 dict）
                    target["units"] = new_units
                    if price is not None:
                        target["avg_cost"] = new_avg
                    # 兜底：旧 holding 若缺 kind 字段（v1 迁移残留），补上启发式推断值
                    if not target.get("kind"):
                        target["kind"] = _guess_kind_from_symbol(symbol)
                else:
                    # 新建 holding：补全 schema 必填字段（kind/cost_currency）
                    # kind 用启发式规则猜测（与 record_external_trade 保持一致）
                    new_holding: Dict[str, Any] = {
                        "symbol": symbol,
                        "kind": _guess_kind_from_symbol(symbol),
                        "units": new_units,
                        "cost_currency": cost_currency,
                        "avg_cost": round(new_avg, 4) if price is not None else 0.0,
                    }
                    holdings.append(new_holding)
                    target = new_holding

                # BUY 同步扣 cash[cost_currency] —— 但**允许扣到负数**（不报错）
                # 现实场景：用户记账时 cash 可能还没补上工资入账，强制限制反而误伤。
                # 透支由 daily_report / Risk Officer 后续告警，这里只做账本一致性。
                if cash_delta_amount > 0:
                    prev_cash = float(cash.get(cash_delta_currency, 0) or 0)
                    cash[cash_delta_currency] = round(prev_cash - cash_delta_amount, 2)

                synced_holding = dict(target)

            elif direction == "SELL":
                # ---- SELL：减 units，归零则 remove ----
                if target is None:
                    # portfolio.md 里没有这个 symbol，无法减仓，跳过
                    log.warning(
                        f"_sync_trade_to_portfolio: SELL {symbol} 但 portfolio 中无持仓，跳过"
                    )
                    # 抛出一个标记异常，让 with 块回滚（不写 portfolio.md）
                    raise _SkipSync("no holding to sell")

                cur_units = float(target.get("units") or 0)
                new_units = max(0.0, cur_units - units)

                if new_units == 0:
                    # 全部卖出 → remove holding
                    holdings[:] = [h for h in holdings if h.get("symbol") != symbol]
                    synced_holding = {"symbol": symbol, "units": 0.0, "removed": True}
                else:
                    # 部分卖出：avg_cost 不变（卖出不影响成本基础）
                    target["units"] = new_units
                    # 兜底：旧 holding 若缺 kind 字段（v1 迁移残留），补上启发式推断值
                    if not target.get("kind"):
                        target["kind"] = _guess_kind_from_symbol(symbol)
                    synced_holding = dict(target)

                # SELL 同步加 cash[cost_currency] —— 不扣手续费（这一层简化处理；
                # 真实手续费 = sell_fee_pct × 总额，由 holding.sell_fee_pct 决定。
                # 后续可加，本轮先做最小一致性闭环）
                if cash_delta_amount > 0:
                    prev_cash = float(cash.get(cash_delta_currency, 0) or 0)
                    cash[cash_delta_currency] = round(prev_cash + cash_delta_amount, 2)

            p["holdings"] = holdings
            p["cash"] = cash

            # 把 cash delta 信息塞进 synced_holding 给前端 toast 用
            if synced_holding is not None and cash_delta_amount > 0:
                sign = "-" if direction == "BUY" else "+"
                synced_holding["_cash_delta"] = (
                    f"{sign}{cash_delta_amount:,.2f} {cash_delta_currency}"
                )

        # with_portfolio_tx 退出即已落盘（fsync + atomic rename），此处已经成功。
        # _reload() 只是刷新 pm 的内存视图（保持和 record_external_trade 一致），
        # 失败也不代表 portfolio.md 没写成功——绝不能让它把 True 变回 False，
        # 否则调用方会误判"未同步"而回退状态 + 允许重试，导致同一笔 delta 在
        # 已经落盘一次之后再被重放一次（双花）。
        try:
            pm._reload()
        except Exception as e:
            log.warning(f"_sync_trade_to_portfolio: portfolio.md 已落盘但 _reload 失败（不影响结果）: {e}")
        return True, synced_holding

    except _SkipSync:
        # SELL 但无持仓：不同步，但也不报错（业务上允许"账本有、持仓无"）
        return False, None
    except Exception as e:
        # with_portfolio_tx 内部（落盘前）异常 → portfolio.md 确实未变动，安全降级
        log.error(f"_sync_trade_to_portfolio 异常，portfolio.md 未变动: {e}", exc_info=True)
        return False, None


class _SkipSync(Exception):
    """内部标记异常：让 with_portfolio_tx 回滚但不对外报错"""


@router.patch("/api/trades/{trade_id}/status", tags=["trades"])
async def patch_trade_status(
    trade_id: int,
    status: str = Body(..., embed=True,
                       description="新状态：planned / executed / cancelled"),
) -> Dict[str, Any]:
    """修改账本记录状态（planned → executed → cancelled）

    当 status 改为 executed 时，自动同步更新 portfolio.md 持仓：
    - BUY: upsert holding，重新算加权均价（avg_cost = 加权平均）
    - SELL: holding.units -= trade.units；归零则 remove

    响应额外携带 portfolio_synced 和 synced_holding 让前端展示 toast。

    幂等保证靠**状态守卫**，不靠 _sync_trade_to_portfolio 本身——后者是累加的
    （BUY: cur_units + units、cash -= amount），重复调用会重复入账。所以本端点用
    trade_before["status"] 做转移判定：仅在非 executed → executed 的真实跃迁才同步；
    对一笔已经 executed 的单子再次 PATCH executed（双击 / 网络超时重试 / agent 重发）
    直接幂等返回，绝不二次同步（CLAUDE.md 红线 #4：账本一致性）。

    原子性保证：先同步 portfolio（可重试），成功后再提交 trades.db status。
    同步失败时 trade 保持原状态，重试仍是非 executed → 会重新同步，不会丢账。
    """
    # 先取 trade 原始数据（patch 前），供后面同步用
    trade_before = _get_trades_db().get_trade(trade_id)
    if trade_before is None:
        raise HTTPException(status_code=404, detail=f"trade id={trade_id} 不存在")

    # ---- 非 executed（planned / cancelled）：无 portfolio 副作用，直接改状态 ----
    if status != "executed":
        try:
            patched = _get_trades_db().patch_status(trade_id, status)
            if not patched:
                raise HTTPException(status_code=404, detail=f"trade_id={trade_id} 不存在")
        except ValueError as e:
            raise HTTPException(status_code=500, detail=f"trades.db 更新失败: {e}") from e
        return {
            "id": trade_id,
            "status": status,
            "ok": True,
            "portfolio_synced": False,
            "synced_holding": None,
        }

    # ---- executed：原子 claim 跃迁，只有赢家同步 portfolio（防 #109 并发重复入账）----
    # 旧实现先 get_trade 读状态再 patch，check/set 之间无锁，两个并发 PATCH 都读到
    # planned 各同步一次（实测 units 10→20、AUD cash 3700→2400）。改为单条 SQL 原子
    # CAS：仅 rowcount==1 的请求赢得 planned→executed 跃迁并独占同步；其余幂等返回。
    # 同样覆盖顺序重放（双击 / 客户端超时重试 / agent 重发）。
    won = await asyncio.to_thread(
        _get_trades_db().claim_status_transition, trade_id, "executed"
    )
    if not won:
        # 已是 executed（并发赢家已抢到 / 重放）→ 首次已同步，本次幂等跳过
        return {
            "id": trade_id,
            "status": "executed",
            "ok": True,
            "portfolio_synced": False,
            "synced_holding": None,
        }

    # 赢得跃迁 → 同步 portfolio.md。失败则把状态回退到原值（释放 claim），让重试
    # 能重新 claim+同步，不丢账。trade_before 是 claim 前读的原始行，含同步所需的
    # direction/units/price/symbol（不依赖 status 字段）。
    portfolio_synced, synced_holding = await asyncio.to_thread(
        _sync_trade_to_portfolio, trade_before
    )
    if not portfolio_synced:
        # 用 release_claim 而非无条件 patch_status 回退：如果这段时间里有另一个
        # 并发请求把这笔单子改成了别的状态（如 cancelled），release_claim 检测到
        # 行已不是本请求刚 claim 到的 "executed" 就不写，避免无条件 UPDATE 把
        # 别人的合法状态改动静默覆盖回去。
        released = await asyncio.to_thread(
            _get_trades_db().release_claim,
            trade_id, "executed", trade_before.get("status", "planned"),
        )
        if not released:
            log.error(
                f"trade_id={trade_id} portfolio 同步失败且回退被并发改动抢占——"
                f"状态未回退，需人工核对 trades.db 与 portfolio.md 是否一致"
            )
        raise HTTPException(
            status_code=500,
            detail=f"portfolio 同步失败，trade 已回退至 {trade_before.get('status')} 状态。请重试。"
                   f"trade_id={trade_id}, symbol={trade_before.get('symbol')}",
        )
    log.info(
        f"portfolio.md 已同步: trade_id={trade_id} "
        f"{trade_before.get('direction')} {trade_before.get('units')} "
        f"{trade_before.get('symbol')}"
    )

    return {
        "id": trade_id,
        "status": status,
        "ok": True,
        "portfolio_synced": portfolio_synced,
        "synced_holding": synced_holding,
    }
