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
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from openinvest.utils.symbols import safe_symbol

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
    safe = safe_symbol(symbol)
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def ingest_event(title: str, url: str, snippet: str = "",
                 source: str = "", published_at: Optional[str] = None) -> Dict[str, Any]:
    """把你（宿主 agent）搜到的财经新闻投喂进事件账本：后端 LLM 归一化 →
    severity/symbol 判级 → 入库 → 供委员会 RAG 召回。**你有比自托管爬虫强得多的
    搜索能力（含中文源）——看到与用户持仓相关的新闻就喂进来**，尤其 A 股/区域
    市场（爬虫盲区）。幂等：同 url / 同 claim 重发不重复入账。需后端 LLM key。"""
    from openinvest.services.event_ingest import ingest_events
    return ingest_events([{"title": title, "url": url, "snippet": snippet,
                           "source": source, "published_at": published_at}])


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


# ---------- strategy 写操作（issue #179：读写对等；实现共用 services.strategy_write）----------

_STRAT_W = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


@mcp.tool(annotations=_STRAT_W)
def set_allocations(target_allocation_stock: float, target_allocation_cash: float) -> Dict[str, Any]:
    """改股票/现金目标配比（两者和必须 ≈1，schema 强校验失败自动回滚）。"""
    from openinvest.services import strategy_write as svc
    try:
        return svc.set_allocations(target_allocation_stock, target_allocation_cash)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(annotations=_STRAT_W)
def track_asset(symbol: str, max_single_invest_cny: Optional[float] = None,
                display_name: Optional[str] = None, channel: Optional[str] = None,
                price_offset_pct: Optional[float] = None,
                sell_fee_pct: Optional[float] = None) -> Dict[str, Any]:
    """跟踪标的（upsert 幂等）：不存在则新建（此时 max_single_invest_cny 必填），
    已存在只更新传入的字段。委员会/DCA 覆盖哪些 symbol 由跟踪列表决定。"""
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
def untrack_asset(symbol: str) -> Dict[str, Any]:
    """移除跟踪标的（委员会不再分析它；schema 保证至少剩 1 个跟踪标的）。"""
    from openinvest.services import strategy_write as svc
    try:
        return svc.remove_target_asset(symbol)
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
