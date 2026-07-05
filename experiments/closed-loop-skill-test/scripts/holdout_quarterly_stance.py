"""按季聚合 holdout 委员会对 3 资产的立场,映射到 CMB 逐季表(委员会 vs 专业私行)。
读已烧 .backtest verdict,不调 LLM。holdout 窗口 2025-01→2026-03 → 2025Q1..2026Q1。
用法: PYTHONPATH=... INVEST_HOME=... uv run python holdout_quarterly_stance.py
"""
import re
from collections import defaultdict
from openinvest.core.memory_store import MemoryStore
from openinvest.core.committee import safe_symbol

ASSETS = ["GC=F", "510300.SS", "NDQ.AX"]
CMB_GOLD = {"2025Q1": "标配(↓from中高配)", "2025Q2": "标配", "2025Q3": "标配", "2025Q4": "标配", "2026Q1": "标配"}
CMB_ASHARE = {q: "中高配" for q in CMB_GOLD}          # A股全程中高配
CMB_US = {q: "标配" for q in CMB_GOLD}                  # 美股全程标配
_V = re.compile(r"\*\*Verdict\*\*:\s*(\w+)")
_A = re.compile(r"\*\*Suggested allocation CNY\*\*:\s*(-?[\d.]+)")


def quarter(d):
    y, m = d[:4], int(d[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def stance(cell):
    """把 verdict 分布概括成一句倾向"""
    n = cell["n"]
    if not n:
        return "—"
    buys = cell.get("BUY", 0) + cell.get("ACCUMULATE", 0)
    trims = cell.get("TRIM", 0) + cell.get("SELL", 0)
    hold = cell.get("HOLD", 0)
    net = cell["net_alloc"]
    if buys > trims and buys >= n * 0.2:
        lab = "偏多(加仓)"
    elif trims > buys and trims >= n * 0.2:
        lab = "偏空(减仓)"
    else:
        lab = "中性(多 HOLD)"
    return f"{lab} [B/A {buys} HOLD {hold} T/S {trims} | net ¥{net:+,.0f}]"


def main():
    store = MemoryStore()
    bt = store.root / ".backtest"
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for p in sorted(bt.iterdir()):
        if not p.is_dir() or p.name < "2025-01":
            continue
        q = quarter(p.name)
        for sym in ASSETS:
            f = p / f"{safe_symbol(sym)}.md"
            if not f.exists():
                continue
            t = f.read_text(encoding="utf-8")
            v = _V.search(t)
            if not v:
                continue
            cell = agg[q][sym]
            cell[v.group(1).upper()] += 1
            cell["n"] += 1
            a = _A.search(t)
            if a:
                cell["net_alloc"] += float(a.group(1))
    cmb = {"GC=F": CMB_GOLD, "510300.SS": CMB_ASHARE, "NDQ.AX": CMB_US}
    print("# 委员会逐季立场 vs CMB(holdout 预览)\n")
    for q in sorted(agg):
        print(f"## {q}")
        for sym in ASSETS:
            c = agg[q].get(sym)
            cm = cmb[sym].get(q, "?")
            print(f"  {sym:10} 委员会: {stance(c) if c else '—':52} | CMB: {cm}")
        print()


if __name__ == "__main__":
    main()
