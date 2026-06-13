"""三臂对照·免费臂 — GC=F 确定性基线（零 LLM，纯算术，可逐位复现）。

**预注册声明（跑之前写死，无任何参数搜索）**：
- Arm A 买入持有：第一天全仓黄金到最后一天
- Arm B 200DMA 趋势：收盘 > 200 日均线 → 持仓；否则现金（0 收益，不计利息）。
  经典外部基准（Faber 2007 风格），参数 200 是文献默认非本仓调出
- Arm C regime 确定性：production classify_regime（零前视逐日帧）∈
  {uptrend, recovery, range, unknown} → 持仓；{downtrend, crash} → 现金。
  映射 = 系统文档语义（趋势内持有、下行/急跌离场），非搜索所得
- 信号 T 日收盘算出，T+1 日收盘成交（避免同日前视）
- 指标：CAGR、年化 Sharpe（日收益 mean/std×√252，rf=0）、MaxDD、持仓时间占比
- 周期：全历史（数据起点起）+ 近窗 2024-01-01 起（委员会时代）
- 第四臂（完整 LLM 系统）后补：复用历史消融 draws，不在本脚本

用途：research/ 复现包的外部参照系——"纯确定性策略"不再只和自己比；
同时填 README 局限"基线③未对标经典因子"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # _metrics.py 同目录

from _metrics import metrics  # noqa: E402  共享指标，避免逐字复制漂移
from core.regime_probability import compute_regime_return_frame  # noqa: E402
from db.market_store import MarketStore  # noqa: E402

SYMBOL = "GC=F"
RECENT_START = "2024-01-01"


def run() -> dict:
    df = MarketStore().get_history_df(SYMBOL, days=100000)
    close = df["Close"]
    asset_ret = close.pct_change()

    frame = compute_regime_return_frame(df, SYMBOL, windows=("30d",))
    regime = frame["regime"].reindex(close.index)

    signals = {
        "A_buy_and_hold": pd.Series(True, index=close.index),
        "B_200dma_trend": close > close.rolling(200).mean(),
        "C_regime_deterministic": ~regime.isin(["downtrend", "crash"]),
    }
    out: dict = {"symbol": SYMBOL,
                 "span": [str(close.index.min().date()), str(close.index.max().date())]}
    for era, sl in (("full", slice(None, None)), ("2024+", slice(RECENT_START, None))):
        out[era] = {}
        for name, sig in signals.items():
            pos = sig.shift(1, fill_value=False)      # T 收盘信号 → T+1 生效
            strat = (asset_ret * pos)[sl]
            out[era][name] = metrics(strat, pos[sl])
    return out


def main() -> None:
    res = run()
    print(f"{res['symbol']}  数据: {res['span'][0]} → {res['span'][1]}\n")
    for era in ("full", "2024+"):
        print(f"[{era}]")
        print(f"  {'臂':<26}{'CAGR':>8}{'Sharpe':>8}{'MaxDD':>9}{'持仓占比':>9}{'天数':>7}")
        for name, m in res[era].items():
            print(f"  {name:<26}{m['cagr_pct']:>+7.1f}%{m['sharpe']:>8.2f}"
                  f"{m['max_dd_pct']:>+8.1f}%{m['time_in_market_pct']:>8.1f}%{m['n_days']:>7}")
        print()
    out = Path(__file__).parent / "gold_baselines_result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
