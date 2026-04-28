"""把过往交易历史按日回填到 memory/.state/pnl_history.jsonl

作用：让 docs/pnl_chart.svg 第一次渲染就有完整曲线（而不是只有"现在这一刻"
一个点），方便 README 上的图直接展示 30 天 / 90 天的真实趋势。

数据源：
- 黄金 7 笔交易 → 硬编码（用户在对话里给的浙商积存金完整明细）
- NDQ.AX 当前持仓 → 从 memory/portfolio.md 读 ndq_shares + 均价
- 历史价格 → yfinance（GC=F / USDCNY=X / NDQ.AX / AUDCNY=X 都从 2025-03-20 起可拉）

逻辑：
1. 黄金：从首笔交易日开始，cumsum grams + total_cost；每个交易日往后，
   用 (当日 GC=F 现货 USD/oz / 31.1035 * USDCNY) 当克价，算 PnL %
2. NDQ：用当前 ndq_avg_cost_aud_per_share 当成历史不变的均价（保守假设
   用户没记录买入日期），每日浮盈 = NDQ 当日 close / 均价 - 1
3. Total：按 cost 加权的两类资产 PnL %（NDQ 用 AUDCNY 折算成 CNY）
4. 一天一个数据点（用收盘）

幂等：写之前先把 jsonl 截掉旧数据 → 重新写。

跑法：python -m scripts.backfill_pnl_history
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yfinance as yf

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"

# 黄金交易明细从 git-ignored 私有文件读取，绝不硬编码到入库代码
# Schema 见 memory/.state/gold_trades.private.json 注释；不存在时用合成 demo
# 数据，让公开仓库 clone 的人能跑通流程但拿不到任何真实人的持仓信息
PRIVATE_TRADES_PATH = ROOT / "memory" / ".state" / "gold_trades.private.json"

DEMO_GOLD_TRADES: List[Dict] = [
    # 演示用合成数据：单笔 10g 买入 @ ¥500，便于跑通脚本；与任何真实账户无关
    {"ts": "2024-01-15 10:00:00", "kind": "买金-演示", "grams": 10.0, "price": 500.00, "total": 5000.00},
]


def _load_gold_trades() -> List[Dict]:
    """优先读私有 JSON，没有则用合成数据。

    将真实交易明细从 git 仓库剥离出去（audit C1），避免：
    - 公开仓库被 clone 后精确反推持仓规模 / 入场点位
    - 分钟级时间戳被用于浙商客服侧的社工对账
    """
    if PRIVATE_TRADES_PATH.exists():
        with open(PRIVATE_TRADES_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("trades", [])
    return DEMO_GOLD_TRADES


GOLD_OZ_PER_GRAM = 31.1035

# 浙商积存金点差（实测 ≈ 0%，可被 strategy.md target_assets[gold].price_offset_pct
# 覆盖；本脚本是 one-shot backfill，不动态读 strategy 简化）
GOLD_OFFSET_PCT = 0.0

# 回填窗口：只填最近 N 天。早期数据有 yfinance GC=F 现货价 vs 浙商积存金
# 实际报价的固有 spread（银行点差 + 期货-现货差），算出来会有 -27% 的虚假浮亏，
# 误导性强。最近 60 天用户的均价已经接近最新 spot，spread 可忽略。
BACKFILL_DAYS = 60
END_DATE = datetime.now().date()
START_DATE = max(
    datetime(2025, 3, 27).date(),    # 不早于黄金首笔
    END_DATE - timedelta(days=BACKFILL_DAYS),
)


def _fetch_yf(symbol: str) -> Dict[str, float]:
    """yfinance 拉日线，返回 {YYYY-MM-DD: close}"""
    df = yf.Ticker(symbol).history(
        start=(START_DATE - timedelta(days=5)).isoformat(),
        end=(END_DATE + timedelta(days=1)).isoformat(),
    )
    return {idx.strftime("%Y-%m-%d"): float(row["Close"]) for idx, row in df.iterrows()}


def _last_close_on_or_before(prices: Dict[str, float], date_str: str) -> Optional[float]:
    """如果 date_str 当天没数据（周末/假期），回溯最近的有效收盘"""
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    for delta in range(0, 7):
        d = (target - timedelta(days=delta)).strftime("%Y-%m-%d")
        if d in prices:
            return prices[d]
    return None


def _gold_state_at(date_str: str, trades: List[Dict]) -> Tuple[float, float]:
    """该日期收盘时（含当日交易），累计 grams 和 cumulative cost (CNY)"""
    cutoff = datetime.strptime(date_str, "%Y-%m-%d").date()
    grams = 0.0
    cost = 0.0
    for t in trades:
        trade_date = datetime.strptime(t["ts"][:10], "%Y-%m-%d").date()
        if trade_date <= cutoff:
            grams += t["grams"]
            # 赠金 cost 算 0（白来的不计成本，但计 grams）
            if t["kind"] != "赠金":
                cost += t["total"]
    return grams, cost


def main() -> None:
    trades = _load_gold_trades()
    if trades is DEMO_GOLD_TRADES:
        print(f"⚠️ 未找到 {PRIVATE_TRADES_PATH}，使用合成 demo 数据。")
        print(f"   要用真实数据请把交易明细写到该路径（git ignored）")
    else:
        print(f"📋 加载真实交易明细 {len(trades)} 笔（来源: gold_trades.private.json）")
    print(f"📡 拉取 yfinance 历史数据 ({START_DATE} → {END_DATE})...")
    gc_prices = _fetch_yf("GC=F")
    usdcny_prices = _fetch_yf("USDCNY=X")
    audcny_prices = _fetch_yf("AUDCNY=X")
    ndq_prices = _fetch_yf("NDQ.AX")
    print(f"  GC=F: {len(gc_prices)} 天, USDCNY: {len(usdcny_prices)}, "
          f"AUDCNY: {len(audcny_prices)}, NDQ.AX: {len(ndq_prices)}")

    # NDQ 持仓用当前 portfolio 里的字段（用户没给历史买入日期，假设全程持有）
    store = MemoryStore()
    portfolio = store.read("portfolio")
    if portfolio is None:
        print("❌ memory/portfolio.md 不存在")
        sys.exit(1)
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    ndq_avg = float(portfolio.get("ndq_avg_cost_aud_per_share", 0) or 0)
    print(f"  NDQ 持仓: {ndq_shares} 股 @ ${ndq_avg}/股 AUD")

    # 逐日生成
    snapshots: List[Dict] = []
    current = START_DATE
    while current <= END_DATE:
        date_str = current.strftime("%Y-%m-%d")
        # 跳过 yfinance 完全没数据的日期（最早期市场假期）—— 让最近的有效价代填
        gc_usd = _last_close_on_or_before(gc_prices, date_str)
        usdcny = _last_close_on_or_before(usdcny_prices, date_str)
        audcny = _last_close_on_or_before(audcny_prices, date_str)
        ndq_close = _last_close_on_or_before(ndq_prices, date_str)

        if not (gc_usd and usdcny):
            current += timedelta(days=1)
            continue

        # 黄金当日克价 + 该日累计持仓 + 平均成本
        # 算 spot 克价后乘 (1+offset) 得"浙商口径估值价"，与用户买入价同基础
        # （audit financial C2: 不修这里 backfill 历史浮盈系统性偏低 1-1.5%）
        gold_spot = (gc_usd / GOLD_OZ_PER_GRAM) * usdcny
        gold_now = gold_spot * (1 + GOLD_OFFSET_PCT)
        gold_grams, gold_cost = _gold_state_at(date_str, trades)
        if gold_grams > 0 and gold_cost > 0:
            gold_avg = gold_cost / gold_grams
            gold_pnl_pct = ((gold_now / gold_avg) - 1) * 100
            gold_value_cny = gold_now * gold_grams
        else:
            gold_pnl_pct = None
            gold_avg = 0.0
            gold_value_cny = 0.0

        # NDQ
        if ndq_shares > 0 and ndq_avg > 0 and ndq_close:
            ndq_pnl_pct = ((ndq_close / ndq_avg) - 1) * 100
            ndq_cost_cny = ndq_avg * ndq_shares * (audcny or 4.7)
            ndq_value_cny = ndq_close * ndq_shares * (audcny or 4.7)
        else:
            ndq_pnl_pct = None
            ndq_cost_cny = 0.0
            ndq_value_cny = 0.0

        # Total（按 cost 加权）
        total_cost = gold_cost + ndq_cost_cny
        total_value = gold_value_cny + ndq_value_cny
        total_pnl_pct = ((total_value / total_cost) - 1) * 100 if total_cost > 0 else 0.0

        snapshots.append({
            "ts": current.strftime("%Y-%m-%dT16:00:00+08:00"),  # 模拟收盘时刻
            "total_pnl_pct": round(total_pnl_pct, 4),
            "ndq_pnl_pct": round(ndq_pnl_pct, 4) if ndq_pnl_pct is not None else None,
            "gold_pnl_pct": round(gold_pnl_pct, 4) if gold_pnl_pct is not None else None,
        })
        current += timedelta(days=1)

    # 截掉旧 jsonl 重写（幂等）
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"\n✅ 写入 {len(snapshots)} 个数据点 → {HISTORY_PATH}")
    print(f"   首日 ({snapshots[0]['ts'][:10]}): "
          f"total {snapshots[0]['total_pnl_pct']:+.2f}%, "
          f"gold {snapshots[0]['gold_pnl_pct']:+.2f}%")
    print(f"   末日 ({snapshots[-1]['ts'][:10]}): "
          f"total {snapshots[-1]['total_pnl_pct']:+.2f}%, "
          f"gold {snapshots[-1]['gold_pnl_pct']:+.2f}%")
    print("\n下一步：python -m jobs.pnl_snapshot  渲染 SVG")


if __name__ == "__main__":
    main()
