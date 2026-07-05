"""价格异动哨兵（ADR-025）：盯"垂直线"，报警先行，委员会随后。

与 event_watch（新闻感知）共享同一事件管道（EventStore → 邮件 → 委员会重跑），
但感官不同：
- 感知：yfinance 5 分钟收盘价，10 分钟涨跌幅 vs 日 ATR%（纯算术，零 LLM）
- 时序契约（用户需求 2026-07-03："先报给我，再跑 committee"）：
  **先发报警邮件（含最近 verdict 锚点），再触发委员会重跑**——报警绝不被
  committee 路径拖慢或阻塞；委员会触发失败也不影响已发出的报警
- 冷却：同 symbol 同方向 cooldown_min 分钟内不重复报（memory/.state 持久化）

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
        if intraday is None or len(intraday) < _WINDOW_BARS + 1:
            return None
        closes = [float(v) for v in intraday["Close"].tolist()]
        last_bar_utc = intraday.index[-1].tz_convert("UTC").to_pydatetime()

        daily = t.history(period="3mo", interval="1d")
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


# ---------- 主流程 ----------

def run(dry_run: Optional[bool] = None) -> Dict[str, Any]:
    from openinvest.core.config import load_config
    from openinvest.core.memory_store import MemoryStore
    from openinvest.db.event_store import EventStore
    from openinvest.jobs.event_watch import _holdings_snapshot, _load_user_context, _trigger_committee
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
        try:
            task_id = _trigger_committee([sym], [eid])
            if task_id:
                store.mark_committee_task(eid, task_id)
        except Exception as e:
            log.warning(f"[{sym}] 委员会触发失败（报警已发出，不回滚）: {e}")

        state[f"{sym}:{hit['direction']}"] = now.isoformat(timespec="seconds")
        alerted.append(sym)
        # 每报警一个 symbol 立即落盘一次——不要攒到循环结束才写（dry_run 已在上面
        # continue 掉，走到这里必然是真实报警）。冷却状态是防重复报警的唯一闸门，
        # 立即持久化比"等全部处理完再一次性写"更安全：即便后面某个 symbol 抛出
        # 未预期异常导致 run() 提前退出，已经报过的 symbol 的冷却时间也不会丢。
        ms.state_set(_STATE_NAME, state)

    return {"status": "ok", "checked": len(symbols), "alerted": len(alerted),
            "symbols": alerted, "dry_run": dry_run}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
