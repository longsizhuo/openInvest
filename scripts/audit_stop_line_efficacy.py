"""确定性回算：CIO 在 HOLD 备忘里写的"跌破 X 就减仓"那条线，到底有没有预测力。

背景（2026-08-20）：166 份 HOLD transcript 里 76% 的 CIO 备忘写了带具体价格的
止损/减仓触发线，但系统里没有任何字段承载、没有任何东西监控它。要不要把这条线
做成结构化字段 + price_sentinel 监控，取决于这些线是不是噪音。

零 LLM，纯价格回算（治理红线允许：只依赖 ≤ 当天收盘，无前视）。

================== 判据在跑之前冻结（不看结果后调） ==================
样本口径
  - 语料 A（primary）：memory/.backtest/<D>/<SYM>.md，**Contaminated**: false 的
  - 语料 B（secondary）：memory/.committee/<D>/<SYM>.md（真实持仓上下文，n 小）
  - 只取 **Verdict**: HOLD
触发线提取（v2；v1 作废见下）
  按句切 CIO 备忘（。；\n 分句），只保留**同时满足**的句子：
    a. 含卖出动作词 止损|减仓|清仓|卖出|TRIM
    b. 不含买入动作词 ACCUMULATE|加仓|买入|抄底|建仓（同句两头堵 = 歧义，丢）
  句内先找 跌破|失守|低于 + 数字，找不到再找 止损(位|线|价)? + 数字（"止损（约 HK$36.4）"）；
  纯百分比止损（数字在"止损"前，如"-15% 止损"）无绝对价，如实丢弃。
  若该处前 15 字内出现宏观主语
  （美元指数|DXY|VIX|TNX|收益率|恐慌指数|实际利率）则跳过这一处，取下一处。
  取首个存活的数字。

  ⚠️ v1 作废（2026-08-20）：v1 只按"跌破/止损 + 数字"全文取首个命中，抽检 8 条
  有 5 条误抓——"美元指数跌破100"（DXY 不是标的价，这句宏观样板话几乎每份备忘
  都有）、"跌破1300…可转为 ACCUMULATE"（买点不是卖点）。合理性过滤挡不住 DXY=100
  落在标的价 0.6~1.0 倍区间的情形。v1 结果不可用；**判定规则未随之改动**。
单位/合理性过滤（挡掉 GC=F 的 ¥/克 vs USD/oz 这类口径不一致、以及解析垃圾）
  - 必须 0.60 × close_D ≤ trigger < close_D（下行线必须低于当日收盘且不离谱）
击穿定义
  - D 之后 20 个交易日内，首个 close < trigger 的交易日 D'
结果度量
  - D' 收盘起 +5 / +20 个交易日的收盘收益率
  - excess = 该样本收益 − 同 symbol 全窗口无条件同期限收益中位数（自动做 symbol 匹配）
判定规则（冻结）
  - n_击穿 ≥ 20，且 median(excess@20d) ≤ -1.0pp，且 median(excess@5d) < 0
    → 判"线有预测力，值得做监控"
  - 否则 → 判"不采纳"。样本不足不改判据凑样本。
======================================================================
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "experiments", "stop-line-efficacy")

BREACH_WINDOW = 20          # 交易日
HORIZONS = (5, 20)          # 交易日
SANITY_LO, SANITY_HI = 0.60, 1.00
MIN_N = 20
EXCESS_20D_BAR = -1.0       # pp

SELL_KW = re.compile(r"止损|减仓|清仓|卖出|TRIM", re.I)
BUY_KW = re.compile(r"ACCUMULATE|加仓|买入|抄底|建仓", re.I)
BREACH_RE = re.compile(
    r"(?:跌破|失守|低于)\s*(?:HK\$|US\$|A\$|港元|美元|¥|￥|\$)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
)
MACRO_SUBJ = re.compile(r"美元指数|DXY|VIX|TNX|收益率|恐慌指数|实际利率")
# 第二形态：绝对价写在"止损"后面的括号/位/线里（"止损（约 HK$36.4）"、"止损位 36.4"）。
# 纯百分比止损（"维持 -15% 止损"，数字在词前）不匹配——没有绝对价没法回算，如实丢弃。
STOP_LEVEL_RE = re.compile(
    r"止损(?:位|线|价)?[^0-9\n]{0,10}(?:HK\$|US\$|A\$|港元|美元|¥|￥|\$)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
)
SENT_SPLIT = re.compile(r"[。；;\n]")
VERDICT_RE = re.compile(r"\*\*Verdict\*\*:\s*(\w+)")
DATE_RE = re.compile(r"\*\*Date\*\*:\s*(\d{4}-\d{2}-\d{2})")
SYMBOL_RE = re.compile(r"\*\*Symbol\*\*:\s*(\S+)")
CONTAM_RE = re.compile(r"\*\*Contaminated\*\*:\s*(true|false)")


def load_prices() -> Dict[str, Tuple[List[str], List[float]]]:
    """symbol -> (按日期升序的 date 列表, close 列表)"""
    conn = sqlite3.connect(os.path.join(ROOT, "db", "market_data.db"))
    out: Dict[str, Tuple[List[str], List[float]]] = {}
    cur = conn.execute(
        "select symbol, date, close from daily_prices "
        "where date >= '2020-01-01' and close is not null order by symbol, date"
    )
    cur_sym, dates, closes = None, [], []
    for sym, d, c in cur:
        if sym != cur_sym:
            if cur_sym is not None:
                out[cur_sym] = (dates, closes)
            cur_sym, dates, closes = sym, [], []
        dates.append(str(d)[:10])
        closes.append(float(c))
    if cur_sym is not None:
        out[cur_sym] = (dates, closes)
    return out


def extract_trigger(memo: str) -> Optional[float]:
    """见模块 docstring 的 v2 提取规则。返回首个存活的卖出触发价。"""
    for sent in SENT_SPLIT.split(memo):
        if not SELL_KW.search(sent) or BUY_KW.search(sent):
            continue
        for m in BREACH_RE.finditer(sent):
            head = sent[max(0, m.start() - 15):m.start()]
            if MACRO_SUBJ.search(head):
                continue          # "美元指数跌破100" 这类宏观数字，不是标的价
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
        m2 = STOP_LEVEL_RE.search(sent)
        if m2 and not MACRO_SUBJ.search(sent[max(0, m2.start() - 15):m2.start()]):
            try:
                return float(m2.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def scan(pattern: str, require_uncontaminated: bool) -> List[dict]:
    rows = []
    for f in glob.glob(pattern):
        try:
            t = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        v = VERDICT_RE.search(t)
        if not v or v.group(1) != "HOLD":
            continue
        if require_uncontaminated:
            cm = CONTAM_RE.search(t)
            if not cm or cm.group(1) != "false":
                continue
        d = DATE_RE.search(t)
        s = SYMBOL_RE.search(t)
        if not d or not s:
            continue
        memo = t.split("## CIO Memo", 1)[-1].split("\n---", 1)[0]
        trig = extract_trigger(memo)
        if trig is None:
            rows.append({"date": d.group(1), "symbol": s.group(1), "trigger": None})
            continue
        rows.append({"date": d.group(1), "symbol": s.group(1), "trigger": trig})
    return rows


def idx_on_or_after(dates: List[str], d: str) -> Optional[int]:
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(dates) else None


def baseline_medians(closes: List[float], h: int) -> Optional[float]:
    if len(closes) <= h + 1:
        return None
    rets = [(closes[i + h] / closes[i] - 1.0) * 100.0
            for i in range(len(closes) - h) if closes[i] > 0]
    return statistics.median(rets) if rets else None


def analyse(rows: List[dict], prices, label: str) -> dict:
    stats = {"label": label, "hold_total": len(rows),
             "with_line": 0, "sanity_ok": 0, "breached": 0,
             "dropped_no_price": 0, "dropped_sanity": 0}
    base_cache: Dict[Tuple[str, int], Optional[float]] = {}
    excess: Dict[int, List[float]] = defaultdict(list)
    raw: Dict[int, List[float]] = defaultdict(list)
    per_symbol = defaultdict(int)

    for r in rows:
        if r["trigger"] is None:
            continue
        stats["with_line"] += 1
        sym = r["symbol"]
        if sym not in prices:
            stats["dropped_no_price"] += 1
            continue
        dates, closes = prices[sym]
        i = idx_on_or_after(dates, r["date"])
        if i is None or i == 0:
            stats["dropped_no_price"] += 1
            continue
        close_d = closes[i] if dates[i] == r["date"] else closes[i - 1]
        trig = r["trigger"]
        if not (SANITY_LO * close_d <= trig < SANITY_HI * close_d):
            stats["dropped_sanity"] += 1
            continue
        stats["sanity_ok"] += 1
        bi = None
        for j in range(i + 1, min(i + 1 + BREACH_WINDOW, len(closes))):
            if closes[j] < trig:
                bi = j
                break
        if bi is None:
            continue
        stats["breached"] += 1
        per_symbol[sym] += 1
        for h in HORIZONS:
            if bi + h >= len(closes):
                continue
            ret = (closes[bi + h] / closes[bi] - 1.0) * 100.0
            key = (sym, h)
            if key not in base_cache:
                base_cache[key] = baseline_medians(closes, h)
            b = base_cache[key]
            if b is None:
                continue
            raw[h].append(ret)
            excess[h].append(ret - b)

    stats["breach_rate"] = (stats["breached"] / stats["sanity_ok"]
                            if stats["sanity_ok"] else None)
    stats["per_symbol"] = dict(sorted(per_symbol.items(), key=lambda kv: -kv[1])[:10])
    for h in HORIZONS:
        stats[f"n_{h}d"] = len(raw[h])
        stats[f"median_raw_{h}d"] = round(statistics.median(raw[h]), 2) if raw[h] else None
        stats[f"median_excess_{h}d"] = round(statistics.median(excess[h]), 2) if excess[h] else None
        stats[f"pct_negative_{h}d"] = (round(100.0 * sum(1 for x in raw[h] if x < 0) / len(raw[h]), 1)
                                       if raw[h] else None)
    n20 = stats["n_20d"]
    e20 = stats["median_excess_20d"]
    e5 = stats["median_excess_5d"]
    stats["verdict"] = (
        "有预测力" if (n20 >= MIN_N and e20 is not None and e5 is not None
                       and e20 <= EXCESS_20D_BAR and e5 < 0)
        else ("样本不足" if n20 < MIN_N else "不采纳")
    )
    return stats


def main() -> None:
    print("载入行情…", flush=True)
    prices = load_prices()
    print(f"  {len(prices)} 个 symbol 有价格序列", flush=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    results = []
    for label, pattern, uncont in (
        ("A_backtest_uncontaminated",
         os.path.join(ROOT, "memory/.backtest/[0-9]*/*.md"), True),
        ("B_live_committee",
         os.path.join(ROOT, "memory/.committee/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/*.md"), False),
    ):
        print(f"扫描 {label} …", flush=True)
        rows = scan(pattern, uncont)
        st = analyse(rows, prices, label)
        results.append(st)
        print(json.dumps(st, ensure_ascii=False, indent=2), flush=True)

    with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n写入 {OUT_DIR}/result.json")


if __name__ == "__main__":
    sys.exit(main())
