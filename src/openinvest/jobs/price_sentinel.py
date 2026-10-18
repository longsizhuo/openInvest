"""价格异动哨兵（ADR-025）：盯"垂直线"，报警先行，委员会随后。

与 event_watch（新闻感知）共享同一事件管道（EventStore → 邮件 → 委员会重跑），
但感官不同：
- 感知：yfinance 5 分钟收盘价，10 分钟涨跌幅 vs 日 ATR%（纯算术，零 LLM）
- 时序契约（用户需求 2026-07-03："先报给我，再跑 committee"）：
  **先发报警邮件（含最近 verdict 锚点），再触发委员会重跑**——报警绝不被
  committee 路径拖慢或阻塞；委员会触发失败也不影响已发出的报警
- 冷却：同 symbol 同方向 cooldown_min 分钟内不重复报（memory/.state 持久化）

用户自设报警线（C8）：strategy.target_assets[].alerts（price_above / price_below / day_drop_pct /
day_rise_pct / drawdown_from_cost_pct）每个 tick 用最新价判一次，与上面的 ATR 逻辑并行、互不依赖：
- 价格口径同 status（services/price_alerts：持仓按 get_quote，黄金按克价），行情是回退兜底（is_stale）时不报
- 冷却用 state_update 在同一把文件锁内「判冷却 + 盖章」（原子，两个实例不会同报）：价格线每标的每条 24h 滚动；
  day_drop/day_rise 按**行情交易日**——同一根日线只报一次（休市日行情停在上一交易日那根线，按 24h 判
  会把周五的跌幅周六、周日各再报一遍），新的交易日才能再报
- 命中写一条事件进 events.db（event_type=price_alert），再经 send_event_alert 同一通道发
  （Discord/邮件）；INVEST_EVENT_ALERT=1 才真发，默认关时只入库不发（冷却照常盖章）
- **只入库、不触发委员会**：线是用户自己的提醒，不是新信息；委员会额度（共享冷却 + 日上限）留给新闻事件。
  severity=low 让 RAG 召回 / 日报资产事件 / 触发闸（默认都要 ≥ mid）天然看不见它，
  用户的价格线不会经事件层混进委员会的论据
- event.sentinel_enabled=false / INVEST_SENTINEL_DRY_RUN=1 对这条路径同样生效（整个 job 停 / 只检测不落任何东西）

数据现实：yfinance 期货/股票分钟线延迟 ~10-15 分钟，本哨兵定位是"系统先于
用户开口"的 FOMO 拦截 + 下跌侧衔接 DCA/防御哨兵，不是抢跑工具（ADR-023
诚实定位）。报警阈值用日 ATR% 归一化（10 分钟走完一根日常波动的大部分才叫
垂直线），固定百分比阈值在黄金这种日内 1-2% 常态品种上会天天响。

环境变量：
  INVEST_SENTINEL_DRY_RUN=1   只检测不发邮件不触发委员会（调试用）

config（ADR-017 白名单，GUI/API/CLI 可改）：
  event.sentinel_enabled       总开关（默认开）
  event.sentinel_atr_mult      触发倍数：|10min 涨跌| ≥ mult × 日ATR%（默认 0.8）
  event.sentinel_cooldown_min  同 symbol 同方向冷却分钟数（默认 120）
  event.sentinel_schedule      扫描窗口 crontab（默认同 event_watch 窗口，5 分钟一次）
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

# 10 分钟 = 2 根 5m bar；数据最后一根 bar 距今超过该分钟数视为闭市/停滞，跳过
_WINDOW_BARS = 2
_MAX_STALE_MIN = 25
# ATR 不可得时的绝对兜底阈值（百分比）
_ABS_FALLBACK_PCT = 1.0
# 冷却状态在 memory/.state 里的名字
_STATE_NAME = "price_sentinel_cooldowns"
# 用户自设报警线：每标的每条规则的冷却（键 "SYM:rule"；价格线值 = 盖章时刻，日涨跌线值 = {"bar", "ts"}）
_ALERT_STATE_NAME = "price_alert_cooldowns"
_ALERT_COOLDOWN_MIN = 24 * 60
_ALERT_UP_RULES = ("price_above", "day_rise_pct")  # 上行线 = 机会措辞，其余 = 风险


# ---------- 纯函数（可单测） ----------

def _detect_move(
    closes: List[float],
    atr_pct: Optional[float],
    mult: float,
) -> Optional[Dict[str, Any]]:
    """从 5m 收盘价序列检测 10 分钟异动。

    返回 {move_pct, ratio, direction} 或 None（不足以触发）。
    ratio = |move| / 日ATR%；ATR 缺失时按绝对兜底阈值判，ratio 记 None 表示未归一化
    （不能记 0.0——那会和"真算出 0 倍"混淆，且下游 severity 判断会把巨幅异动误判成
    最低档：ATR 缺失时的 severity 改用 move_pct 本身判断，见 run()）。
    """
    if len(closes) < _WINDOW_BARS + 1:
        return None
    prev, last = closes[-1 - _WINDOW_BARS], closes[-1]
    if not prev or not last:
        return None
    move_pct = (last / prev - 1.0) * 100.0
    if atr_pct and atr_pct > 0:
        ratio: Optional[float] = abs(move_pct) / atr_pct
        if ratio < mult:
            return None
    else:
        # ATR 拿不到（新上市/数据缺口）→ 绝对阈值兜底
        ratio = None
        if abs(move_pct) < _ABS_FALLBACK_PCT:
            return None
    return {
        "move_pct": move_pct,
        "ratio": ratio,
        "direction": "up" if move_pct > 0 else "down",
    }


def _cooldown_ok(
    state: Dict[str, Any],
    symbol: str,
    direction: str,
    now: datetime,
    cooldown_min: int,
) -> bool:
    """同 symbol 同方向在冷却期内 → False。急涨后急跌属不同方向，各自可报。"""
    rec = (state or {}).get(f"{symbol}:{direction}")
    if not rec:
        return True
    try:
        last = datetime.fromisoformat(rec)
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= cooldown_min * 60


def _latest_verdict(symbol: str, committee_root: Optional[Path] = None) -> str:
    """读最近一天 transcript 的 verdict 行做报警锚点。拿不到给中性文案，绝不抛。"""
    try:
        root = committee_root or INVEST_ROOT / "memory" / ".committee"
        fname = re.sub(r"[^A-Za-z0-9]", "_", symbol) + ".md"
        candidates = sorted(root.glob(f"*/{fname}"), reverse=True)  # 目录名是 ISO 日期,倒序=最新
        if not candidates:
            return "近期无委员会 verdict"
        text = candidates[0].read_text(encoding="utf-8", errors="ignore")[:2000]
        m = re.search(r"\*\*Verdict\*\*:\s*(\w+)[^\n]*", text)
        date = candidates[0].parent.name
        return f"最近委员会 verdict {m.group(1)}（{date}）" if m else f"最近委员会记录 {date}"
    except Exception as e:  # 锚点只是锦上添花，任何异常不拦报警
        log.warning(f"[{symbol}] 读最近 verdict 失败: {e}")
        return "近期无委员会 verdict"


# ---------- 数据抓取（monkeypatch 点） ----------

def _fetch_frames(symbol: str) -> Optional[Dict[str, Any]]:
    """拉 5m 收盘序列 + 日 ATR%。返回 {closes, last_bar_utc, price, atr_pct} 或 None。"""
    try:
        import yfinance as yf
        from openinvest.utils.market_metrics import compute_metrics

        t = yf.Ticker(symbol)
        intraday = t.history(period="1d", interval="5m")
        # 末根 Close=NaN（半成型 bar）会让涨跌幅变 NaN、比较全为 False → 这一轮静默不报
        intraday = intraday.dropna(subset=["Close"]) if intraday is not None else None
        if intraday is None or len(intraday) < _WINDOW_BARS + 1:
            return None
        closes = [float(v) for v in intraday["Close"].tolist()]
        last_bar_utc = intraday.index[-1].tz_convert("UTC").to_pydatetime()

        daily = t.history(period="3mo", interval="1d")
        daily = daily.dropna(subset=["Close"]) if daily is not None and len(daily) else daily  # 同上，否则 ATR% 变 None、退回绝对阈值
        atr_pct = compute_metrics(daily).get("atr_pct") if daily is not None and len(daily) else None
        return {
            "closes": closes,
            "last_bar_utc": last_bar_utc,
            "price": closes[-1],
            "atr_pct": atr_pct,
        }
    except Exception as e:
        log.warning(f"[{symbol}] 行情抓取失败: {e}")
        return None


# ---------- 用户自设报警线（C8） ----------

def _alert_targets() -> List[Dict[str, Any]]:
    """strategy.target_assets 里设了 alerts 的标的 → [{symbol, alerts, holding}]。PM 不可用 → []。"""
    try:
        from openinvest.core.portfolio_manager import PortfolioManager

        pm = PortfolioManager()
        return [
            {"symbol": a["symbol"], "alerts": a["alerts"], "holding": pm.holdings.find(a["symbol"])}
            for a in (pm.strategy.get("target_assets") or []) if a.get("symbol") and a.get("alerts")
        ]
    except Exception as e:  # 没初始化 / 读失败：这条路径静默让位，不影响 ATR 哨兵
        log.warning(f"[price_alert] 读 target_assets 失败，本轮跳过用户报警线: {e}")
        return []


def _claim_alert(ms: Any, sym: str, rule: str, now: datetime, bar: Optional[str] = None) -> Any:
    """原子冷却：同一把文件锁内「读 → 判冷却 → 盖章」。抢到返回盖的章（给 _release_alert 认领），冷却内返回 None。
    bar=None（价格线）：24h 滚动。bar=行情交易日（日涨跌线）：该交易日已报过就不再报，换了交易日才再报。"""
    stamp = now.isoformat(timespec="seconds")
    key = f"{sym}:{rule}"

    def _fn(cur: Any):
        cur = cur if isinstance(cur, dict) else {}
        if bar is None:
            if not _cooldown_ok(cur, sym, rule, now, _ALERT_COOLDOWN_MIN):
                return cur, None
            mark: Any = stamp
        else:
            prev = cur.get(key)
            if isinstance(prev, dict) and prev.get("bar") == bar:
                return cur, None
            mark = {"bar": bar, "ts": stamp}
        return {**cur, key: mark}, mark

    return ms.state_update(_ALERT_STATE_NAME, _fn)


def _release_alert(ms: Any, sym: str, rule: str, mark: Any) -> None:
    """入库失败时撤回刚盖的章（只撤自己的，别人期间写的不动），下个 tick 重试。"""
    key = f"{sym}:{rule}"
    ms.state_update(_ALERT_STATE_NAME, lambda cur: (
        {k: v for k, v in (cur if isinstance(cur, dict) else {}).items() if not (k == key and v == mark)}, None))


def _check_user_alerts(ms: Any, now: datetime, dry_run: bool) -> List[str]:
    """判所有带 alerts 的 target_asset，命中即冷却 → 入库 → 一条 digest 报警。返回命中的 "SYM:rule"。"""
    from openinvest.db.event_store import EventStore
    from openinvest.services.event_notifier import send_event_alert
    from openinvest.services.price_alerts import evaluate_symbol, fetch_quote

    fired: List[str] = []
    events: List[Dict[str, Any]] = []
    for t in _alert_targets():
        sym = t["symbol"]
        try:
            quote = fetch_quote(sym, t["holding"])
            if quote is None or not (quote.price > 0):
                continue
            if quote.is_stale:  # 行情源挂了在用库里旧价：别拿旧价当"触线"
                log.info(f"[{sym}] 行情是回退兜底（is_stale），跳过报警线判定")
                continue
            hits = [r for r in evaluate_symbol(sym, t["alerts"], t["holding"], price=quote.price,
                                               extra=quote.extra, last_updated=quote.last_updated)
                    if r["breached"]]
        except Exception as e:
            log.warning(f"[{sym}] 报警线判定失败: {e}")
            continue
        for r in hits:
            rule = r["rule"]
            if dry_run:
                fired.append(f"{sym}:{rule}")
                continue
            mark = _claim_alert(ms, sym, rule, now, r.get("bar_date"))
            if mark is None:
                log.info(f"[{sym}] {rule} 在冷却内（价格线 24h / 日涨跌线同一交易日），跳过")
                continue
            ccy = f" {quote.currency}" if quote.currency else ""
            claim = (f"{sym} 触发你设的报警线：{r['text']}，现价 {quote.price:.2f}{ccy}；{_latest_verdict(sym)}"
                     f"（{now:%Y-%m-%d}）")
            event = {
                "one_line_claim": claim,
                # 显式 event_id：默认的 claim_to_event_id 会剥掉 ISO 日期，同价同线隔天命中会被并成一行。
                # 价格线按盖章时刻、日涨跌线按行情交易日——每次命中一行，且不会和新闻事件撞 id
                "event_id": hashlib.sha256(f"price_alert|{sym}|{rule}|{r.get('bar_date') or mark}".encode()).hexdigest()[:16],
                "event_type": "price_alert",
                "stance": "opportunity" if rule in _ALERT_UP_RULES else "risk",
                "severity": "low",  # 见模块 docstring：低于所有事件层闸，用户的线不进委员会论据
                "affected_symbols": [sym],
                "entities": ["price_alert", sym.lower(), rule],
                "ts": now.isoformat(timespec="seconds"),
                "ingested_by": "price_sentinel",
            }
            try:
                _, eid = EventStore().upsert_event(event, embedding=None)
            except Exception as e:
                log.warning(f"[{sym}] {rule} 事件入库失败，撤回冷却下轮重试: {e}")
                _release_alert(ms, sym, rule, mark)
                continue
            log.info(f"[price_alert] {claim}")
            events.append({**event, "event_id": eid})
            fired.append(f"{sym}:{rule}")

    if events:
        try:
            # 持仓快照要拉行情：只在报警真会发时才算（send_event_alert 总开关默认关，判据同它内部）
            snap = {}
            if os.getenv("INVEST_EVENT_ALERT", "0") == "1":
                from openinvest.services.event_trigger import _holdings_snapshot
                snap = _holdings_snapshot(sorted({e["affected_symbols"][0] for e in events}))
            send_event_alert(events, committee_task_id=None, holdings_snapshot=snap)
        except Exception as e:
            log.warning(f"[price_alert] 报警发送失败（事件已入库、冷却已盖章）: {e}")
    return fired


# ---------- 主流程 ----------

def run(dry_run: Optional[bool] = None) -> Dict[str, Any]:
    from openinvest.core.config import load_config
    from openinvest.core.memory_store import MemoryStore
    from openinvest.db.event_store import EventStore
    from openinvest.jobs.event_watch import _load_user_context
    from openinvest.services.event_trigger import _holdings_snapshot, _trigger_committee
    from openinvest.services.event_notifier import send_event_alert

    if dry_run is None:
        dry_run = os.getenv("INVEST_SENTINEL_DRY_RUN", "").lower() in ("1", "true")

    # 长驻 scheduler 进程必须强制重读，否则命中旧缓存看不到 API 改动（同 dca_daily 先例）
    cfg = load_config(_force_reload=True)
    if not cfg.event.sentinel_enabled:
        return {"status": "disabled", "checked": 0, "alerted": 0}

    ctx = _load_user_context()
    symbols = list(dict.fromkeys([*ctx["holdings"], *ctx["watching"]]))
    if not symbols:
        return {"status": "ok", "checked": 0, "alerted": 0}

    ms = MemoryStore()
    state: Dict[str, Any] = ms.state_get(_STATE_NAME, {}) or {}
    now = datetime.now(timezone.utc)
    alerted: List[str] = []

    # 用户自设报警线先判：与 ATR 并行、互不依赖，任何异常都不拦后面的 ATR 逻辑
    try:
        price_alerts = _check_user_alerts(ms, now, dry_run)
    except Exception as e:
        log.warning(f"[price_alert] 用户报警线整体失败（不影响 ATR 哨兵）: {e}")
        price_alerts = []

    for sym in symbols:
        frames = _fetch_frames(sym)
        if frames is None:
            continue
        # 闭市/数据停滞：最后一根 bar 太旧就别拿昨天的尾巴当异动
        age_min = (now - frames["last_bar_utc"]).total_seconds() / 60
        if age_min > _MAX_STALE_MIN:
            continue
        hit = _detect_move(frames["closes"], frames["atr_pct"], cfg.event.sentinel_atr_mult)
        if hit is None:
            continue
        if not _cooldown_ok(state, sym, hit["direction"], now, cfg.event.sentinel_cooldown_min):
            log.info(f"[{sym}] 异动 {hit['move_pct']:+.2f}% 在冷却期内，跳过")
            continue

        verb = "急涨" if hit["direction"] == "up" else "急跌"
        if hit["ratio"] is not None:
            ratio_txt = f"{hit['ratio']:.1f}× 日ATR"
            # ATR 归一化：≥1.5× 日常波动才算 high
            severity = "high" if hit["ratio"] >= 1.5 else "mid"
        else:
            ratio_txt = "ATR 缺失,绝对阈值"
            # ATR 不可得时按绝对涨跌幅本身判 severity，不能读 ratio（已是 None，
            # 不再是旧版的哨兵值 0.0——旧版会把巨幅异动错判成最低档 mid）
            severity = "high" if abs(hit["move_pct"]) >= _ABS_FALLBACK_PCT * 3 else "mid"
        claim = (
            f"{sym} 10 分钟{verb} {hit['move_pct']:+.2f}%（{ratio_txt}），"
            f"现价 {frames['price']:.2f}；{_latest_verdict(sym)}"
        )
        event = {
            "one_line_claim": claim,
            "event_type": "price_action",
            # 急跌=风险（接 DCA/防御），急涨=机会措辞但语义上主要是 FOMO 拦截
            "stance": "risk" if hit["direction"] == "down" else "opportunity",
            "severity": severity,
            "affected_symbols": [sym],
            "entities": ["price_action", sym.lower()],
            "ts": now.isoformat(timespec="seconds"),
        }
        log.info(f"[price_sentinel] {claim}")

        if dry_run:
            alerted.append(sym)
            continue

        # 发报警前对**最新**冷却状态再核一次（run 开头读的 state 可能已过期）：本 job 有墙钟超时
        # （scheduler/runner.py），超时被放弃的旧实例若在新实例已报警之后才醒来，它手里的 state
        # 快照还是旧的，会把同一次异动再报一遍。仍保持"先报警后落冷却"的至少一次语义，只是把
        # 过期快照的重复窗口收窄到两个实例同时在途的几秒内。
        if not _cooldown_ok(ms.state_get(_STATE_NAME, {}) or {}, sym, hit["direction"], now,
                            cfg.event.sentinel_cooldown_min):
            log.info(f"[{sym}] 异动 {hit['move_pct']:+.2f}% 刚被另一实例报过，跳过")
            continue

        try:
            store = EventStore()
            _, eid = store.upsert_event(event, embedding=None)  # 纯价格事件无文本向量
        except Exception as e:
            # EventStore 写入失败：既没发报警也没触发委员会，本 symbol 这轮直接
            # 跳过（不落冷却，下个 5min tick 会重试）——不能让它中断整个 for 循环，
            # 否则本轮已经成功报警、冷却状态还没来得及处理的其它 symbol 会被一起
            # 拖下水（异常直接冒出 run()，跳过它们后面的冷却落盘）。
            log.warning(f"[{sym}] EventStore 写入失败，本轮跳过: {e}")
            continue
        ev_email = {**event, "event_id": eid}

        # === 时序契约：报警永远先于委员会（且不因委员会失败而丢失） ===
        try:
            send_event_alert([ev_email], committee_task_id=None,
                             holdings_snapshot=_holdings_snapshot([sym]))
        except Exception as e:
            log.warning(f"[{sym}] 报警邮件发送失败: {e}")
        # 委员会只能跑 target_assets（session 拒绝其它 symbol）：只在持仓的标的只报警
        if sym not in ctx["watching"]:
            log.info(f"[{sym}] 不在 target_assets，只报警不跑委员会")
        else:
            try:
                task_id = _trigger_committee([sym], [eid])
                if task_id:
                    store.mark_committee_task(eid, task_id)
            except Exception as e:
                log.warning(f"[{sym}] 委员会触发失败（报警已发出，不回滚）: {e}")

        alerted.append(sym)
        # 每报警一个 symbol 立即落盘一次——不要攒到循环结束才写（dry_run 已在上面
        # continue 掉，走到这里必然是真实报警）。冷却状态是防重复报警的唯一闸门，
        # 立即持久化比"等全部处理完再一次性写"更安全：即便后面某个 symbol 抛出
        # 未预期异常导致 run() 提前退出，已经报过的 symbol 的冷却时间也不会丢。
        # 只合并本 key（state_update 持锁 RMW），不整份回写 run 开头读的旧 state——
        # 否则被放弃的旧实例晚醒时会把新实例刚写的其它 symbol 冷却覆盖掉。
        _key, _stamp = f"{sym}:{hit['direction']}", now.isoformat(timespec="seconds")
        ms.state_update(
            _STATE_NAME,
            lambda cur, _k=_key, _v=_stamp: ({**(cur if isinstance(cur, dict) else {}), _k: _v}, None),
        )

    return {"status": "ok", "checked": len(symbols), "alerted": len(alerted),
            "symbols": alerted, "price_alerts": price_alerts, "dry_run": dry_run}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
