"""宏观涨跌修复前后回放（get_history_data period 根因，2026-10）。

修复前 get_history_data 无视 period、恒返近 730 行（≈3 年）。委员会有**两个通道**吃到它：
  1. 宏观块 "MoM"（utils/exchange_fee.get_macro_data，period="1mo"）——进 Macro prompt + 日报
  2. Macro agent 自己调的 get_history_data 工具（capabilities/tools.py，LLM 选 period，
     生产 tool_calls.jsonl 里 98% 是 "3mo"/"6mo"）——返回的 first/last_close、
     cumulative_return_pct 标着 3mo/6mo，实为同一段 ~3 年

只读生产行情库（sqlite mode=ro），逐个工作日 as_of D 对 TNX/VIX/DXY/TIP 复算：
  old    = DB 截到 D → tail(730) → 首尾涨跌（两个通道修复前都是这一个数）
  new_P  = DB 截到 D → _apply_period(P)（与生产 get_history_data 同一函数）→ 首尾涨跌，P ∈ 1mo/3mo/6mo
统计每个通道 DXY / TIP 方向（符号）翻转天数 = 黄金货币因素叙事被说反的天数。

可选对账（证明 old 就是委员会当天真实看到的）：
  --daily-dir   memory/daily/<D>.md 宏观块原文（通道 1）
  --tool-calls  memory/.state/tool_calls.jsonl 的 get_history_data 调用返回（通道 2）
日报在亚洲时段跑，美盘可能只到 D-1，故 ≤D / <D 两种截断任一吻合即算复现；工具调用按其返回的 end_date 截断。

  uv run python experiments/macro-mom-fix-2026-10/replay_macro_mom.py \
      --db "$INVEST_HOME/db/market_data.db" --daily-dir "$INVEST_HOME/memory/daily" \
      --tool-calls "$INVEST_HOME/memory/.state/tool_calls.jsonl" \
      --json experiments/macro-mom-fix-2026-10/result.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pandas as pd

from openinvest.calc.timeframe_analysis import _apply_period, _calc_change

SYMS = {"^TNX": "TNX", "^VIX": "VIX", "DX-Y.NYB": "DXY", "TIP": "TIP"}
OLD_ROWS = 730  # 修复前 MarketStore.get_history_df 默认 days=730
START, END = "2026-07-13", "2026-10-07"
PERIODS = ("1mo", "3mo", "6mo")  # 1mo = 宏观块；3mo/6mo = Macro agent 工具调用的主力窗口
# 日报宏观块原文格式（utils/exchange_fee.get_macro_data）：(当时最新价, MoM)；TIP 不落价位
_REPORT_RE = {
    "TNX": r"\(\^TNX\): ([\d.]+)% \(MoM: ([+-][\d.]+)%\)",
    "VIX": r"\(\^VIX\): ([\d.]+) \(MoM: ([+-][\d.]+)%\)",
    "DXY": r"DX-Y\.NYB\): ([\d.]+) \(MoM: ([+-][\d.]+)%\)",
    "TIP": r"TIP, MoM\): ()([+-][\d.]+)%",
}
DATA: dict = {}


def load(db: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for sym in SYMS:
        # 与 get_history_df 同 SQL（不滤 NULL），保证 old 列逐字复刻
        df = pd.read_sql_query(
            "SELECT date, close FROM daily_prices WHERE symbol = ? ORDER BY date ASC",
            con, params=(sym,),
        )
        out[sym] = pd.Series(df["close"].values, index=pd.to_datetime(df["date"]))
    con.close()
    return out


def _chg(s: pd.Series) -> float:
    return round(float(_calc_change(s.iloc[0], s.iloc[-1])), 6)


@lru_cache(maxsize=None)
def window(sym: str, day: str, strict: bool = False):
    series = DATA[sym]
    cutoff = pd.Timestamp(day)
    cut = series[series.index < cutoff] if strict else series[series.index <= cutoff]
    if cut.empty:
        return None
    old = cut.tail(OLD_ROWS)  # 修复前：无论 period 都是这 730 行
    out = {
        "last_date": str(cut.index[-1].date()),
        "old": _chg(old),
        "old_base": str(old.index[0].date()),
        "_old_base_close": float(old.iloc[0]),
    }
    for p in PERIODS:
        new = _apply_period(cut.to_frame("Close"), p)["Close"]
        out[p] = _chg(new)
        out[f"{p}_base"] = str(new.index[0].date())
        out[f"_{p}_base_close"] = float(new.iloc[0])
    return out


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


def channel_summary(rows: list, p: str) -> dict:
    def flipped(r, k):
        return sign(r[k]["old"]) != sign(r[k][p])

    return {
        "sign_flips": {k: sum(flipped(r, k) for r in rows) for k in SYMS.values()},
        "gold_factor_inverted_days_dxy_or_tip": sum(flipped(r, "DXY") or flipped(r, "TIP") for r in rows),
        "gold_factor_both_inverted_days": sum(flipped(r, "DXY") and flipped(r, "TIP") for r in rows),
        # 双顺风 = 美元跌 + TIP 涨；双逆风 = 美元涨 + TIP 跌
        "old_double_tailwind_days": sum(r["DXY"]["old"] < 0 < r["TIP"]["old"] for r in rows),
        "new_double_tailwind_days": sum(r["DXY"][p] < 0 < r["TIP"][p] for r in rows),
        "old_double_tailwind_but_true_double_headwind_days": sum(
            r["DXY"]["old"] < 0 < r["TIP"]["old"] and r["TIP"][p] < 0 < r["DXY"][p] for r in rows
        ),
        "mean_abs_gap_pp": {
            k: round(sum(abs(r[k]["old"] - r[k][p]) for r in rows) / len(rows) * 100, 2)
            for k in SYMS.values()
        },
    }


def audit_daily(daily_dir: str, days) -> dict:
    """通道 1：日报宏观块原文 vs old / new_1mo。"""
    audit = {k: {"recorded": 0, "old_reproduced": 0, "new_reproduced": 0} for k in SYMS.values()}
    for d in days:
        f = Path(daily_dir) / f"{d.date()}.md"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        for sym, k in SYMS.items():
            m = re.search(_REPORT_RE[k], text)
            if not m:
                continue
            level, rec = (float(m.group(1)) if m.group(1) else None), float(m.group(2)) / 100
            cands = [c for c in (window(sym, str(d.date())), window(sym, str(d.date()), True)) if c]
            audit[k]["recorded"] += 1

            def hit(c, val, base_key):
                # (a) DB 收盘价复算，日报 {:+.2%} 落盘 → 半基点容差；
                # (b) 日内跑的日报最后一根是盘中价（后被收盘覆盖）→ 用日报自己落的价位 /
                #     同一基准价复算，容差再加价位 2 位小数的舍入
                if abs(c[val] - rec) <= 5e-5:
                    return True
                if level is None:
                    return False
                return abs(level / c[base_key] - 1 - rec) <= 5e-5 + 0.005 / level

            audit[k]["old_reproduced"] += any(hit(c, "old", "_old_base_close") for c in cands)
            audit[k]["new_reproduced"] += any(hit(c, "1mo", "_1mo_base_close") for c in cands)
    return audit


def audit_tool_calls(path: str) -> dict:
    """通道 2：Macro agent 的 get_history_data 工具返回（first_close / n_days）vs old / new。

    按返回里的 end_date（= 当时最后一根）截断复算，与调用时刻/时区无关；end_date 落在回放窗口内
    的都算（含当期回测跑的调用，同一口径）。"""
    out = {}
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        a = r.get("arguments") or {}
        sym, p, prev = a.get("symbol"), a.get("period"), r.get("result_preview", "")
        if r.get("tool_name") != "get_history_data" or sym not in SYMS or p not in ("3mo", "6mo"):
            continue
        end = re.search(r'"end_date": "([\d-]+)"', prev)
        fc, nd = re.search(r'"first_close": ([\d.]+)', prev), re.search(r'"n_days": (\d+)', prev)
        if not (end and fc) or not START <= end.group(1) <= END:
            continue
        day = end.group(1)
        a_ = out.setdefault(f"{SYMS[sym]}_{p}", Counter())
        a_["recorded"] += 1
        a_["n_days_730"] += bool(nd and int(nd.group(1)) == OLD_ROWS)
        cands = [c for c in (window(sym, day),) if c]
        first = float(fc.group(1))
        # 工具输出 round(…, 4)
        a_["old_base_reproduced"] += any(abs(c["_old_base_close"] - first) <= 1e-4 for c in cands)
        a_["new_base_reproduced"] += any(abs(c[f"_{p}_base_close"] - first) <= 1e-4 for c in cands)
    return {k: dict(v) for k, v in sorted(out.items())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--daily-dir")
    ap.add_argument("--tool-calls")
    ap.add_argument("--json")
    args = ap.parse_args()

    DATA.update(load(args.db))
    days = pd.bdate_range(START, END)
    rows = [{"date": str(d.date()), **{k: window(s, str(d.date())) for s, k in SYMS.items()}}
            for d in days]

    print(f"{'date':<11}" + "".join(f"{k + ' old':>10}{'1mo':>8}{'3mo':>8}{'6mo':>8}" for k in SYMS.values()))
    for r in rows:
        line = f"{r['date']:<11}"
        for k in SYMS.values():
            line += f"{r[k]['old']:>+10.2%}" + "".join(
                f"{r[k][p]:>+7.2%}{'*' if sign(r[k][p]) != sign(r[k]['old']) else ' '}" for p in PERIODS
            )
        print(line)
    print("(* = 方向与 old 相反)")

    summary = {
        "weekdays": len(rows),
        "macro_block_mom_1mo": channel_summary(rows, "1mo"),
        "macro_agent_tool_3mo": channel_summary(rows, "3mo"),
        "macro_agent_tool_6mo": channel_summary(rows, "6mo"),
    }
    if args.daily_dir:
        summary["daily_report_audit_1mo"] = audit_daily(args.daily_dir, days)
    if args.tool_calls:
        summary["tool_call_audit"] = audit_tool_calls(args.tool_calls)

    print(json.dumps(summary, indent=1, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
