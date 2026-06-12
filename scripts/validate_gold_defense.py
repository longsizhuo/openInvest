"""黄金 VIX 防御腿语义验收 — fit/OOS 分割，预注册（确定性，零 LLM）。

背景：快崩防御的 VIX 腿（VIX 2y 分位 ≥ vix_defense_quantile → 拦 BUY/ACCUMULATE）
是股票语义（恐慌=接刀）。黄金是双相资产：流动性挤兑期被一起卖（2020-03 两周
-8.6%），之后危机买盘接力（同谷后 90d +30.7%）。假设：对黄金，高 VIX 不是
买入禁区而是右偏条件桶，拦截方向性错误。

**预注册验收（跑之前写死，ADR-010 rule 4）**：
- 数据：GC=F + ^VIX 全历史；VIX 分位 = 近 504 交易日滚动分位（生产同款口径）
- 防御区 = 分位 ≥ 0.85（生产默认 vix_defense_quantile）
- 分割：fit = 2017-12-31 及之前；OOS = 2018-01-01 之后
- **接受"黄金豁免 VIX 腿"当且仅当**（两个时代分别满足，30d 与 60d 两窗都满足）：
    1. 防御区中位前向收益 ≥ 无条件中位（拦截无保护价值）
    2. 防御区跌破概率 P(fwd<0) ≤ 无条件跌破概率
- 90d 与 P10/P25 仅报告不参与判定（防御区 90d 独立样本 ~10 个，太薄）
- 左尾（P10）无论结果如何如实报告——豁免 ≠ 鼓励一把梭，分批纪律由
  CIO prompt / sizing 层管，不在本验收范围
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.market_store import MarketStore  # noqa: E402

DEFENSE_Q = 0.85
FIT_END = "2017-12-31"
GATE_WINDOWS = (30, 60)     # 参与判定
REPORT_WINDOWS = (30, 60, 90)


def build_frame():
    ms = MarketStore()
    gc = ms.get_history_df("GC=F", days=100000)[["Close"]].rename(columns={"Close": "gc"})
    vix = ms.get_history_df("^VIX", days=100000)[["Close"]].rename(columns={"Close": "vix"})
    df = gc.join(vix, how="inner").dropna()
    df["vix_q"] = df["vix"].rolling(504).apply(lambda w: (w <= w.iloc[-1]).mean(), raw=False)
    df = df.dropna()
    for w in REPORT_WINDOWS:
        df[f"fwd{w}"] = df["gc"].shift(-w) / df["gc"] - 1
    return df


def stats(s):
    s = s.dropna() * 100
    return {
        "n": len(s), "median": s.median(), "p25": s.quantile(0.25),
        "p10": s.quantile(0.10), "p_below": (s < 0).mean(),
    }


def main():
    df = build_frame()
    print(f"样本: {df.index.min().date()} → {df.index.max().date()}  n={len(df)}")
    eras = {"fit(≤2017)": df.loc[:FIT_END], "OOS(2018+)": df.loc["2018-01-01":]}
    verdict_ok = True
    for era, sub in eras.items():
        zone = sub[sub.vix_q >= DEFENSE_Q]
        print(f"\n[{era}]  防御区天数 {len(zone)}/{len(sub)} ({len(zone)/len(sub):.0%})")
        print(f"  {'窗':<5}{'桶':<8}{'n':>6}{'独立≈':>6}{'中位':>8}{'P25':>8}{'P10':>8}{'跌破':>7}")
        for w in REPORT_WINDOWS:
            z, u = stats(zone[f"fwd{w}"]), stats(sub[f"fwd{w}"])
            for tag, x in (("防御区", z), ("无条件", u)):
                print(f"  {w:<5}{tag:<8}{x['n']:>6}{x['n']//w:>6}"
                      f"{x['median']:>+7.1f}%{x['p25']:>+7.1f}%{x['p10']:>+7.1f}%"
                      f"{x['p_below']*100:>6.0f}%")
            if w in GATE_WINDOWS:
                ok = z["median"] >= u["median"] and z["p_below"] <= u["p_below"]
                print(f"        → {w}d 判定: 中位 {z['median']:+.1f} vs {u['median']:+.1f}, "
                      f"跌破 {z['p_below']:.0%} vs {u['p_below']:.0%} → {'满足' if ok else '不满足'}")
                verdict_ok &= ok
    print(f"\n预注册验收：{'PASS — 黄金豁免 VIX 防御腿（type=metal），ATR 腿保留' if verdict_ok else 'FAIL — 维持现状，结论落文档'}")
    return verdict_ok


if __name__ == "__main__":
    main()
