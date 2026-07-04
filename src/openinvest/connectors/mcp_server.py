"""MCP stdio adapter（issue #133 Phase 3）—— 薄包装层，零业务逻辑。

Claude Code（或任意 MCP client）按 session spawn 本进程，stdin/stdout 说
JSON-RPC，无端口无 daemon。写安全与 CLI / web API 并存同一模型
（with_portfolio_tx fcntl 锁）。

工具刻意克制在 ~15 个高频能力（81 个 REST 端点全暴露会撑爆 agent context），
全部复用 service 层 / PortfolioManager / decision_ledger——与 CLI、REST 同源，
防三 adapter 漂移。委员会 Coordinator workflow 不在此处（Decision 5：那是
Skill 的职责，MCP 只暴露 Direct 路径 run_committee）。

注册（本地开发）：
    claude mcp add openinvest -- uv --directory <repo> run python -m connectors.mcp_server
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# client（Claude Code 等）靠这些 hint 决定要不要弹确认：
# 读工具零确认放行；动钱工具必须过 destructiveHint 闸——status 和 sell 不能同级
_RO = ToolAnnotations(readOnlyHint=True)
_MONEY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

mcp = FastMCP(
    "openinvest",
    instructions=(
        "openInvest —— 面向 agent 的投资决策 runtime。持仓/行情/决议账本读写 + "
        "4 角色 LLM 投资委员会（Direct 路径）。金额币种默认 CNY。"
        "用户拒绝委员会建议时，问一句原因再 record_execution——原因闭环的采集端在宿主 agent。"
    ),
)


def _pm():
    from openinvest.core.portfolio_manager import PortfolioManager
    return PortfolioManager()


# ---------- 只读 ----------

@mcp.tool(annotations=_RO)
def status() -> Dict[str, Any]:
    """当前持仓全景：现金（各币种）+ holdings + 实时价 + P&L。"""
    from openinvest.services.skill_views import build_status_view
    return build_status_view()


@mcp.tool(annotations=_RO)
def strategy() -> Dict[str, Any]:
    """投资策略：target_assets + Dreaming 长期洞察。"""
    from openinvest.services.skill_views import build_strategy_view
    return build_strategy_view()


@mcp.tool(annotations=_RO)
def history(n: int = 10) -> Dict[str, Any]:
    """最近 n 笔交易流水 + 委员会决议记录。"""
    from openinvest.services.skill_views import build_history_view
    return build_history_view(n)


@mcp.tool(annotations=_RO)
def live_prices() -> Dict[str, Any]:
    """背景行情一次拉齐：金价（USD/oz + CNY/克）/ USDCNY / AUDCNY / NDQ.AX / VIX / TNX。"""
    from datetime import datetime
    from openinvest.services.skill_views import _safe_close
    from openinvest.utils.gold_price import get_gold_snapshot
    snap = get_gold_snapshot(offset_pct=0.0)
    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "GC_F_usd_per_oz": snap.gold_usd_per_oz if snap else None,
        "gold_cny_per_gram_spot": round(snap.spot_cny_per_gram, 2) if snap else None,
        "USDCNY": snap.usdcny_rate if snap else None,
        "AUDCNY": _safe_close("AUDCNY=X"),
        "NDQ_AX": _safe_close("NDQ.AX"),
        "VIX": _safe_close("^VIX"),
        "TNX": _safe_close("^TNX"),
    }


@mcp.tool(annotations=_RO)
def what_if(symbol: str, pct: Optional[float] = None,
            price: Optional[float] = None) -> Dict[str, Any]:
    """P&L 情景模拟："symbol 涨跌 pct% / 到 price 我的组合怎样"。纯算术零 LLM。"""
    from openinvest.services.skill_views import build_what_if_view
    return build_what_if_view(symbol=symbol, pct=pct, price=price)


@mcp.tool(annotations=_RO)
def discipline() -> Dict[str, Any]:
    """委员会纪律台账：不作为率（HOLD 占比）+ 拦截冲动操作次数 + 反事实省/费钱（ADR-023）。"""
    from openinvest.services.discipline import discipline_summary, render_discipline_md
    s = discipline_summary()
    return {"summary": s, "markdown": render_discipline_md(s)}


@mcp.tool(annotations=_RO)
def decisions(days: int = 90) -> Dict[str, Any]:
    """统一决策视图：每条委员会决议 join 规则干预/用户执行/事后结果 + 采纳率汇总。
    回答"我听了几次建议""哪些建议我没执行""被规则改写过什么"。"""
    from openinvest.core.decision_ledger import list_decisions, summarize_decisions
    ds = list_decisions(days=days)
    return {"count": len(ds), "summary": summarize_decisions(ds), "decisions": ds}


@mcp.tool(annotations=_RO)
def explain_decision(decision_id: str) -> Dict[str, Any]:
    """某条决议的完整依据：委员会 transcript（4 角色辩论 + CIO memo）+ 路径预测快照。
    decision_id 形如 "2026-07-03/GC=F"（decisions 输出里的 decision_id）。"""
    import json
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.core.memory_store import MemoryStore
    if "/" not in decision_id:
        return {"status": "error",
                "error": f'decision_id 应为 "<date>/<symbol>"，收到 {decision_id!r}'}
    date, symbol = decision_id.split("/", 1)
    # date 段必须是日期字面量——否则 "../.." 之类会拼进路径逃出 .committee/
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"status": "error", "error": f"decision_id 日期段非法: {date!r}"}
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)
    base = MemoryStore().root / ".committee" / date
    md = base / f"{safe}.md"
    parsed = parse_committee_file(md)
    if not parsed:
        return {"status": "error",
                "error": f"未找到决议 {decision_id}（{md} 不存在或无 verdict）"}
    path_json = base / f"{safe}_path.json"
    path_snapshot = None
    if path_json.exists():
        try:
            path_snapshot = json.loads(path_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "decision_id": decision_id,
        **{k: parsed[k] for k in ("verdict", "confidence", "alloc_cny")},
        "transcript_markdown": md.read_text(encoding="utf-8"),
        "path_snapshot": path_snapshot,
    }


# ---------- 决策账本写 ----------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def record_execution(decision_id: str, executed: bool,
                     reason: Optional[str] = None,
                     trade_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """记录用户对某决议的执行/拒绝 + 原因（幂等追加账本）。
    用户说"我没买/我买了/我不同意"时调用；拒绝时先问一句原因。"""
    from openinvest.core.decision_ledger import record_execution as _rec
    try:
        return _rec(decision_id, executed, reason=reason, trade_ids=trade_ids)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


# ---------- 持仓写（与 CLI / REST 共享 PortfolioManager，fcntl 锁保证一致） ----------

@mcp.tool(annotations=_MONEY)
def buy(symbol: str, units: float, price: float, currency: str = "CNY",
        kind: str = "equity", unit_label: str = "股") -> Dict[str, Any]:
    """加仓/建仓：已有 symbol 加权平均成本，新 symbol 直接建仓。price 与 currency 同币种。"""
    try:
        return _pm().buy(symbol=symbol, units=units, price=price, currency=currency,
                         kind=kind, unit_label=unit_label, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def sell(symbol: str, units: float, price: float) -> Dict[str, Any]:
    """减仓：units 减少、cost_avg 不变，按 holding 的 cost_currency 还现金。"""
    if units <= 0 or price <= 0:
        return {"status": "error", "error": "units / price 必须 > 0"}
    try:
        return _pm().sell(symbol=symbol, units=units, price=price, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def deposit(currency: str, amount: float) -> Dict[str, Any]:
    """存入现金（任意币种）。"""
    try:
        return _pm().deposit_cash(currency, amount, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def withdraw(currency: str, amount: float) -> Dict[str, Any]:
    """取出现金（任意币种），余额不足报错。"""
    if amount <= 0:
        return {"status": "error", "error": "amount 必须 > 0"}
    try:
        return _pm().withdraw_cash(currency, amount, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


# ---------- 委员会（Direct 路径） ----------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def run_committee(symbol: str, force: bool = False,
                  max_rounds: int = 1) -> Dict[str, Any]:
    """跑 4 角色 LLM 投资委员会（Direct 路径，需 DEEPSEEK_API_KEY，30-90s）。
    返回 verdict + confidence + CIO memo。当天已跑过默认读缓存，force=True 重跑。"""
    import json
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.core.memory_store import MemoryStore
    from datetime import datetime

    if not force:
        today = datetime.now().strftime("%Y-%m-%d")
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)
        cached = MemoryStore().root / ".committee" / today / f"{safe}.md"
        parsed = parse_committee_file(cached)
        if parsed:
            return {"cached": True, "decision_id": f"{today}/{symbol}",
                    **{k: parsed[k] for k in ("verdict", "confidence", "alloc_cny")}}

    from openinvest.core.committee_runner import run_committee_session
    out = run_committee_session(symbols=[symbol], max_debate_rounds=max_rounds)
    res = (out.get("asset_committees") or {}).get(symbol) or {}
    v = res.get("verdict") if isinstance(res, dict) else None
    return {
        "cached": False,
        "decision_id": f"{datetime.now().strftime('%Y-%m-%d')}/{symbol}",
        "verdict": json.loads(json.dumps(v if v is not None else res, default=str)),
    }


def main() -> None:
    mcp.run()  # stdio transport（默认）


if __name__ == "__main__":
    main()
