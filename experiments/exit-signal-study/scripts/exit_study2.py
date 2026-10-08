"""第二轮（单独声明，不是调参）：第一轮 DD 桶只到 25%，而深套个股常见 -60% 级回撤、
且连续 100+ 个交易日在 MA120 下方 —— 超出第一轮覆盖范围，所以补两类状态量。
判据与第一轮完全相同（60d 中位低 ≥2pp 且 P(<0) 高 ≥5pp），结果无论好坏都照报。

新增信号：
  DD40/50/60 : 从 252 日高点回撤首次跨过 40/50/60%
  PERSIST_N  : 收盘连续在 MA120 下方达到 N 个交易日的那一天（N=60/120）—— 状态量，
               对应"持续下跌趋势"，区别于第一轮的一日穿越事件
新增指标：P(120d < -20%) = 再挨一条大腿的概率（离场决策真正关心的左尾）
新增窗口：250d
"""
import sys, yaml, numpy as np, pandas as pd
sys.path.insert(0, "/home/ubuntu/projects-review/invest/src")
from openinvest.db.market_store import MarketStore
H = [60, 120, 250]; ms = MarketStore()

def run(symbols, label):
    sig = {f"DD{x}": [] for x in (40, 50, 60)}
    sig.update({f"PERSIST{n}": [] for n in (60, 120)})
    base = []
    for sym in symbols:
        df = ms.get_history_df(sym, days=100000)
        if df is None or len(df) < 600: continue
        c = df["Close"].astype(float)
        peak = c.rolling(252, min_periods=60).max(); dd = c/peak - 1
        ma = c.rolling(120, min_periods=120).mean()
        fwd = {h: c.shift(-h)/c - 1 for h in H}
        for i in range(len(c)):
            if pd.isna(fwd[250].iloc[i]): continue
            base.append({h: fwd[h].iloc[i] for h in H})
        for x in (40, 50, 60):
            hit = (dd <= -x/100) & (dd.shift(1) > -x/100)
            for i in np.where(hit.fillna(False).to_numpy())[0]:
                if pd.isna(fwd[250].iloc[i]): continue
                sig[f"DD{x}"].append({h: fwd[h].iloc[i] for h in H})
        below = (c < ma).fillna(False).to_numpy()
        runlen = 0
        for i, v in enumerate(below):
            runlen = runlen + 1 if v else 0
            for n in (60, 120):
                if runlen == n and not pd.isna(fwd[250].iloc[i]):
                    sig[f"PERSIST{n}"].append({h: fwd[h].iloc[i] for h in H})
    b = pd.DataFrame(base)
    print(f"\n{'='*82}\n{label}  标的{len(symbols)}  无条件 {len(b):,} 日\n{'='*82}")
    hdr = f"{'信号':<11}{'n':>7}"
    for h in H: hdr += f"{'  中位'+str(h)+'d':>11}{'P(<0)':>8}"
    print(hdr + f"{'P(120d<-20%)':>14}")
    line = f"{'无条件':<11}{len(b):>7}"
    for h in H: line += f"{b[h].median():>10.1%}{(b[h]<0).mean():>8.0%}"
    print(line + f"{(b[120]<-0.2).mean():>14.0%}")
    for k, rows in sig.items():
        if len(rows) < 10: 
            print(f"{k:<11}{len(rows):>7}   样本不足，不判定"); continue
        d = pd.DataFrame(rows)
        line = f"{k:<11}{len(d):>7}"
        for h in H: line += f"{d[h].median():>10.1%}{(d[h]<0).mean():>8.0%}"
        print(line + f"{(d[120]<-0.2).mean():>14.0%}")
    print("\n判定（同第一轮冻结判据，看 60d）：")
    for k, rows in sig.items():
        if len(rows) < 10: continue
        d = pd.DataFrame(rows)
        dm = (d[60].median() - b[60].median())*100; dp = ((d[60]<0).mean() - (b[60]<0).mean())*100
        print(f"  {k:<11} 中位差 {dm:+.1f}pp  P(<0)差 {dp:+.1f}pp  → "
              f"{'有信息' if (dm<=-2 and dp>=5) else '不构成离场依据'}")

uni = yaml.safe_load(open("/home/ubuntu/projects-review/invest/experiments/paper_fleet/universe.yml"))["symbols"]
run(uni, "D. paper_fleet 50 标的 —— 深度回撤 / 持续下跌")
run([s for s in uni if s.endswith(".HK")], "E. 港股子集 —— 深度回撤 / 持续下跌")
