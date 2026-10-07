"""事件 → 委员会触发闸 —— 爬虫 jobs/event_watch 与 agent 投喂 services/event_ingest 共用。

2026-10 修：判级闸（severity ≥ event.min_severity + stance ≠ neutral + 命中
holdings ∪ target_assets）和"触发委员会 + 报警"此前只写在 event_watch.run() 里，
agent 投喂门（MCP/CLI ingest_event，Hermes market-intel-sentinel 走这条）只入库
不触发——哨兵喂进来的持仓风险事件 committee_task_id 全 NULL，一次委员会都没跑过。

频控（两门共享，状态落 memory/.state/event_committee_triggers.json，同
price_sentinel 冷却先例）：
- 冷却：同 symbol 触发后 event.committee_cooldown_hours（默认 12h）内不重跑；
  但新事件 severity **严格高于**开冷却那条时越级放行（event.committee_escalation_bypass，
  默认开）——回放发现 sev-2 前瞻开的冷却会吞掉随后的 sev-3 实锤
- 上限：任意滚动 24h 内事件触发的委员会 ≤ event.committee_daily_cap（默认 4），
  按 symbol 计——一个多 symbol task 算 N 个（LLM 花费按 symbol 走）；额度紧时高 severity 先占
- 额度在 HTTP 触发**之前**于文件锁内预占，触发失败回滚（两门并发不互相覆盖）
报警沿用 send_event_alert 现行策略（默认静默，INVEST_EVENT_ALERT=1 恢复；非 HOLD
verdict 通知在委员会跑完后由 web 路径发）。

顾问模式（INVEST_ADVISORY_MODE）一律不触发、不报警：顾问实例的 ingest_event 对
群聊陌生人放行，不能让陌生人驱动 LLM 花费 / 推送（且 _trigger_committee 默认打
本机 8765 = 生产 hub）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openinvest.services.event_notifier import send_event_alert
from openinvest.utils.advisory import is_advisory_mode

log = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 1, "mid": 2, "high": 3}
_STATE_NAME = "event_committee_triggers"
_CAP_WINDOW = timedelta(hours=24)


# ---------- 纯函数（可单测；experiments/event-gate-replay-2026-10 回放复用） ----------

def is_triggerable(ev: Dict[str, Any], watched_lower: Iterable[str], min_severity: str) -> bool:
    """单条新事件过闸：severity ≥ min_severity + stance ≠ neutral + 命中关注 symbol（大小写不敏感）。"""
    if _SEVERITY_RANK.get(ev.get("severity"), 1) < _SEVERITY_RANK.get(min_severity, 2):
        return False
    if ev.get("stance") == "neutral":
        return False
    return bool({s.lower() for s in (ev.get("affected_symbols") or [])} & set(watched_lower))


def _parse(ts: Any, now: datetime) -> Optional[datetime]:
    """状态里的 ISO 时刻 → aware datetime。缺时区按 UTC；解析失败 / 晚于 now（手改、
    时钟回拨）→ None 当没记录——状态坏了宁可多跑一次，也别让触发路径抛。"""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt if dt <= now else None


def _last(rec: Any, now: datetime) -> Tuple[Optional[datetime], int]:
    """state["last"][sym] → (开冷却时刻, 开冷却那条的 severity rank)。
    兼容早期纯 ISO 字符串格式：rank 未知按 high 处理（不给越级，保守省钱）。"""
    if isinstance(rec, dict):
        sev = rec.get("sev")
        return _parse(rec.get("ts"), now), sev if isinstance(sev, int) else 3
    return _parse(rec, now), 3


def admit(
    state: Any,
    candidates: List[Tuple[str, int]],
    now: datetime,
    *,
    cooldown_hours: int,
    daily_cap: int,
    escalation_bypass: bool = True,
) -> Tuple[List[str], Dict[str, Any]]:
    """冷却 + 滚动 24h 上限过滤。candidates = [(symbol, 本批该 symbol 最高 severity rank)]。
    返回 (放行 symbols, 放行后的新 state)。

    state = {"last": {symbol: {"ts": iso, "sev": rank}}, "runs": [iso, ...]}；runs 每个
    放行 symbol 记一条，只留 24h 窗口内的。额度紧时按 severity 降序先到先得（同级保持
    调用方顺序）。冷却期内 severity 严格升级 → escalation_bypass 开时越级放行（仍受上限）。
    """
    state = state if isinstance(state, dict) else {}
    last = dict(state["last"]) if isinstance(state.get("last"), dict) else {}
    runs = state["runs"] if isinstance(state.get("runs"), list) else []
    runs = [t for t in runs if (p := _parse(t, now)) is not None and now - p < _CAP_WINDOW]
    cooldown = timedelta(hours=cooldown_hours)
    iso = now.isoformat(timespec="seconds")
    admitted: List[str] = []
    for sym, sev in sorted(candidates, key=lambda c: -c[1]):
        if len(runs) + len(admitted) >= daily_cap:
            break
        prev, prev_sev = _last(last.get(sym), now)
        if prev is not None and now - prev < cooldown and not (escalation_bypass and sev > prev_sev):
            continue
        admitted.append(sym)
        last[sym] = {"ts": iso, "sev": sev}
    return admitted, {"last": last, "runs": runs + [iso] * len(admitted)}


# ---------- IO（monkeypatch 点） ----------

def _watched_symbols() -> List[str]:
    """holdings ∪ target_assets（canonical 写法）。PM 不可用 → []（= 什么都不触发）。"""
    try:
        from openinvest.core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
    except Exception as e:
        log.warning(f"PortfolioManager 不可用，事件触发闸按空关注列表处理: {e}")
        return []
    held = [h.get("symbol") for h in pm.holdings.all() if h.get("symbol")]
    targets = [a.get("symbol") for a in (pm.strategy.get("target_assets") or []) if a.get("symbol")]
    return list(dict.fromkeys(held + targets))


def _holdings_snapshot(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """给邮件正文用：每个受影响 symbol 当前 units / 现价 / pnl

    现价必须经 get_quote(holding) 拿——它按 holding 的 proxy_kind/cost_currency
    把行情换算成与 avg_cost 同币种、同单位的价格（如黄金积存金：金期货 USD/oz
    经 /31.1035*USDCNY 反推成 CNY/克）。早期直接用 get_history_data 拿原始
    期货价（USD/oz），跟 CNY/克 成本相除，把真实浮亏 -1% 算成了 +348%——
    币种/单位错配 bug，统一走 get_quote 后根除。
    """
    try:
        from openinvest.core.portfolio_manager import PortfolioManager
        from openinvest.utils.quotes import get_quote
        pm = PortfolioManager()
    except Exception:
        return {}

    snap: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        h = pm.holdings.find(sym)
        if not h:
            continue
        units = float(h.get("units", 0) or 0)
        avg_cost = float(h.get("avg_cost", 0) or 0)
        try:
            quote = get_quote(h)
        except Exception:
            quote = None
        entry: Dict[str, Any] = {"units": units}
        if quote is not None:
            # price 与 avg_cost 现在保证同币种同单位，相除才有意义
            entry["price"] = round(quote.price, 2)
            entry["currency"] = quote.currency
            # 追踪仓 / 无成本不算 P&L，只报现价（对齐 web_api._build_holding_v2）
            if not h.get("is_tracking_only") and units > 0:
                entry["mv"] = quote.price * units
                if avg_cost > 0:
                    entry["pnl_pct"] = (quote.price / avg_cost) - 1.0
        snap[sym] = entry
    return snap


def _trigger_committee(symbols: List[str], event_ids: List[str]) -> Optional[str]:
    """调本机 web_api /api/committee/run 触发委员会重跑（web 路径走 run_committee_session）。返回 task_id"""
    if not symbols:
        return None
    import requests
    from openinvest.core.config import load_config
    base = os.getenv("INVEST_EVENT_API_URL", "http://127.0.0.1:8765")
    # hub 开了 INVEST_API_TOKEN 时 loopback 也要带（#106 起 token 全域强制）
    _tok = os.getenv("INVEST_API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {_tok}"} if _tok else {}
    try:
        r = requests.post(
            f"{base.rstrip('/')}/api/committee/run",
            headers=headers,
            json={
                "symbols": symbols,
                "max_debate_rounds": load_config().event.max_rounds,
                "note": f"triggered by event_trigger event_ids={','.join(event_ids[:4])}",
                "event_ids": event_ids,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("task_id")
    except Exception as e:
        log.warning(f"trigger committee 失败: {type(e).__name__}: {e}")
        return None


# ---------- 主流程 ----------

def trigger_for_new_events(
    new_events: List[Dict[str, Any]],
    *,
    store: Any,
    watched: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """对一批**新入库**事件（须含 event_id）过闸 → 冷却/上限 → 触发委员会 → 报警。

    Args:
        new_events: upsert 返回 was_new 的事件（重复事件不该再触发）
        store: EventStore（取 sources 给报警、mark_committee_task 打链接）
        watched: holdings ∪ target_assets；None → 现读 PortfolioManager
        dry_run: 只算闸，不触发不报警不落冷却

    Returns:
        {triggered: 过闸事件数, affected_symbols, committee_symbols: 冷却/上限后真跑的,
         committee_task_id}
    """
    if is_advisory_mode():
        log.info(f"[event_trigger] 顾问模式：{len(new_events)} 条新事件只入库，不触发不报警")
        return {"triggered": 0, "affected_symbols": [], "committee_symbols": [],
                "committee_task_id": None}

    from openinvest.core.config import load_config
    # 长驻进程（scheduler / MCP）必须强制重读，否则看不到 API 改的冷却/上限（同 price_sentinel）
    cfg = load_config(_force_reload=True).event
    if watched is None:
        watched = _watched_symbols()
    canonical = {s.lower(): s for s in watched}

    triggerable: List[Dict[str, Any]] = []
    sev_by_sym: Dict[str, int] = {}  # 本批每个命中 symbol 的最高 severity（越级 / 额度排序用）
    for ev in new_events:
        if not is_triggerable(ev, canonical.keys(), cfg.min_severity):
            continue
        triggerable.append({**ev, "sources": store.get_sources(ev["event_id"])})
        rank = _SEVERITY_RANK.get(ev.get("severity"), 1)
        # 大小写不敏感匹配 + 映射回 canonical 写法：LLM 归一化吐出小写 ticker 时
        # 也要触发 canonical 写法的委员会（不然邮件发了委员会静默不跑）
        for s in ev.get("affected_symbols") or []:
            if s.lower() in canonical:
                c = canonical[s.lower()]
                sev_by_sym[c] = max(sev_by_sym.get(c, 0), rank)
    affected = sorted(sev_by_sym)
    out: Dict[str, Any] = {"triggered": len(triggerable), "affected_symbols": affected,
                           "committee_symbols": [], "committee_task_id": None}
    if not triggerable or dry_run:
        return out

    from openinvest.core.memory_store import MemoryStore
    ms = MemoryStore()
    now = datetime.now(timezone.utc)
    reserved: Dict[str, Any] = {}

    def _reserve(cur: Any):
        # 文件锁内 read → admit → write：额度在 HTTP 触发前就占下，两门并发不互相覆盖
        admitted, new_state = admit(
            cur, [(s, sev_by_sym[s]) for s in affected], now,
            cooldown_hours=cfg.committee_cooldown_hours, daily_cap=cfg.committee_daily_cap,
            escalation_bypass=cfg.committee_escalation_bypass)
        prev_last = cur.get("last") if isinstance(cur, dict) and isinstance(cur.get("last"), dict) else {}
        reserved.update(admitted=admitted, mine={s: new_state["last"][s] for s in admitted},
                        prev={s: prev_last.get(s) for s in admitted})
        return new_state, admitted

    def _release(cur: Any):
        # 触发失败：只撤回本次预占（别人期间写入的记录不动），让下一条事件能重试
        cur = cur if isinstance(cur, dict) else {}
        last = dict(cur["last"]) if isinstance(cur.get("last"), dict) else {}
        runs = list(cur["runs"]) if isinstance(cur.get("runs"), list) else []
        for sym, mine in reserved["mine"].items():
            if last.get(sym) == mine:
                if reserved["prev"][sym] is None:
                    last.pop(sym, None)
                else:
                    last[sym] = reserved["prev"][sym]
            if mine["ts"] in runs:
                runs.remove(mine["ts"])
        return {"last": last, "runs": runs}, None

    admitted = ms.state_update(_STATE_NAME, _reserve)
    if admitted:
        adm = {s.lower() for s in admitted}
        fed = [ev for ev in triggerable
               if {s.lower() for s in (ev.get("affected_symbols") or [])} & adm]
        task_id = _trigger_committee(symbols=admitted, event_ids=[ev["event_id"] for ev in fed])
        if task_id:
            for ev in fed:
                store.mark_committee_task(ev["event_id"], task_id)
            out.update(committee_symbols=admitted, committee_task_id=task_id)
        else:
            ms.state_update(_STATE_NAME, _release)
    if set(affected) - set(admitted):
        log.info(f"[event_trigger] 冷却/日上限拦下 {sorted(set(affected) - set(admitted))}")

    # 持仓快照要拉行情：只在报警真会发时才算（send_event_alert 默认静默，判据同它内部）
    alerts_on = os.getenv("INVEST_EVENT_ALERT", "0") == "1"
    try:
        send_event_alert(
            triggerable,
            committee_task_id=out["committee_task_id"],
            holdings_snapshot=_holdings_snapshot(affected) if alerts_on else {},
        )
    except Exception as e:
        log.warning(f"send_event_alert 失败: {type(e).__name__}: {e}")
    return out
