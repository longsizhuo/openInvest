"""services/event_notifier —— digest 邮件给 trigger 路径

合并 cycle 内的多个事件到一封 digest 邮件，避免轰炸用户。
邮件含：事件 title/source/触发时间/stance/severity/受影响 symbol/one_line_claim/持仓数值/委员会进度链接。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from services.notifier import (
    EmailDeliveryError,
    render_markdown_email,
    send_email_html,
)

log = logging.getLogger(__name__)


_STANCE_ICON = {"risk": "🚨", "opportunity": "🎯", "neutral": "📰"}


def send_event_alert(
    events: List[Dict[str, Any]],
    *,
    committee_task_id: Optional[str] = None,
    api_base_url: Optional[str] = None,
    holdings_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """合并多事件成一封 digest 邮件。

    Args:
        events: 每条至少含 one_line_claim / stance / severity / affected_symbols / ts。
                可选 sources / committee_task_id。
        committee_task_id: 整批触发的委员会 task id（一条链接）
        api_base_url: 委员会进度链接前缀，默认 INVEST_API_BASE_URL or http://localhost:8765
        holdings_snapshot: { symbol: { units, price, mv, pnl_pct } } 用户持仓快照，
                          若给了会在邮件里渲染"我当前持仓 X，浮盈亏 Y"

    Returns:
        receiver 邮箱，凭据缺失 → "", 投递失败抛 EmailDeliveryError。
    """
    if not events:
        return ""

    api_base_url = api_base_url or os.getenv("INVEST_API_BASE_URL", "http://localhost:8765")
    subject = _build_subject(events)
    md = _build_markdown(
        events,
        committee_task_id=committee_task_id,
        api_base_url=api_base_url,
        holdings_snapshot=holdings_snapshot or {},
    )
    html = render_markdown_email(md, footer_label="Invest Event Watch")
    return send_email_html(subject=subject, html_body=html, plain_body=md)


def _build_subject(events: List[Dict[str, Any]]) -> str:
    stances = {e.get("stance", "neutral") for e in events}
    n = len(events)
    if stances == {"risk"}:
        icon = "🚨"
        label = "Risk"
    elif stances == {"opportunity"}:
        icon = "🎯"
        label = "Opportunity"
    else:
        icon = "📰"
        label = "Mixed"
    date_str = datetime.now().strftime("%H:%M")
    syms = sorted({s for e in events for s in (e.get("affected_symbols") or [])})[:3]
    sym_part = f" — {', '.join(syms)}" if syms else ""
    return f"{icon} Event Alert [{label}] {date_str}{sym_part} ({n})"


def _build_markdown(
    events: List[Dict[str, Any]],
    *,
    committee_task_id: Optional[str],
    api_base_url: str,
    holdings_snapshot: Dict[str, Dict[str, Any]],
) -> str:
    lines: List[str] = ["# 事件预警 (Event Watch)"]
    if committee_task_id:
        url = f"{api_base_url.rstrip('/')}/api/committee/{committee_task_id}"
        lines.append(
            f"\n> 已自动触发投资委员会重跑：[{committee_task_id}]({url})\n"
            f"> verdict 邮件将在数分钟内单独送达。"
        )

    for i, e in enumerate(events, 1):
        icon = _STANCE_ICON.get(e.get("stance", "neutral"), "📰")
        stance = e.get("stance", "neutral").upper()
        severity = e.get("severity", "low").upper()
        symbols = e.get("affected_symbols") or []
        ts = e.get("ts", "")
        sources = e.get("sources") or []

        lines.append(f"\n## {i}. {icon} {e.get('one_line_claim', '')}")
        lines.append("")
        lines.append(f"- **Stance**: {stance} / **Severity**: {severity}")
        lines.append(f"- **Affected**: {', '.join(symbols) if symbols else '(macro/无指定 symbol)'}")
        lines.append(f"- **Event time**: {ts or 'n/a'}")
        if sources:
            src_lines = []
            for s in sources[:4]:
                title = s.get("title") or "(no title)"
                url = s.get("url") or ""
                name = s.get("src_name") or "?"
                src_lines.append(f"  - [{title}]({url}) ({name})")
            lines.append("- **Sources**:")
            lines.extend(src_lines)

        # 持仓快照（如果有）
        for sym in symbols:
            snap = holdings_snapshot.get(sym)
            if not snap:
                continue
            units = snap.get("units", 0)
            price = snap.get("price")
            mv = snap.get("mv")
            pnl_pct = snap.get("pnl_pct")
            bits = [f"units={units}"]
            if price is not None:
                bits.append(f"price={price}")
            if mv is not None:
                bits.append(f"mv={mv:.0f}")
            if pnl_pct is not None:
                bits.append(f"pnl={pnl_pct*100:+.2f}%")
            lines.append(f"- **My {sym}**: " + ", ".join(bits))

    lines.append("\n---")
    lines.append(
        "_Event Watch 是 openInvest 第一层（盘中实时）。如果你希望调阈值或关掉，"
        "改 `jobs/event_watch.yml`。_"
    )
    return "\n".join(lines)
