"""EVENT_STANCE 聚合公式验证 — 确定性，0 LLM，可重跑。

回答一个问题：severity 加权 + 时效衰减的 EVENT_STANCE 净信号，是否比纯计数
（现状）更能预测前向收益？没有证据 → 加权公式保持禁用（默认等权+无衰减，
见 core/config defaults.yaml sentiment 节注释，ADR-010 rule 4 纪律）。

方法（全部确定性）：
1. events.db 全量事件（ts/stance/severity/affected_symbols）
2. 对每个资产构造逐日净信号：当日往回 7 天窗（对齐 event_store.recall 默认窗）
   内的相关事件，按 config 网格算 score = Σ sign × w(severity) × 0.5^(age_h/half_life)
   - sign: opportunity=+1, risk=-1, neutral=0
   - half_life=0 → 不衰减；w 全 1 + 不衰减 = 纯计数（现状基线）
3. 信号质量：净信号方向 vs 1/5/10 个交易日前向收益的方向命中率 +
   score↔前向收益 Spearman 秩相关
4. 诚实闸门：信号日数 / 时间跨度 / 带标注事件数不达标 → 判 INSUFFICIENT_DATA，
   不许拍脑袋设权重

两种相关性口径：
- market：全部事件都计入（EVENT_STANCE 市场级行的口径）
- tagged：仅 affected_symbols 含该资产的事件（per-asset 行的口径）

用法：
    uv run python scripts/eval_event_stance.py
    uv run python scripts/eval_event_stance.py --assets GC=F NDQ.AX --report /tmp/r.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.db.market_store import MarketStore  # noqa: E402

EVENTS_DB = ROOT / "db" / "events.db"
WINDOW_DAYS = 7          # 对齐 event_store.recall 默认时间窗
HORIZONS = (1, 5, 10)    # 前向收益交易日

# 诚实闸门：低于任一阈值 → INSUFFICIENT_DATA
MIN_SIGNAL_DAYS = 60     # 有非中性净信号的交易日数
MIN_SPAN_DAYS = 90       # 事件历史日历跨度
MIN_TAGGED_EVENTS = 100  # tagged 口径的最少带标注事件数

# config 网格：第一个 = 纯计数（现状基线）
GRID: List[Tuple[str, Dict[str, float]]] = [
    ("counting(现状)", {"w": (1, 1, 1), "half_life_h": 0.0}),
    ("w123_无衰减", {"w": (1, 2, 3), "half_life_h": 0.0}),
    ("w123_hl24h", {"w": (1, 2, 3), "half_life_h": 24.0}),
    ("w123_hl48h", {"w": (1, 2, 3), "half_life_h": 48.0}),
    ("w123_hl96h", {"w": (1, 2, 3), "half_life_h": 96.0}),
    ("w124_hl48h", {"w": (1, 2, 4), "half_life_h": 48.0}),
]

_SIGN = {"opportunity": 1.0, "risk": -1.0, "neutral": 0.0}


@dataclass
class Ev:
    ts: datetime
    sign: float
    severity: int           # 1/2/3
    symbols: Tuple[str, ...]


def _parse_ts(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_events(db_path: Path = EVENTS_DB) -> List[Ev]:
    con = sqlite3.connect(str(db_path))
    out: List[Ev] = []
    for ts_raw, stance, sev, syms_json in con.execute(
        "SELECT ts, stance, severity, affected_symbols_json FROM events"
    ):
        ts = _parse_ts(ts_raw)
        if ts is None:
            continue
        try:
            syms = tuple(s.upper() for s in (json.loads(syms_json or "[]") or []))
        except Exception:  # noqa: BLE001
            syms = ()
        out.append(Ev(ts=ts, sign=_SIGN.get(stance, 0.0),
                      severity=int(sev), symbols=syms))
    con.close()
    return out


def _score(events: List[Ev], asof: datetime, w: Tuple[float, float, float],
           half_life_h: float) -> Tuple[float, int]:
    """7 天窗内事件的加权净分 + 参与事件数。LLM 错标未来 ts → age clamp 0。"""
    score, n = 0.0, 0
    lo = asof - timedelta(days=WINDOW_DAYS)
    for e in events:
        if not (lo <= e.ts <= asof + timedelta(days=1)):
            # +1d 容忍"未来几小时"的轻微错标；更远的未来事件不计
            continue
        n += 1
        if e.sign == 0.0:
            continue
        weight = w[min(max(e.severity, 1), 3) - 1]
        if half_life_h > 0:
            age_h = max(0.0, (asof - e.ts).total_seconds() / 3600.0)
            weight *= 0.5 ** (age_h / half_life_h)
        score += e.sign * weight
    return score, n


def eval_asset(asset: str, events: List[Ev]) -> List[dict]:
    """对一个资产逐日评估全部 config × {market, tagged} 口径。"""
    df = MarketStore().get_history_df(asset, days=20000)
    if df is None or df.empty or "Close" not in df:
        return [{"asset": asset, "error": "无行情数据"}]
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close[~close.index.duplicated(keep="last")].dropna()

    # 稳健跨度：取 ts 的 5%-95% 分位区间——LLM 偶发错标 2019/未来日期，
    # min/max 会把跨度虚标成几年（实际密集区只有 5-6 周）
    ts_sorted = sorted(e.ts for e in events)
    span_lo = ts_sorted[int(len(ts_sorted) * 0.05)]
    span_hi = ts_sorted[int(len(ts_sorted) * 0.95) - 1]
    span_days = (span_hi - span_lo).days
    tagged = [e for e in events if asset.upper() in e.symbols]

    # 评估交易日：事件跨度内 + 留足 10 个交易日 lookahead
    idx = close.index
    if idx.tz is None:
        day_lo = span_lo.replace(tzinfo=None)
        day_hi = span_hi.replace(tzinfo=None)
    else:
        day_lo, day_hi = span_lo, span_hi
    days = [d for d in idx if day_lo <= d <= day_hi]
    days = [d for d in days if idx.get_loc(d) + max(HORIZONS) < len(idx)]

    results = []
    for mode, pool in (("market", events), ("tagged", tagged)):
        for name, cfg in GRID:
            rows = []
            for d in days:
                asof = d.to_pydatetime()
                if asof.tzinfo is None:
                    asof = asof.replace(tzinfo=timezone.utc)
                score, n_ev = _score(pool, asof, cfg["w"], cfg["half_life_h"])
                if n_ev == 0:
                    continue
                i = idx.get_loc(d)
                fwd = {h: float(close.iloc[i + h] / close.iloc[i] - 1)
                       for h in HORIZONS}
                rows.append({"score": score, **{f"fwd_{h}d": fwd[h] for h in HORIZONS}})
            r = pd.DataFrame(rows)
            res = {"asset": asset, "mode": mode, "config": name,
                   "days_with_events": len(r),
                   "signal_days": 0 if r.empty else int((r["score"] != 0).sum()),
                   "span_days": span_days, "tagged_events": len(tagged)}
            if not r.empty:
                sig = r[r["score"] != 0]
                for h in HORIZONS:
                    col = f"fwd_{h}d"
                    if len(sig):
                        hit = (np.sign(sig["score"]) == np.sign(sig[col])).mean()
                        res[f"hit_{h}d"] = round(float(hit), 3)
                    # Spearman = ranks 上的 Pearson（无 scipy 依赖）
                    res[f"rho_{h}d"] = round(
                        float(r["score"].rank().corr(r[col].rank())), 3,
                    ) if len(r) >= 5 else None
            results.append(res)
    return results


def sufficiency_verdict(results: List[dict]) -> Tuple[bool, List[str]]:
    """诚实闸门：任一口径数据不足 → 不许下'换公式'结论。"""
    reasons = []
    for r in results:
        if "error" in r:
            reasons.append(f"{r['asset']}: {r['error']}")
            continue
        key = f"{r['asset']}/{r['mode']}"
        if r["span_days"] < MIN_SPAN_DAYS:
            reasons.append(f"{key}: 事件史跨度 {r['span_days']}d < {MIN_SPAN_DAYS}d")
        if r["signal_days"] < MIN_SIGNAL_DAYS:
            reasons.append(f"{key}: 信号日 {r['signal_days']} < {MIN_SIGNAL_DAYS}")
        if r["mode"] == "tagged" and r["tagged_events"] < MIN_TAGGED_EVENTS:
            reasons.append(
                f"{key}: 带标注事件 {r['tagged_events']} < {MIN_TAGGED_EVENTS}")
    reasons = list(dict.fromkeys(reasons))
    return (len(reasons) == 0), reasons


def main() -> None:
    ap = argparse.ArgumentParser(description="EVENT_STANCE 加权公式验证（确定性）")
    ap.add_argument("--assets", nargs="+", default=["GC=F", "NDQ.AX"],
                    help="评估资产（默认拿当前持仓 spot-check；驱动链本身不硬编码）")
    ap.add_argument("--report", default=None, help="可选 markdown 报告输出路径")
    args = ap.parse_args()

    events = load_events()
    print(f"事件总数: {len(events)}  "
          f"跨度: {min(e.ts for e in events):%Y-%m-%d} → "
          f"{max(e.ts for e in events):%Y-%m-%d}")

    all_results: List[dict] = []
    for asset in args.assets:
        all_results.extend(eval_asset(asset, events))

    out_lines = []
    cols = (["asset", "mode", "config", "signal_days"]
            + [f"hit_{h}d" for h in HORIZONS] + [f"rho_{h}d" for h in HORIZONS])
    table = pd.DataFrame([r for r in all_results if "error" not in r])
    if not table.empty:
        out_lines.append(table[cols].to_string(index=False))

    ok, reasons = sufficiency_verdict(all_results)
    out_lines.append("")
    if ok:
        out_lines.append("VERDICT: 数据量达标 — 可按上表对比 counting vs 加权配置，"
                         "若某配置在多数 horizon 稳定占优可考虑启用（仍建议留出 OOS）。")
    else:
        out_lines.append("VERDICT: INSUFFICIENT_DATA — 保持默认禁用（等权+无衰减=纯计数）。")
        for r in reasons:
            out_lines.append(f"  - {r}")
        out_lines.append(f"  事件库每月 ~2000 条增长，预计 2-3 个月后重跑本脚本可下结论。")

    text = "\n".join(out_lines)
    print(text)
    if args.report:
        Path(args.report).write_text(
            f"# EVENT_STANCE 公式验证报告\n\n```\n{text}\n```\n", encoding="utf-8")
        print(f"\n报告已写入 {args.report}")


if __name__ == "__main__":
    main()
