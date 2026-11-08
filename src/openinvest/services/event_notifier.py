"""services/event_notifier —— digest 邮件给 trigger 路径

合并 cycle 内的多个事件到一封 digest 邮件，避免轰炸用户。
邮件含：事件 title/source/触发时间/stance/severity/受影响 symbol/one_line_claim/持仓数值/委员会进度链接。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from openinvest.services.discord_notify import send_discord_alert
from openinvest.services.notifier import (
    md_text,
    md_url,
    render_markdown_email,
    send_email_html,
)

log = logging.getLogger(__name__)


# opportunity 曾用 🎯（暗示"命中/买入"）——issue #210 事件研究：opportunity 类事件后
# 5 日超额 -0.48%、按标的聚类不显著（2026-10 重跑；早先"-1.07% 显著"已不成立），
# 其后 5 日上涨的只占 39.5%，不能当买入信号。换成 🔍（"留意/值得细看"），不给用户方向性暗示。
_STANCE_ICON = {"risk": "🚨", "opportunity": "🔍", "neutral": "📰"}


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
    receipt = send_event_alert_receipt(
        events, committee_task_id=committee_task_id, api_base_url=api_base_url,
        holdings_snapshot=holdings_snapshot,
    )
    return receipt.get("channels", {}).get("email", {}).get("target") or ""


def send_event_alert_receipt(
    events: List[Dict[str, Any]], *, committee_task_id=None, api_base_url=None,
    holdings_snapshot=None, notification_key=None, risk_alert=False, dispatch=True, store=None,
    followup=None,
    cooldown=None,
    now=None,
) -> dict:
    """Queue first; a receipt reports channel acceptance, never merely a detection."""
    if not events:
        return {"accepted": False, "channels": {}, "status": "empty"}
    import hashlib
    import json
    from openinvest.services.notification_outbox import NotificationOutbox
    api_base_url = api_base_url or os.getenv("INVEST_API_BASE_URL", "http://localhost:8765")
    # Market notices carry market facts. Account figures belong in a current,
    # source-qualified portfolio view, never in a delayed pre-rendered email.
    md = _build_markdown(events, committee_task_id=committee_task_id,
                         api_base_url=api_base_url, holdings_snapshot={})
    payload = {"subject": _build_subject(events), "plain": md,
               "html": render_markdown_email(md, footer_label="Invest Event Watch", link_hosts=_link_hosts(events, api_base_url)),
               "discord": _build_discord_text(events, committee_task_id=committee_task_id, api_base_url=api_base_url),
               "events": events, "followup": followup, "cooldown": cooldown,
               "account_snapshot_included":False}
    fingerprint = json.dumps([{k: e.get(k) for k in ("event_id", "_version_id", "one_line_claim", "stance", "severity", "ts")} for e in events], sort_keys=True, ensure_ascii=False)
    key = notification_key or "event:" + hashlib.sha256(fingerprint.encode()).hexdigest()
    outbox = NotificationOutbox(store)
    ident = outbox.enqueue(key, payload, category="market_risk" if risk_alert else "event",
                           policy="risk" if risk_alert else "events",now=now)
    if dispatch:
        outbox.flush(only_id=ident,now=now)
    return outbox.receipt(ident)


def _build_discord_text(
    events: List[Dict[str, Any]],
    *,
    committee_task_id: Optional[str],
    api_base_url: str,
) -> str:
    """事件报警的 Discord 精简版：一行一事件，带委员会链接。

    完整详情（sources / 持仓快照）仍在邮件里；DM 版只求"手机上 10 秒读完，
    想深挖就直接回复让 agent 就地分析"。
    """
    lines = [f"**{_build_subject(events)}**"]
    for e in events[:6]:
        icon = _STANCE_ICON.get(e.get("stance", "neutral"), "📰")
        syms = ", ".join(e.get("affected_symbols") or []) or "macro"
        lines.append(f"{icon} {e.get('one_line_claim', '')} `[{syms}]`")
    if len(events) > 6:
        lines.append(f"…… 另有 {len(events) - 6} 条，见邮件")
    if committee_task_id and str(committee_task_id).startswith("queued:"):
        lines.append("⚙️ 委员会重跑已排队，等宿主 Agent 来跑（后端不调模型）")
    elif committee_task_id:
        lines.append(
            f"⚙️ 已自动触发委员会重跑：{api_base_url.rstrip('/')}/api/committee/{committee_task_id}"
        )
    lines.append("-# 详情在邮件里；直接回复这条消息可让 AI 助手就地分析")
    return "\n".join(lines)


def _link_hosts(events: List[Dict[str, Any]], api_base_url: str) -> set:
    """本封预警里模板自己拼的链接的 host（委员会链接 + 经 md_url 的新闻来源）——
    渲染白名单只放行这些 host 的 href，正文里别的链接都退化成文字。"""
    urls = [api_base_url] + [md_url(s.get("url")) for e in events for s in (e.get("sources") or [])[:4]]
    return {urlsplit(u).hostname for u in urls if u} - {None}


def _build_subject(events: List[Dict[str, Any]]) -> str:
    stances = {e.get("stance", "neutral") for e in events}
    n = len(events)
    if stances == {"risk"}:
        icon = "🚨"
        label = "Risk"
    elif stances == {"opportunity"}:
        icon = "🔍"
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
    if committee_task_id and str(committee_task_id).startswith("queued:"):
        lines.append("\n> 投资委员会重跑已排队，等宿主 Agent 来跑（agent 模式，后端不调模型）；"
                     "Agent 交卷后裁决照常进日报。")
    elif committee_task_id:
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

        lines.append(f"\n## {i}. {icon} {md_text(e.get('one_line_claim', ''))}")
        lines.append("")
        lines.append(f"- **Stance**: {md_text(stance)} / **Severity**: {md_text(severity)}")
        lines.append(f"- **Affected**: {md_text(', '.join(symbols)) if symbols else '(macro/无指定 symbol)'}")
        lines.append(f"- **Event time**: {md_text(ts) or 'n/a'}")
        if sources:
            src_lines = []
            for s in sources[:4]:
                # 标题/来源名/URL 都来自外部新闻源：转义后再拼，非 http(s) 链接只出文字
                title = md_text(s.get("title") or "(no title)")
                url = md_url(s.get("url"))
                name = md_text(s.get("src_name") or "?")
                src_lines.append(f"  - [{title}]({url}) ({name})" if url else f"  - {title} ({name})")
            lines.append("- **Sources**:")
            lines.extend(src_lines)

        # 持仓快照（如果有）
        for sym in symbols:
            snap = holdings_snapshot.get(sym)
            if not snap:
                continue
            units = snap.get("units")
            price = snap.get("price")
            mv = snap.get("mv")
            pnl_pct = snap.get("pnl_pct")
            bits = [f"units={units}" if units is not None else "数量尚待核对"]
            if price is not None:
                bits.append(f"price={price}")
            if mv is not None:
                bits.append(f"mv={mv:.0f}")
            if pnl_pct is not None:
                bits.append(f"pnl={pnl_pct*100:+.2f}%")
            lines.append(f"- **My {sym}（记录时）**: " + ", ".join(bits))

    lines.append("\n---")
    lines.append(
        "_新闻记录不等于新的交易信号；通知频率可直接向助手调整。_"
    )
    return "\n".join(lines)


def render_committee_notice(
    *,
    task_id: str,
    symbols: List[str],
    by_asset: Dict[str, Any],
    event_ids: Optional[List[str]] = None,
    api_base_url: Optional[str] = None,
) -> dict | None:
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
    # 2026-09-10 老大要求：只有非 HOLD 裁决才推送（全 HOLD 静默不打扰）。
    # 运行失败（error）仍要通知——不能把失败也静默掉。
    _verdicts: List[str] = []
    _has_error = False
    for _sym in symbols:
        _a = by_asset.get(_sym) or {}
        if _a.get("error"):
            _has_error = True
            continue
        _verdicts.append(str((_a.get("verdict") or {}).get("verdict") or "").upper())
    _non_hold = [v for v in _verdicts if v and v != "HOLD"]
    if not _non_hold and not _has_error:
        log.info("send_committee_verdict_email: 全 HOLD %s，按老大要求静默", _verdicts)
        return None

    api_base_url = api_base_url or os.getenv("INVEST_API_BASE_URL", "http://localhost:8765")
    today = datetime.now().strftime("%Y-%m-%d")
    lines = ["# 📊 事件触发的委员会重跑结果\n"]
    if event_ids:
        lines.append(f"触发事件: `{', '.join(event_ids[:6])}`")
    lines.append(f"任务: `{task_id}` · {datetime.now():%Y-%m-%d %H:%M}\n")
    # 裁决旁给同类决议查表，不给 CIO 自报 confidence（D10 P1，只改展示）
    from openinvest.jobs.review_calc import confidence_display
    from openinvest.jobs.verdict_review import load_confidence_lookup
    lookup = load_confidence_lookup()
    for sym in symbols:
        a = by_asset.get(sym) or {}
        if a.get("error"):
            lines.append(f"## {sym}\n- ⚠️ 运行失败: {a['error']}")
            continue
        v = a.get("verdict") or {}
        lines.append(f"## {sym} — **{v.get('verdict', 'UNCLEAR')}**")
        bits = []
        if v.get("verdict"):
            bits.append(confidence_display(v, lookup))
        if v.get("dominant_view"):
            bits.append(f"主导视角 {v['dominant_view']}")
        if v.get("alloc_cny") is not None:
            from openinvest.utils.base_currency import base_currency
            bits.append(f"建议金额 {v['alloc_cny']:+d} {v.get('alloc_currency') or base_currency()}")
        if bits:
            lines.append("- " + " · ".join(bits))
        # decision_id 是唯一不需要任何网络配置就能拿到详情的方式——GUI 已退役，
        # api_base_url 链接指向的是原始 JSON（不是渲染页面），且没配置
        # INVEST_API_BASE_URL 时会退化成本机 localhost，读邮件/DM 的设备打不开。
        # 让任何 agent（Hermes/Claude/...）调 explain_decision 才是零配置能打开的路。
        lines.append(f"  详情：让你的 agent 调 `explain_decision(\"{today}/{sym}\")`")
    # api_base_url 链接是次要的技术备选（需要 INVEST_API_BASE_URL 配置成公网可达地址，
    # 否则默认 localhost 在邮件/DM 里打不开——见上方每个资产的 explain_decision）。
    lines.append(f"\n详情 / transcript: {api_base_url.rstrip('/')}/api/committee/{task_id}/view")
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
    # verdict 同样先推 DM（md 本身已足够精简，Discord 原生渲染 markdown）
    return {"subject":subject,"html":html,"plain":md,"discord":f"**{subject}**\n{md}"}


def send_committee_verdict_email(*,task_id,symbols,by_asset,event_ids=None,api_base_url=None) -> str:
    """All entrances reconcile the same sealed decisions; the dispatcher sends once."""
    from datetime import timezone
    from openinvest.core.memory_store import MemoryStore
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.jobs.notification_delivery import reconcile_decisions
    store=MemoryStore();outbox=NotificationOutbox(store)
    now=datetime.now(timezone.utc)
    reconcile_decisions(store,outbox,now=now)
    failed=[]
    for symbol in symbols:
        item=by_asset.get(symbol) or {}
        verdict=item.get("verdict") or {}
        if item.get("error") or (verdict.get("verdict") not in (None,"HOLD") and not verdict.get("decision_id")):
            failed.append(symbol)
    if failed:
        text="、".join(failed)+" 的分析未完成或无法核验完整输入。本轮没有新的可执行建议，需重新复核。"
        payload={"subject":"openInvest 分析需要复核","plain":text,"discord":text,
                 "html":render_markdown_email(text,footer_label="Investment Committee"),"decision_quality":True}
        outbox.enqueue("committee-task-error:"+str(task_id),payload,category="decision_quality",policy="decision",now=now)
    return ""  # Queued is not a receiver/provider acceptance receipt.
