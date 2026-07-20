"""MCP adapter（issue #133 Phase 3）—— 薄包装层，零业务逻辑。

两种 transport：
- **stdio（默认）**：Claude Code（或任意 MCP client）按 session spawn 本进程，
  stdin/stdout 说 JSON-RPC，无端口无 daemon。
- **streamable-HTTP（`--http`，BETA）**：remote MCP——hub 上常驻，spoke 机器的 agent
  直连 `http://hub:8766/mcp`，替代旧的"CLI → REST 转发"（INVEST_API_BASE）路径。
  鉴权复用 INVEST_API_TOKEN（与 web_api 同一 bearer 语义），/health 豁免探活。

写安全与 CLI / web API 并存同一模型（with_portfolio_tx fcntl 锁）。

工具刻意克制在高频能力（现 18 个）（80+ REST 端点全暴露会撑爆 agent context），
全部复用 service 层 / PortfolioManager / decision_ledger——与 CLI、REST 同源，
防三 adapter 漂移。委员会 Coordinator workflow 不在此处（Decision 5：那是
Skill 的职责，MCP 只暴露 Direct 路径 run_committee）。

注册（本地开发）：
    claude mcp add openinvest -- uv --directory <repo> run python -m connectors.mcp_server
注册（remote spoke → hub）：
    claude mcp add --transport http openinvest https://<hub>/mcp \\
        --header "Authorization: Bearer $INVEST_API_TOKEN"
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from openinvest.utils.advisory import is_advisory_mode
from openinvest.utils.symbols import safe_symbol

# client（Claude Code 等）靠这些 hint 决定要不要弹确认：
# 读工具零确认放行；动钱工具必须过 destructiveHint 闸——status 和 sell 不能同级
_RO = ToolAnnotations(readOnlyHint=True)
_MONEY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

mcp = FastMCP(
    "openinvest",
    instructions=(
        "openInvest — an investment decision runtime built for AI agents. "
        "Read/write portfolio, live prices, and the decision ledger; run a 4-role LLM "
        "investment committee (Direct path). Default currency for amounts is CNY. "
        "When the user declines a committee recommendation, ask why in one sentence, "
        "then call record_execution — reason capture is the host agent's job. "
        "Never place real orders: this runtime is decision support only; the human executes."
    ),
)


def _pm():
    from openinvest.core.portfolio_manager import PortfolioManager
    return PortfolioManager()


# 顾问模式白名单：委员会分析仍可用的工具，其余一律经 _check_advisory() 拒绝。
# 单一可信源——test_mcp_server.py::test_advisory_mode_gate_is_closed_set 用它反向
# 校验源码：不在这个集合里的工具必须显式调 _check_advisory()，新增工具漏加闸会
# 直接测试红（而不是悄悄放行给群聊陌生人）。
#
# what_if / record_execution 原本在顾问模式放行，收紧为拒绝：
# - what_if 是"对当前真实持仓做假设推演"，本质就是读持仓，泄露仓位和浮盈；
# - record_execution 写真实决策账本，顾问模式下群友既拿不到真实 decision_id
#   （decisions/history 已拒绝），放行也没有合法用途，只有被用来污染账本的风险。
ADVISORY_ALLOWED_TOOLS = frozenset({
    "run_committee", "explain_decision", "live_prices", "ingest_event",
})


def _check_advisory():
    """Disable portfolio-sensitive tools in INVEST_ADVISORY_MODE (guest/advisory mode)."""
    if is_advisory_mode():
        raise RuntimeError(
            "INVEST_ADVISORY_MODE=1: this tool is disabled in advisory mode. "
            "Only run_committee, explain_decision, live_prices, and ingest_event "
            "are available."
        )


# ---------- 只读 ----------

@mcp.tool(annotations=_RO)
def status() -> Dict[str, Any]:
    """Get a full snapshot of the user's portfolio: cash balances per currency,
    every holding with units / average cost / live price, and unrealized P&L
    per position and in total.

    Use when the user asks "show my portfolio", "how is my P&L", or before
    proposing any trade. Read-only; fetches live quotes, so values change
    between calls.

    Returns:
        Object with `cash` (currency → amount), `holdings` (list of positions
        with symbol, units, avg_cost, live price, market value, pnl_pct), and
        portfolio-level totals.
    """
    _check_advisory()
    from openinvest.services.skill_views import build_status_view
    return build_status_view()


@mcp.tool(annotations=_RO)
def strategy() -> Dict[str, Any]:
    """Get the user's investment strategy: target stock/cash allocation, the
    list of tracked assets (per-asset investment cap, purchase channel, fee
    settings), and long-term insights distilled by the nightly Dreaming
    memory-consolidation job.

    Use when deciding whether a proposed trade fits the user's plan, or when
    the user asks "what is my strategy / what am I tracking". Read-only.

    Returns:
        Object with `target_allocation` (stock/cash ratios), `target_assets`
        (tracked symbols with constraints), and `insights` (distilled
        lessons from past decisions).
    """
    _check_advisory()
    from openinvest.services.skill_views import build_strategy_view
    return build_strategy_view()


@mcp.tool(annotations=_RO)
def history(
    n: Annotated[int, Field(description="Maximum number of recent trades to return.")] = 10,
) -> Dict[str, Any]:
    """Get the most recent trade records and committee verdict history.

    Use when the user asks "what did I buy recently" or "what did the
    committee decide lately". Read-only.

    Args:
        n: Maximum number of recent trades to return (default 10).

    Returns:
        Object with `trades` (each with symbol, direction, units, price,
        timestamp, status) and recent committee verdict records.
    """
    _check_advisory()
    from openinvest.services.skill_views import build_history_view
    return build_history_view(n)


@mcp.tool(annotations=_RO)
def live_prices() -> Dict[str, Any]:
    """Fetch a one-shot market backdrop: spot gold (USD/oz and CNY/gram),
    USDCNY and AUDCNY FX rates, the NDQ.AX ETF price, the VIX volatility
    index, and the 10-year US Treasury yield (TNX).

    Use for quick market context before analysis or when the user asks
    "how is the market / what's the gold price". Read-only; single batch,
    no arguments.

    Returns:
        Object keyed by instrument (GC_F_usd_per_oz, gold_cny_per_gram_spot,
        USDCNY, AUDCNY, NDQ_AX, VIX, TNX) plus `as_of` ISO timestamp. A field
        is null when its upstream quote is unavailable.
    """
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
def what_if(
    symbol: Annotated[str, Field(description="yfinance ticker held or tracked by the user, e.g. 'NDQ.AX', 'GC=F', '510300.SS'.")],
    pct: Annotated[Optional[float], Field(description="Hypothetical percent change, e.g. -10 for a 10% drop. Provide exactly one of pct or price.")] = None,
    price: Annotated[Optional[float], Field(description="Hypothetical absolute target price (alternative to pct).")] = None,
) -> Dict[str, Any]:
    """Simulate portfolio P&L for a hypothetical price move: "what happens to
    my portfolio if <symbol> moves ±pct% / reaches <price>". Pure arithmetic
    over current holdings — no LLM call, instant, free.

    Use when the user asks scenario questions like "if the Nasdaq drops 10%,
    how much do I lose". Provide exactly one of `pct` or `price`.

    Args:
        symbol: yfinance ticker held or tracked by the user (e.g. "NDQ.AX",
            "GC=F", "510300.SS").
        pct: Hypothetical percent change, e.g. -10 for a 10% drop.
        price: Hypothetical absolute target price (alternative to pct).

    Returns:
        Object with the position's simulated value change and the resulting
        portfolio-level P&L delta.
    """
    _check_advisory()
    from openinvest.services.skill_views import build_what_if_view
    return build_what_if_view(symbol=symbol, pct=pct, price=price)


@mcp.tool(annotations=_RO)
def discipline() -> Dict[str, Any]:
    """Get the committee's discipline ledger: how often it chose inaction
    (HOLD ratio), how many impulsive user trades its rules intercepted, and
    the counterfactual money saved/lost by those interventions (ADR-023:
    the system's proven value is discipline and transparency, not alpha).

    Use when the user asks "what has the committee blocked" or "is this tool
    actually helping". Read-only.

    Returns:
        Object with `summary` (structured stats) and `markdown` (the same
        ledger pre-rendered for direct display to the user).
    """
    _check_advisory()
    from openinvest.services.discipline import discipline_summary, render_discipline_md
    s = discipline_summary()
    return {"summary": s, "markdown": render_discipline_md(s)}


@mcp.tool(annotations=_RO)
def decisions(
    days: Annotated[int, Field(description="Look-back window in days.")] = 90,
) -> Dict[str, Any]:
    """Get the unified decision ledger: every committee verdict joined with
    rule interventions, the user's actual executions or refusals (with
    reasons), and post-hoc outcome data — plus an adoption-rate summary.

    Answers "how often did I follow the advice", "which recommendations did
    I skip", and "what did the safety rules rewrite". Read-only.

    Args:
        days: Look-back window in days (default 90).

    Returns:
        Object with `count`, `summary` (adoption rate and aggregates), and
        `decisions` (list; each entry has decision_id, verdict, confidence,
        intervention, executed flag, matched trades, and outcome).
    """
    _check_advisory()
    from openinvest.core.decision_ledger import list_decisions, summarize_decisions
    ds = list_decisions(days=days)
    return {"count": len(ds), "summary": summarize_decisions(ds), "decisions": ds}


@mcp.tool(annotations=_RO)
def explain_decision(
    decision_id: Annotated[str, Field(description="\"<date>/<symbol>\", e.g. \"2026-07-03/GC=F\" — exactly as returned by the decisions tool.")],
) -> Dict[str, Any]:
    """Get the full reasoning behind one committee verdict: the complete
    4-role debate transcript with the CIO memo, plus the path-probability
    snapshot the CIO saw at decision time.

    Use when the user asks "why was today's verdict HOLD" or wants to audit
    a past decision. Read-only.

    Args:
        decision_id: "<date>/<symbol>", e.g. "2026-07-03/GC=F" — exactly as
            returned in the `decisions` tool output.

    Returns:
        Object with verdict, confidence, alloc_cny, `transcript_markdown`
        (render this to the user), and `path_snapshot` (may be null).
    """
    import json
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.core.memory_store import MemoryStore
    if "/" not in decision_id:
        return {"status": "error",
                "error": f'decision_id must be "<date>/<symbol>", got {decision_id!r}'}
    date, symbol = decision_id.split("/", 1)
    # date 段必须是日期字面量——否则 "../.." 之类会拼进路径逃出 .committee/
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"status": "error", "error": f"invalid date segment in decision_id: {date!r}"}
    safe = safe_symbol(symbol)
    base = MemoryStore().root / ".committee" / date
    md = base / f"{safe}.md"
    parsed = parse_committee_file(md)
    if not parsed:
        return {"status": "error",
                "error": f"decision {decision_id} not found ({md} missing or has no verdict)"}
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
def record_execution(
    decision_id: Annotated[str, Field(description="\"<date>/<symbol>\" from the decisions tool output.")],
    executed: Annotated[bool, Field(description="True if the user acted on the verdict, False if they declined.")],
    reason: Annotated[Optional[str], Field(description="The user's stated reason (especially when declined).")] = None,
    trade_ids: Annotated[Optional[List[int]], Field(description="Optional trade record IDs to link explicitly.")] = None,
) -> Dict[str, Any]:
    """Record whether the user executed or declined a committee verdict, with
    their reason. Appends to the execution ledger; idempotent — replaying the
    same record is a no-op, so retries are safe.

    Call when the user says "I bought it / I didn't buy / I disagree". When
    they decline, ask one short question for the reason first — this closes
    the adoption-rate loop that `decisions` reports on.

    Args:
        decision_id: "<date>/<symbol>" from the `decisions` output.
        executed: True if the user acted on the verdict, False if declined.
        reason: The user's stated reason (especially when declined).
        trade_ids: Optional trade record IDs to link explicitly.

    Returns:
        The stored execution record, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    from openinvest.core.decision_ledger import record_execution as _rec
    try:
        return _rec(decision_id, executed, reason=reason, trade_ids=trade_ids)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def ingest_event(
    title: Annotated[str, Field(description="Headline of the news item.")],
    url: Annotated[str, Field(description="Canonical source URL (also the dedup key).")],
    snippet: Annotated[str, Field(description="Short excerpt or summary of the article body.")] = "",
    source: Annotated[str, Field(description="Publisher name (e.g. 'Reuters') — the news outlet.")] = "",
    published_at: Annotated[Optional[str], Field(description="ISO 8601 publication time, if known.")] = None,
    ingested_by: Annotated[str, Field(description="Your own agent identity (e.g. 'hermes') for provenance; distinct from source.")] = "host-agent",
) -> Dict[str, Any]:
    """Feed a finance news item you (the host agent) found into the event
    ledger. The backend LLM normalizes it, grades severity, maps affected
    symbols, and stores it for committee RAG recall.

    You have far better search reach than the self-hosted crawler (including
    Chinese-language sources) — proactively feed news relevant to the user's
    holdings, especially A-share/regional coverage the crawler misses.
    Idempotent: re-sending the same url or claim does not double-insert.
    Requires a backend LLM key.

    Args:
        title: Headline of the news item.
        url: Canonical source URL (also the dedup key).
        snippet: Short excerpt or summary of the article body.
        source: Publisher name (e.g. "Reuters") — the news outlet.
        published_at: ISO 8601 publication time, if known.
        ingested_by: Your own agent identity (e.g. "hermes") for provenance;
            distinct in meaning from `source`.

    Returns:
        Ingestion result with the normalized event id(s) and dedup status.
    """
    from openinvest.services.event_ingest import ingest_events
    return ingest_events([{"title": title, "url": url, "snippet": snippet,
                           "source": source, "published_at": published_at}],
                         ingested_by=ingested_by)


# ---------- 持仓写（与 CLI / REST 共享 PortfolioManager，fcntl 锁保证一致） ----------

@mcp.tool(annotations=_MONEY)
def buy(
    symbol: Annotated[str, Field(description="yfinance ticker, e.g. 'AAPL', '510300.SS', 'GC=F'.")],
    units: Annotated[float, Field(description="Quantity bought; must be > 0.", gt=0)],
    price: Annotated[float, Field(description="Execution price per unit, in `currency`.", gt=0)],
    currency: Annotated[str, Field(description="Currency of `price`, e.g. 'CNY', 'USD', 'AUD'.")] = "CNY",
    kind: Annotated[str, Field(description="Asset kind tag, e.g. 'equity', 'etf', 'commodity'.")] = "equity",
    unit_label: Annotated[str, Field(description="Human display label for units (default '股', i.e. shares).")] = "股",
) -> Dict[str, Any]:
    """Record a buy in the local ledger: adds to an existing position with
    weighted-average cost, or opens a new position for an unseen symbol.
    This bookkeeps a trade the user already placed with their broker —
    openInvest never places real orders.

    Confirm symbol, units, and price with the user before calling; this
    moves ledger cash.

    Args:
        symbol: yfinance ticker (e.g. "AAPL", "510300.SS", "GC=F").
        units: Quantity bought; must be > 0.
        price: Execution price per unit, in `currency`.
        currency: Currency of `price` (default "CNY").
        kind: Asset kind tag, e.g. "equity", "etf", "commodity".
        unit_label: Human display label for units (default "股", i.e. shares).

    Returns:
        Updated position summary, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    try:
        return _pm().buy(symbol=symbol, units=units, price=price, currency=currency,
                         kind=kind, unit_label=unit_label, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def sell(
    symbol: Annotated[str, Field(description="yfinance ticker of an existing holding.")],
    units: Annotated[float, Field(description="Quantity sold; must be > 0.", gt=0)],
    price: Annotated[float, Field(description="Execution price per unit, in the holding's cost currency.", gt=0)],
) -> Dict[str, Any]:
    """Record a sell in the local ledger: reduces the position's units
    (average cost unchanged) and credits cash in the holding's cost
    currency. Bookkeeps a trade already executed at the user's broker —
    openInvest never places real orders.

    Confirm symbol, units, and price with the user before calling; this
    moves ledger cash.

    Args:
        symbol: yfinance ticker of an existing holding.
        units: Quantity sold; must be > 0.
        price: Execution price per unit, in the holding's cost currency.

    Returns:
        Updated position summary, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    if units <= 0 or price <= 0:
        return {"status": "error", "error": "units and price must both be > 0"}
    try:
        return _pm().sell(symbol=symbol, units=units, price=price, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def deposit(
    currency: Annotated[str, Field(description="ISO-style currency code, e.g. 'CNY', 'USD', 'AUD'.")],
    amount: Annotated[float, Field(description="Amount to add; must be > 0.", gt=0)],
) -> Dict[str, Any]:
    """Record a cash deposit into the ledger, in any currency. Bookkeeping
    only — no real payment system is connected.

    Args:
        currency: ISO-style currency code, e.g. "CNY", "USD", "AUD".
        amount: Amount to add; must be > 0.

    Returns:
        Updated cash balances, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    try:
        return _pm().deposit_cash(currency, amount, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_MONEY)
def withdraw(
    currency: Annotated[str, Field(description="ISO-style currency code, e.g. 'CNY', 'USD', 'AUD'.")],
    amount: Annotated[float, Field(description="Amount to remove; must be > 0.", gt=0)],
) -> Dict[str, Any]:
    """Record a cash withdrawal from the ledger, in any currency. Fails if
    the balance is insufficient. Bookkeeping only — no real payment system
    is connected.

    Args:
        currency: ISO-style currency code, e.g. "CNY", "USD", "AUD".
        amount: Amount to remove; must be > 0.

    Returns:
        Updated cash balances, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    if amount <= 0:
        return {"status": "error", "error": "amount must be > 0"}
    try:
        return _pm().withdraw_cash(currency, amount, source="mcp")
    except ValueError as e:
        return {"status": "error", "error": str(e)}


# ---------- strategy 写操作（issue #179：读写对等；实现共用 services.strategy_write）----------

_STRAT_W = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


@mcp.tool(annotations=_STRAT_W)
def set_allocations(
    target_allocation_stock: Annotated[float, Field(description="Stock weight in [0, 1], e.g. 0.7. Must sum to ~1.0 with cash.", ge=0, le=1)],
    target_allocation_cash: Annotated[float, Field(description="Cash weight in [0, 1], e.g. 0.3. Must sum to ~1.0 with stock.", ge=0, le=1)],
) -> Dict[str, Any]:
    """Update the strategy's target stock/cash allocation ratio. The two
    values must sum to ≈1.0; schema validation rejects and rolls back any
    write that would corrupt the strategy file.

    Use when the user says e.g. "set my target to 70% stock / 30% cash".

    Args:
        target_allocation_stock: Stock weight in [0, 1], e.g. 0.7.
        target_allocation_cash: Cash weight in [0, 1], e.g. 0.3.

    Returns:
        The updated allocation, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    from openinvest.services import strategy_write as svc
    try:
        return svc.set_allocations(target_allocation_stock, target_allocation_cash)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_STRAT_W)
def track_asset(
    symbol: Annotated[str, Field(description="yfinance ticker to track, e.g. 'AAPL', '0700.HK', 'BTC-USD'.")],
    max_single_invest_cny: Annotated[Optional[float], Field(description="Per-decision investment cap in CNY. Required when creating a new entry; optional on update.")] = None,
    display_name: Annotated[Optional[str], Field(description="Human-friendly name shown in reports.")] = None,
    channel: Annotated[Optional[str], Field(description="Where the user actually buys it (broker/app name).")] = None,
    price_offset_pct: Annotated[Optional[float], Field(description="Systematic offset between quote and actual fill price, in percent (e.g. bank gold spread).")] = None,
    sell_fee_pct: Annotated[Optional[float], Field(description="Sell-side fee in percent, used by fee-aware math.")] = None,
) -> Dict[str, Any]:
    """Add a symbol to the tracked-asset list, or update an existing entry
    (idempotent upsert: only the fields you pass are changed). The tracked
    list decides which symbols the committee and DCA jobs cover.

    Use when the user says "track AAPL" or wants to change a tracked
    asset's cap/channel/fees.

    Args:
        symbol: yfinance ticker to track (e.g. "AAPL", "0700.HK", "BTC-USD").
        max_single_invest_cny: Per-decision investment cap in CNY. Required
            when creating a new entry; optional on update.
        display_name: Human-friendly name shown in reports.
        channel: Where the user actually buys it (broker/app name).
        price_offset_pct: Systematic offset between the quote and the user's
            actual fill price, in percent (e.g. bank gold spread).
        sell_fee_pct: Sell-side fee in percent, used by fee-aware math.

    Returns:
        The stored asset entry, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    from openinvest.services import strategy_write as svc
    try:
        return svc.upsert_target_asset(symbol, {
            "max_single_invest_cny": max_single_invest_cny,
            "display_name": display_name,
            "channel": channel,
            "price_offset_pct": price_offset_pct,
            "sell_fee_pct": sell_fee_pct,
        })
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
def untrack_asset(
    symbol: Annotated[str, Field(description="yfinance ticker currently in the tracked list.")],
) -> Dict[str, Any]:
    """Remove a symbol from the tracked-asset list — the committee and DCA
    jobs stop covering it. Holdings and trade history are untouched; schema
    validation guarantees at least one tracked asset remains.

    Args:
        symbol: yfinance ticker currently in the tracked list.

    Returns:
        The updated tracked list, or {"status": "error", "error": ...}.
    """
    _check_advisory()
    from openinvest.services import strategy_write as svc
    try:
        return svc.remove_target_asset(symbol)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


# ---------- 委员会（Direct 路径） ----------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def run_committee(
    symbol: Annotated[str, Field(description="Any yfinance ticker (US / HK / A-share / ETF / crypto / commodities), e.g. 'AAPL', 'GC=F', '510300.SS'.")],
    force: Annotated[bool, Field(description="Re-run even if a verdict already exists for today.")] = False,
    max_rounds: Annotated[int, Field(description="Cross-challenge debate rounds.", ge=1)] = 1,
) -> Dict[str, Any]:
    """Run the 4-role LLM investment committee on a symbol (Direct path):
    Macro Strategist, Quant Analyst, and Risk Officer debate from isolated
    evidence, then a CIO synthesizes one calibrated BUY/HOLD/SELL-style
    verdict with a written memo.

    Requires a backend LLM key (e.g. DEEPSEEK_API_KEY) and takes 30-90s on
    a cache miss. If the symbol was already analyzed today, the cached
    verdict is returned instantly unless `force` is set. Decision support
    only — the human always executes.

    Args:
        symbol: Any yfinance ticker (US / HK / A-share / ETF / crypto /
            commodities), e.g. "AAPL", "GC=F", "510300.SS".
        force: Re-run even if a verdict already exists for today.
        max_rounds: Cross-challenge debate rounds (default 1).

    Returns:
        Object with `decision_id`, `cached` flag, and `verdict` (verdict,
        confidence, suggested allocation, CIO memo).
    """
    import json
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.core.memory_store import MemoryStore
    from datetime import datetime

    if not force:
        today = datetime.now().strftime("%Y-%m-%d")
        safe = safe_symbol(symbol)
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


# ---------- streamable-HTTP transport（remote MCP，issue #179 后续：REST 退役路线 A）----------


@mcp.custom_route("/health", methods=["GET"])
async def _health(_request):  # noqa: ANN001
    """探活（鉴权豁免，对齐 web_api /api/health）。custom route 只在
    streamable_http_app() 物化——stdio 模式不起 HTTP 栈，注册零影响。"""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


class _BearerAuthMiddleware:
    """与 web_api._bearer_token_auth 同语义（同一 INVEST_API_TOKEN，两处注释互指对齐）：

    - 不设 token → 直通（仅限 loopback 开发形态；_serve_http 拒绝"非 loopback 且无 token"）
    - 设了 token → 除 /health 外所有 HTTP 请求必须 `Authorization: Bearer <token>`
    - 每请求读 env（systemd reload / 测试 monkeypatch 即时生效）
    - secrets.compare_digest 防时序侧信道；token 永不进日志、永不进响应体

    原生 ASGI 三段式（不用 BaseHTTPMiddleware：它对流式响应有历史坑，且这里
    只需改写 4xx 短路径）。lifespan / websocket scope 原样穿透。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        import os
        import secrets as _secrets

        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        token = os.getenv("INVEST_API_TOKEN", "").strip()
        if token:
            # 取第一个 authorization 头（对齐 Starlette Headers.get / web_api 语义；
            # dict() 会取最后一个，重复头时两面行为漂移）
            auth = next(
                (v for k, v in (scope.get("headers") or []) if k == b"authorization"),
                b"",
            ).decode("latin-1")
            provided = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not (provided and _secrets.compare_digest(provided, token)):
                import json as _json

                body = _json.dumps({
                    "detail": (
                        "unauthorized：本 hub 开启了 INVEST_API_TOKEN 鉴权，"
                        "请求需带 `Authorization: Bearer <token>`"
                    )
                }, ensure_ascii=False).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _configure_http_settings(host: str, port: int) -> None:
    """HTTP transport 的全部 settings 突变（_serve_http 与测试共用一套组装）。

    Host 校验（SDK DNS-rebinding 防护）三档，按**信任边界**排：
    1) INVEST_MCP_ALLOWED_HOSTS 显式白名单 → 开校验（最紧；条目支持 host:* 通配端口）
    2) 设了 INVEST_API_TOKEN → 关校验：DNS-rebinding 的威胁模型是"浏览器页面打
       无鉴权本机服务"，而浏览器发起的重绑请求带不上 Authorization 头（先吃
       _BearerAuthMiddleware 的 401）。文档推荐形态 = 绑 loopback + Caddy 反代，
       下游 Host 是公网域名——此档若开校验会把全部合法流量 421（review 真机踩过）
    3) 无 token（_serve_http 守卫保证此时必为 loopback 绑定）→ 保留 SDK 构造时的
       loopback 自动白名单——无鉴权本机服务正是 rebinding 防护该管的形态
    """
    import os

    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True

    token = os.getenv("INVEST_API_TOKEN", "").strip()
    allowed = os.getenv("INVEST_MCP_ALLOWED_HOSTS", "").strip()
    if allowed:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        )
    elif token:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )


def _serve_http() -> None:
    """streamable-HTTP 常驻服务（`openinvest-mcp --http`）。

    - 绑定：INVEST_MCP_HOST（默认 127.0.0.1，生产由 Caddy/CF 反代）/ INVEST_MCP_PORT（默认 8766）
    - 非 loopback 绑定且未设 INVEST_API_TOKEN → 拒绝启动（信任边界不裸奔）
    - stateless + json_response：18 个工具全无状态；纯 JSON 响应不给 CF 边缘留 SSE 长流
    - /health 探活豁免鉴权（对齐 web_api 的 /api/health 语义）
    """
    import os
    import sys

    import uvicorn

    host = os.getenv("INVEST_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("INVEST_MCP_PORT", "8766"))
    token = os.getenv("INVEST_API_TOKEN", "").strip()
    if not token and host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(
            f"拒绝启动：绑定 {host}（非 loopback）但 INVEST_API_TOKEN 未设置。"
            "远端暴露必须有 bearer 鉴权——在 .env 设 INVEST_API_TOKEN，"
            "或改绑 127.0.0.1 走反向代理。"
        )

    _configure_http_settings(host, port)

    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="openinvest-mcp")
    p.add_argument(
        "--http", action="store_true",
        help="streamable-HTTP transport（remote MCP，INVEST_MCP_HOST/PORT，默认 stdio）",
    )
    if p.parse_args().http:
        _serve_http()
    else:
        mcp.run()  # stdio transport（默认，行为不变）


if __name__ == "__main__":
    main()
