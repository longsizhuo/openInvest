"""离场信号事件研究 —— 纯价格、零 LLM、无前视。判据跑前冻结。

问题：把"离场"解锁到底有没有价值？即：当一个离场信号触发时，之后的走势是否
      明显差于无条件基准？若不差 → 现在这套"永不卖"的设计其实没吃亏，别改。

【跑前冻结的判据，事后不许调】
信号（只用收盘价，t 日只看 ≤t 的数据）：
  DD_X   : 从过去 252 日滚动最高点回撤首次跨过 X%（X ∈ 10/15/20/25），只记穿越日
  MA120  : 收盘从 ≥MA120 变成 <MA120 的那一天（只记穿越日）
前瞻窗口：20 / 60 / 120 个交易日
指标：中位前瞻收益、P(前瞻收益<0)、250 日内重回信号前高点的概率
基准：同一批标的、同一段可用区间内**所有**交易日的无条件前瞻分布
判定（冻结）：某信号称得上"有信息"，需同时满足
  (a) 60 日中位前瞻收益 比无条件中位低 **≥ 2 个百分点**
  (b) 60 日 P(<0) 比无条件高 **≥ 5 个百分点**
两条都不满足 → 结论是"该信号不构成离场依据"。
【注意】重叠窗口有自相关，n 大不等于独立样本多；本研究只报分布差，不报 p 值。
"""
import sys, yaml, numpy as np, pandas as pd
sys.path.insert(0, "/home/ubuntu/projects-review/invest/src")
from openinvest.db.market_store import MarketStore

HORIZONS = [20, 60, 120]
DDS = [10, 15, 20, 25]
ms = MarketStore()

def series(sym):
    df = ms.get_history_df(sym, days=100000)
    if df is None or len(df) < 400: return None
    return df["Close"].astype(float)

def collect(symbols, label):
    rows_sig = {f"DD{x}": [] for x in DDS}; rows_sig["MA120"] = []
    rows_base = []
    for sym in symbols:
        c = series(sym)
        if c is None: continue
        peak = c.rolling(252, min_periods=60).max()
        dd = c / peak - 1.0
        ma = c.rolling(120, min_periods=120).mean()
        n = len(c)
        fwd = {h: c.shift(-h) / c - 1.0 for h in HORIZONS}
        # 无条件基准：所有有 120 日前瞻的交易日
        for i in range(n):
            if pd.isna(fwd[120].iloc[i]): continue
            rows_base.append({h: fwd[h].iloc[i] for h in HORIZONS})
        # 信号日
        for x in DDS:
            hit = (dd <= -x/100) & (dd.shift(1) > -x/100)
            for i in np.where(hit.fillna(False).to_numpy())[0]:
                if pd.isna(fwd[120].iloc[i]): continue
                rec = {h: fwd[h].iloc[i] for h in HORIZONS}
                # 250 日内是否重回信号前的 252 日高点
                fut = c.iloc[i+1:i+251]
                rec["recover"] = bool(len(fut) and fut.max() >= peak.iloc[i])
                rows_sig[f"DD{x}"].append(rec)
        hit = (c < ma) & (c.shift(1) >= ma.shift(1))
        for i in np.where(hit.fillna(False).to_numpy())[0]:
            if pd.isna(fwd[120].iloc[i]): continue
            rec = {h: fwd[h].iloc[i] for h in HORIZONS}
            fut = c.iloc[i+1:i+251]
            rec["recover"] = bool(len(fut) and fut.max() >= peak.iloc[i])
            rows_sig["MA120"].append(rec)
    base = pd.DataFrame(rows_base)
    print(f"\n{'='*78}\n{label}   标的 {len(symbols)}  无条件样本 {len(base):,} 交易日\n{'='*78}")
    print(f"{'信号':<8}{'n':>7}", end="")
    for h in HORIZONS: print(f"{'  中位'+str(h)+'d':>11}{'  P(<0)':>9}", end="")
    print(f"{'  250d回本率':>12}")
    b = {}
    for h in HORIZONS:
        b[h] = (base[h].median(), (base[h] < 0).mean())
    print(f"{'无条件':<8}{len(base):>7}", end="")
    for h in HORIZONS: print(f"{b[h][0]:>10.1%}{b[h][1]:>9.0%}", end="")
    print(f"{'—':>12}")
    out = {}
    for k, rows in rows_sig.items():
        if not rows: continue
        d = pd.DataFrame(rows)
        print(f"{k:<8}{len(d):>7}", end="")
        for h in HORIZONS: print(f"{d[h].median():>10.1%}{(d[h]<0).mean():>9.0%}", end="")
        print(f"{d['recover'].mean():>12.0%}")
        out[k] = d
    # 冻结判据判定
    print("\n判定（冻结判据：60d 中位低 ≥2pp 且 P(<0) 高 ≥5pp）：")
    for k, d in out.items():
        dm = (d[60].median() - b[60][0]) * 100
        dp = ((d[60] < 0).mean() - b[60][1]) * 100
        verdict = "有信息" if (dm <= -2 and dp >= 5) else "不构成离场依据"
        print(f"  {k:<8} 中位差 {dm:+.1f}pp   P(<0)差 {dp:+.1f}pp   → {verdict}")
    return out

uni = yaml.safe_load(open("/home/ubuntu/projects-review/invest/experiments/paper_fleet/universe.yml"))["symbols"]
collect(uni, "A. paper_fleet 50 标的（横截面基准）")
collect(["1024.HK"], "B. 1024.HK 自身（样本薄，仅供参照）")
collect([s for s in uni if s.endswith(".HK")] or ["0700.HK"], "C. 港股子集")
