"""services/event_notifier —— digest 邮件给 trigger 路径

合并 cycle 内的多个事件到一封 digest 邮件，避免轰炸用户。
邮件含：事件 title/source/触发时间/stance/severity/受影响 symbol/one_line_claim/持仓数值/委员会进度链接。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.notifier import (
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


def send_committee_verdict_email(
    *,
    task_id: str,
    symbols: List[str],
    by_asset: Dict[str, Any],
    event_ids: Optional[List[str]] = None,
    api_base_url: Optional[str] = None,
) -> str:
    """事件触发的委员会重跑完成后，发一封 verdict 结果邮件。

    补 event_watch → 委员会 → verdict 邮件链路的最后一环：此前 web 路径
    (_run_committee_task) 跑完不发任何邮件，event 预警里"verdict 邮件将随后送达"
    成了空头支票（委员会其实跑了，结果只进 status.json，用户看不到）。
    这里在委员会 done 后渲染 verdict 摘要并投递。

    Args:
        by_asset: {sym: {"verdict": {verdict/confidence/dominant_view/alloc_cny}, "error": ...}}
    Returns:
        receiver 邮箱；凭据缺失 → ""；投递失败抛 EmailDeliveryError。
    """
    api_base_url = api_base_url or os.getenv("INVEST_API_BASE_URL", "http://localhost:8765")
    lines = ["# 📊 事件触发的委员会重跑结果\n"]
    if event_ids:
        lines.append(f"触发事件: `{', '.join(event_ids[:6])}`")
    lines.append(f"任务: `{task_id}` · {datetime.now():%Y-%m-%d %H:%M}\n")
    for sym in symbols:
        a = by_asset.get(sym) or {}
        if a.get("error"):
            lines.append(f"## {sym}\n- ⚠️ 运行失败: {a['error']}")
            continue
        v = a.get("verdict") or {}
        lines.append(f"## {sym} — **{v.get('verdict', 'UNCLEAR')}**")
        bits = []
        if v.get("confidence") is not None:
            bits.append(f"confidence {v['confidence']:.2f}")
        if v.get("dominant_view"):
            bits.append(f"主导视角 {v['dominant_view']}")
        if v.get("alloc_cny") is not None:
            bits.append(f"建议金额 {v['alloc_cny']:+d} CNY")
        if bits:
            lines.append("- " + " · ".join(bits))
    lines.append(f"\n详情 / transcript: {api_base_url.rstrip('/')}/committee/{task_id}")
    lines.append(
        "\n---\n_这封是事件预警自动触发的委员会 verdict（补齐了 event_watch → 委员会 → "
        "邮件之前断掉的最后一环）。_"
    )
    md = "\n".join(lines)
    subject = "📊 委员会重跑 verdict: " + " / ".join(
        f"{s} {(by_asset.get(s, {}).get('verdict') or {}).get('verdict', '?')}"
        for s in symbols[:3]
    )
    html = render_markdown_email(md, footer_label="Invest Event Watch · Committee")
    return send_email_html(subject=subject, html_body=html, plain_body=md)
