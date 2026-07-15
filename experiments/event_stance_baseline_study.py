"""事件研究基线：stance 标签有没有方向 alpha（issue #210 预注册口径固化）。

回答：opportunity/risk 标签的事件，事后前瞻收益是不是真的往标签暗示的方向走？

预注册口径（对齐 issue #210 / #212，不许赛后改）：
1. 数据：db/events.db 全量事件，展开成 (event × affected_symbol) 样本
2. anchor = created_at（入库时间=信息可得时点，不用 ts——ts 是 LLM 抽取的
   "事件发生时间"，可能早于我们真正拿到这条信息的时刻，用它算前瞻收益是
   会前视的）
3. 前瞻收益：calendar_days 口径（1/3/5 日），与 calc.regime_probability.
   forward_return 全系统同一算法（base=asof 前最后一根收盘，target=asof+N
   天后第一根收盘），未成熟窗口整条丢弃
4. 超额收益 = 该资产该窗事件后前瞻收益 − 该资产同期无条件基线（全历史同
   calendar_days 口径前瞻收益均值）——隔离资产自身趋势/漂移，不然"金价那
   两个月一直在涨"会被误记成"opportunity 事件带来正收益"
5. naive t（样本当独立观测）+ cluster-robust t（按 symbol 聚类，簇均值当
   独立观测的简化近似——不是完整 sandwich estimator，标注清楚不冒充精确
   推断，但足够判断"结论是不是被少数几个资产的重复出现撑起来的"）
6. n<30 的桶不报统计量（诚实闸门，照 scripts/research/eval_event_stance.py
   同一纪律）；赢面输面都报，不只挑显著的
7. 方向命中率：opportunity 期望 fwd>0、risk 期望 fwd<0，neutral 无期望方向
   不报命中率

用法：
    uv run python experiments/event_stance_baseline_study.py
    uv run python experiments/event_stance_baseline_study.py --min-severity high --report /tmp/r.md
    uv run python experiments/event_stance_baseline_study.py --ingested-by hermes-sentinel  # issue #212 通道切片
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.calc.regime_probability import forward_return  # noqa: E402
from openinvest.db.market_store import MarketStore  # noqa: E402

EVENTS_DB = ROOT / "db" / "events.db"
HORIZONS = (1, 3, 5)   # 日历天，全系统统一口径
MIN_BUCKET_N = 30      # 诚实闸门：低于此不报统计量
_SEV_INT = {"low": 1, "mid": 2, "high": 3}
_EXPECTED_DIRECTION = {"opportunity": "up", "risk": "down"}  # neutral 无期望方向


def load_events(
    db_path: Path = EVENTS_DB,
    *,
    ingested_by: Optional[str] = None,
    min_severity: str = "low",
) -> List[Dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    q = ("SELECT event_id, stance, severity, created_at, affected_symbols_json, "
         "ingested_by FROM events WHERE severity >= ?")
    params: Tuple[Any, ...] = (_SEV_INT.get(min_severity, 1),)
    if ingested_by:
        q += " AND ingested_by = ?"
        params += (ingested_by,)
    rows = con.execute(q, params).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            syms = json.loads(r["affected_symbols_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            syms = []
        out.append({
            "event_id": r["event_id"],
            "stance": r["stance"],
            "severity": r["severity"],
            "created_at": r["created_at"],
            "symbols": [s.upper() for s in syms if s],
        })
    return out


def _closes_cache():
    cache: Dict[str, Optional[pd.Series]] = {}

    def get(symbol: str) -> Optional[pd.Series]:
        if symbol not in cache:
            df = MarketStore().get_history_df(symbol, days=100000)
            if df is None or df.empty or "Close" not in df:
                cache[symbol] = None
            else:
                s = df["Close"]
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
                cache[symbol] = s[~s.index.duplicated(keep="last")].dropna()
        return cache[symbol]

    return get


def _unconditional_baseline(closes: pd.Series, calendar_days: int) -> Optional[float]:
    """该 symbol 全历史的无条件前瞻收益均值（同一 calendar_days 口径），
    给事件后收益扣基线用——隔离该 symbol 本身的趋势/漂移，不让"资产那段
    时间本来就在涨"被误记成事件的效果。"""
    idx = closes.index
    delta = pd.Timedelta(days=calendar_days)
    rets = []
    for i, d in enumerate(idx):
        j = idx.searchsorted(d + delta, side="left")
        if j >= len(idx):
            break
        rets.append(float(closes.iloc[j]) / float(closes.iloc[i]) - 1.0)
    return float(np.mean(rets)) if rets else None


def build_samples(
    events: List[Dict[str, Any]], get_closes,
) -> List[Dict[str, Any]]:
    """(event × affected_symbol) 展开 → 每条含各 horizon 的 fwd / excess。"""
    samples: List[Dict[str, Any]] = []
    baseline_cache: Dict[Tuple[str, int], Optional[float]] = {}
    for e in events:
        if not e["created_at"]:
            continue
        anchor_date = e["created_at"][:10]  # 只取日期，closes index 是 tz-naive
        for sym in e["symbols"]:
            closes = get_closes(sym)
            if closes is None:
                continue
            row: Dict[str, Any] = {
                "event_id": e["event_id"], "stance": e["stance"],
                "severity": e["severity"], "symbol": sym,
            }
            got_any = False
            for h in HORIZONS:
                fr = forward_return(sym, anchor_date, h, closes=closes)
                if fr is None:
                    continue
                key = (sym, h)
                if key not in baseline_cache:
                    baseline_cache[key] = _unconditional_baseline(closes, h)
                base = baseline_cache[key]
                if base is None:
                    continue
                row[f"fwd_{h}d"] = fr
                row[f"excess_{h}d"] = fr - base
                got_any = True
            if got_any:
                samples.append(row)
    return samples


def _naive_t(x: pd.Series) -> Optional[float]:
    n = len(x)
    if n < 2:
        return None
    s = x.std(ddof=1)
    if not s or np.isnan(s):
        return None
    return float(x.mean() / (s / np.sqrt(n)))


def _cluster_robust_t(sub: pd.DataFrame, col: str) -> Tuple[Optional[float], int]:
    """簇均值（按 symbol）当独立观测的简化 cluster-robust t —— 判断结论是否
    被少数几个资产的重复出现撑起来。非完整 sandwich estimator。"""
    means = sub.groupby("symbol")[col].mean()
    n_clusters = len(means)
    if n_clusters < 2:
        return None, n_clusters
    s = means.std(ddof=1)
    if not s or np.isnan(s):
        return None, n_clusters
    return float(means.mean() / (s / np.sqrt(n_clusters))), n_clusters


def summarize(samples: List[Dict[str, Any]]) -> pd.DataFrame:
    if not samples:
        return pd.DataFrame()
    df = pd.DataFrame(samples)
    rows: List[Dict[str, Any]] = []
    for stance in ("opportunity", "risk", "neutral"):
        g = df[df["stance"] == stance]
        for h in HORIZONS:
            col_fwd, col_ex = f"fwd_{h}d", f"excess_{h}d"
            if col_ex not in g:
                continue
            sub = g.dropna(subset=[col_ex])
            n = len(sub)
            row: Dict[str, Any] = {"stance": stance, "horizon": f"{h}d", "n": n}
            if n < MIN_BUCKET_N:
                row["note"] = f"INSUFFICIENT_DATA (n={n} < {MIN_BUCKET_N})"
                rows.append(row)
                continue
            row["mean_fwd_pct"] = round(float(sub[col_fwd].mean()) * 100, 3)
            row["mean_excess_pct"] = round(float(sub[col_ex].mean()) * 100, 3)
            nt = _naive_t(sub[col_ex])
            row["naive_t"] = round(nt, 2) if nt is not None else None
            ct, n_clusters = _cluster_robust_t(sub, col_ex)
            row["cluster_t"] = round(ct, 2) if ct is not None else None
            row["n_clusters"] = n_clusters
            if stance in _EXPECTED_DIRECTION:
                direction = _EXPECTED_DIRECTION[stance]
                hits = (sub[col_fwd] > 0) if direction == "up" else (sub[col_fwd] < 0)
                row["hit_rate_pct"] = round(float(hits.mean()) * 100, 1)
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="事件研究基线：stance 方向 alpha 检验（issue #210）")
    ap.add_argument("--db", default=str(EVENTS_DB), help="events.db 路径")
    ap.add_argument("--min-severity", default="low", choices=["low", "mid", "high"])
    ap.add_argument("--ingested-by", default=None,
                    help="按入库通道切片（issue #212：hermes-sentinel vs pipeline 对照）")
    ap.add_argument("--report", default=None, help="可选 markdown 报告输出路径")
    args = ap.parse_args()

    events = load_events(Path(args.db), ingested_by=args.ingested_by,
                          min_severity=args.min_severity)
    print(f"事件总数: {len(events)}")
    if not events:
        print("VERDICT: 无事件数据，退出。")
        return

    symbols = sorted({s for e in events for s in e["symbols"]})
    print(f"涉及标的: {len(symbols)} 个")

    get_closes = _closes_cache()
    samples = build_samples(events, get_closes)
    print(f"(事件×标的) 可计算样本: {len(samples)}")

    table = summarize(samples)
    out_lines = [f"事件总数: {len(events)}  标的数: {len(symbols)}  "
                 f"可计算样本: {len(samples)}", ""]
    if table.empty:
        out_lines.append("VERDICT: 无可计算样本（价格数据缺失或全部窗口未成熟）。")
    else:
        out_lines.append(table.to_string(index=False))
        out_lines.append("")
        out_lines.append(
            "解读：naive t 把每个样本当独立观测（同资产多事件会重复计入，容易虚高）；"
            "cluster_t 把每个 symbol 的均值当一个观测（更保守，n_clusters 是簇数）。"
            "|cluster_t| 明显更小甚至变号 → 结论主要由少数资产撑起来，别当全局结论用。"
        )

    text = "\n".join(out_lines)
    print(text)
    if args.report:
        Path(args.report).write_text(
            f"# 事件研究基线报告（issue #210）\n\n```\n{text}\n```\n", encoding="utf-8")
        print(f"\n报告已写入 {args.report}")


if __name__ == "__main__":
    main()
