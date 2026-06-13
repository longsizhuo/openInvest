"""第四臂：完整 LLM 委员会系统（复用 2026-06-04 消融 draws，零新 LLM 调用）。

**预注册映射（跑之前写死）**：
- 数据：B 链（生产语义，无方向锁）2024-26 窗 draws，GC=F 行，3 个独立 draws
  （主跑 + s1 + s2）——委员会裁决序列是现成的，本脚本只做确定性转换
- verdict → 仓位：TRIM/SELL → 0（离场）；BUY/ACCUMULATE/HOLD → 1（持有）。
  即"满仓起步，听委员会的卖出信号"——与免费臂 C（regime 确定性）同一形态，
  区别仅在信号源是 LLM 委员会而非 regime 分类器
- 周频决策（draws 为 122 个周一），信号周一收盘算出 → 次个交易日生效；
  周间仓位不变
- 指标与免费臂同口径：CAGR / 年化 Sharpe（日收益，rf=0）/ MaxDD / 持仓占比
- 3 draws 分别报告 + 均值——draw 间差异就是 35-45% 翻转噪声的钱口径体现
- UNCLEAR/缺失日 → 沿用前一周仓位（保守：不动）

诚实声明：draws 的 LLM 训练截止晚于回测期（lookahead 风险，wiki 12 已列）；
本臂回答"同形态规则下 LLM 信号源 vs 确定性信号源"，不是"系统 vs 躺平"的
全部答案。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from db.market_store import MarketStore  # noqa: E402

SYMBOL = "GC=F"
# 注意：各窗的 _s1/_s2 文件是主跑的分片（verdict 逐位相同，2026-06-12 核验），
# 不是独立 draws——每窗只有 1 个 draw，结论按 n=1 解读
WINDOWS_DRAWS = {
    "2024-26": "exp_B_2426.jsonl",
    "2022重放": "exp_B_2022.jsonl",
    "2020重放": "exp_B_2020.jsonl",
}
DRAW_DIR = Path("/home/ubuntu/openinvest-research-archive/ablation_draws")
EXIT_VERDICTS = {"TRIM", "SELL"}


def load_signals(fnames) -> pd.Series:
    rows = []
    for fname in fnames:
        rows += [json.loads(l) for l in (DRAW_DIR / fname).open(encoding="utf-8")]
    sig = {}
    for r in rows:
        if r.get("asset") != SYMBOL or r.get("status") != "ok":
            continue
        v = (r.get("verdict") or "").upper()
        if v in ("UNCLEAR", ""):
            continue
        sig[pd.Timestamp(r["date"])] = v not in EXIT_VERDICTS
    return pd.Series(sig).sort_index()


def metrics(daily_ret: pd.Series, in_market: pd.Series) -> dict:
    r = daily_ret.dropna()
    eq = (1 + r).cumprod()
    years = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = (r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else float("nan")
    mdd = (eq / eq.cummax() - 1).min()
    return {"cagr_pct": round(cagr * 100, 2), "sharpe": round(float(sharpe), 2),
            "max_dd_pct": round(float(mdd) * 100, 2),
            "time_in_market_pct": round(float(in_market.mean()) * 100, 1)}


def main() -> None:
    df = MarketStore().get_history_df(SYMBOL, days=100000)
    close = df["Close"]
    asset_ret = close.pct_change()
    from core.regime_probability import compute_regime_return_frame
    frame = compute_regime_return_frame(df, SYMBOL, windows=("30d",))
    regime = frame["regime"].reindex(close.index)
    free = {
        "A_buy_and_hold": pd.Series(True, index=close.index),
        "B_200dma_trend": close > close.rolling(200).mean(),
        "C_regime_deterministic": ~regime.isin(["downtrend", "crash"]),
    }

    all_out = {}
    for win, fname in WINDOWS_DRAWS.items():
        weekly = load_signals([fname])
        if weekly.empty:
            continue
        start, end = weekly.index.min(), weekly.index.max()
        results = {}
        # 决策日可能是非交易日（2022 重放窗是周六）——union 进索引 ffill 再取交易日
        pos = (weekly.reindex(close.index.union(weekly.index)).ffill()
               .reindex(close.index))
        pos = pos.loc[start:end].shift(1).map(
            lambda x: bool(x) if pd.notna(x) else False)
        strat = (asset_ret.loc[start:end] * pos)
        results["LLM_committee_B"] = metrics(strat, pos)
        results["LLM_committee_B"]["exit_weeks"] = f"{int((~weekly).sum())}/{len(weekly)}"
        for name, sig in free.items():
            fpos = sig.shift(1, fill_value=False).loc[start:end]
            fstrat = (asset_ret * fpos).loc[start:end]
            results[name] = metrics(fstrat, fpos)
            results[name]["exit_weeks"] = "-"
        all_out[win] = {"span": [str(start.date()), str(end.date())], **results}

        print(f"\n[{win}]  {start.date()} → {end.date()}（单 draw，35-45% 翻转噪声下不可单跑下结论）")
        print(f"  {'臂':<24}{'CAGR':>8}{'Sharpe':>8}{'MaxDD':>9}{'持仓占比':>9}{'离场周':>8}")
        for name, m in results.items():
            print(f"  {name:<24}{m['cagr_pct']:>+7.1f}%{m['sharpe']:>8.2f}"
                  f"{m['max_dd_pct']:>+8.1f}%{m['time_in_market_pct']:>8.1f}%"
                  f"{m['exit_weeks']:>8}")

    out = Path(__file__).parent / "gold_fourth_arm_result.json"
    out.write_text(json.dumps(all_out, ensure_ascii=False, indent=1))
    print(f"\n已写 {out}")


if __name__ == "__main__":
    main()
