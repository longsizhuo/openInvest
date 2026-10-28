"""给宿主 Agent 的一段离线账本摘要（`openinvest brief`）：插件钩子在会话开头 / 账本变化时注入。

为什么要有：Hermes 不把 MCP server 的 instructions 放进提示词，Agent 只有在用户说"持仓 / 投资"时
才想起 openInvest；问"英伟达最近怎么样，该继续拿吗"（NVDA 就在账本里）只会去网页搜索。给它一行
"账本里有什么、今天有什么待办"，同样的问题就会先查账本（2026-10-27 hermes-clone 对照实验）。

硬约束：
- 纯本地读、不联网——钩子可能每个会话都调，yfinance 一卡就拖住对话；
- 不调 agent_queue.pending_news() / pending_committees() 包装函数：它们会 mark_drained，
  看门狗就再也看不到"有待办但没人来取"；只用纯读的 *_items()；
- 顾问模式（群聊共享账本）不列持仓。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from openinvest.utils.advisory import is_advisory_mode

MAX_SYMBOLS = 15
# 群聊里 "the user" 指谁说不清（Hermes 群会话也注入，#562 评审）——写成 Agent 的主人
HEADER = "[openInvest] This agent's owner keeps their investment ledger in openInvest (MCP server `openinvest`)."
ROUTING = ("For any question about stocks, funds, ETFs, gold, crypto, markets, macro, buying or selling, "
           "or the owner's money: call the openinvest tools first (status / live_prices / committee), "
           "then add web search if needed.")


def build_brief(root: Path) -> Dict[str, Any]:
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.core.portfolio_manager import MemoryNotInitialized, PortfolioManager
    from openinvest.scheduler.lock import holder
    from openinvest.scheduler.service_unit import unit_path
    from openinvest.services import agent_queue

    if is_advisory_mode():
        return {"status": "ok", "advisory": True}
    try:
        pm = PortfolioManager()
    except MemoryNotInitialized:
        return {"status": "needs_setup"}

    holdings = [{"symbol": h.get("symbol"), "name": h.get("display_name") or h.get("name")}
                for h in pm.holdings if h.get("symbol")]
    held = {h["symbol"] for h in holdings}
    watchlist = [a["symbol"] for a in (pm.strategy.get("target_assets") or [])
                 if a.get("symbol") and a["symbol"] not in held]

    verdicts: List[Dict[str, Any]] = []
    day_dir = pm.store.root / ".committee" / datetime.now().strftime("%Y-%m-%d")
    for md in sorted(day_dir.glob("*.md")) if day_dir.is_dir() else []:
        parsed = parse_committee_file(md)
        if parsed:
            verdicts.append({"symbol": parsed.get("symbol") or md.stem, "verdict": parsed["verdict"]})

    sched = holder(root)
    return {
        "status": "ok",
        "holdings": holdings,
        "watchlist": watchlist,
        "verdicts_today": verdicts,
        "pending": {"news": len(agent_queue.pending_news_items()),
                    "committees": len(agent_queue.pending_committee_items())},
        # {} = 持有者刚抢到锁、还没写完正文：也算在跑
        "scheduler": (sched.get("mode") or "unknown") if sched is not None else None,
        "service_installed": unit_path().exists(),
    }


def _symbols(items: List[str]) -> str:
    shown = ", ".join(items[:MAX_SYMBOLS])
    return shown + (f" (+{len(items) - MAX_SYMBOLS} more)" if len(items) > MAX_SYMBOLS else "")


def render_brief_text(b: Dict[str, Any]) -> str:
    """给钩子注入的一段话（英文给模型读；标的名保留原文）。"""
    if b.get("status") == "needs_setup":
        return ("[openInvest] Installed but not set up yet. When the user talks about investing, "
                "use the openinvest:invest-setup skill to onboard them.")
    lines = [HEADER]
    if not b.get("advisory"):
        if b.get("holdings"):
            lines.append("Holdings: " + _symbols(
                [f"{h['symbol']} ({h['name']})" if h.get("name") else h["symbol"] for h in b["holdings"]]) + ".")
        if b.get("watchlist"):
            lines.append("Watchlist: " + _symbols(b["watchlist"]) + ".")
        if b.get("verdicts_today"):
            lines.append("Today's committee verdicts: "
                         + ", ".join(f"{v['symbol']} {v['verdict']}" for v in b["verdicts_today"]) + ".")
        p = b.get("pending") or {}
        if p.get("news") or p.get("committees"):
            lines.append(f"Waiting for you: {p.get('news', 0)} news item(s) to judge, "
                         f"{p.get('committees', 0)} committee run(s) — see the openinvest:invest skill "
                         "(pending_news / pending_committees).")
        # 只靠内嵌调度器时不报：会话开头钩子多半跑在 MCP 进程（和它拉起的调度器）之前，报了是误报。
        # 装了系统服务却没人持锁才是真故障
        if b.get("service_installed") and not b.get("scheduler"):
            lines.append("The openInvest background service is installed but not running, so news and prices "
                         "are not being collected (run doctor for the fix).")
    lines.append(ROUTING)
    return " ".join(lines)


def render_fallback_text() -> str:
    """摘要读失败（账本解析出错 / events.db 被锁）时钩子仍拿到的最小提示：不列账本，只给路由规则。"""
    return f"{HEADER} {ROUTING}"
