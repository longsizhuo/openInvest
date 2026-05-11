"""Committee 历史回测 — 让 4 角色 agent 在历史日期上做决策，blind 未来。

设计：
- 对历史日期 D ∈ [start, end] 逐日跑：
  1. 给 LLM 的 tool 全部 patch 成"只能看 D 之前的数据"
  2. 跑完整 committee（4 角色 + Round 2 + CIO）
  3. 落到 memory/.backtest/<D>/<symbol>.md（不污染真实 .committee/）
- jobs/verdict_review.py 后续可以读 .backtest/ + .committee/ 一起算命中率

关键：blind 未来
- get_history_data(symbol, period) 自动截到 D 之前
- get_macro_snapshot 自动截到 D 之前
- 不能调 DDGS 新闻搜索（SDKAgent 5 个 tool 里本来就没有，OK）
- get_recent_committee_verdicts 自动跳过 .backtest/ 自身（避免环依赖）

跑法：
  python -m scripts.backtest_committee --start 2026-03-01 --end 2026-04-25 --assets NDQ.AX,GC=F
  python -m scripts.backtest_committee --days 30  # 简化：跑最近 30 天

注意：
- 每日每资产 1 次 committee = 6 次 LLM 调用 = 30 天 × 2 资产 = 360 次 LLM call
- DeepSeek 按 token 计费，估 ¥3-8/次完整 backtest（看 prompt + tool call 深度）
- 跑时间 30 天 × 2 资产 × ~30s = ~30min
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")  # 让 _create_agent 能拿到 DEEPSEEK_API_KEY

from core.memory_store import MemoryStore  # noqa: E402

# 默认资产
DEFAULT_ASSETS = ["NDQ.AX", "GC=F"]


def _patch_tools_to_date(decision_date: str):
    """返回一个 context manager，把 5 个 tool 的实现全部 patch 成'只看 decision_date 之前'。

    blind 未来的核心机制：
    - get_history_data(period='1y') 不再用 yfinance 默认（拉到今天），改成 history(end=decision_date)
    - analyze_multi_timeframe 同样
    - get_macro_snapshot 取 decision_date 当天的 close
    - query_dreaming_insights 跳过 decision_date 之后写入的 insight
    - get_recent_committee_verdicts 只看 decision_date 之前的决议
    """
    from contextlib import ExitStack
    from datetime import datetime, timedelta as _td

    cutoff = datetime.strptime(decision_date, "%Y-%m-%d")
    next_day = (cutoff + _td(days=1)).strftime("%Y-%m-%d")

    stack = ExitStack()

    # === 1. get_history_data：传 as_of_date 让底层 DB cache 也按 cutoff 过滤 ===
    # 注：底层 _apply_cutoff 用 `df.index <= cutoff` (含当日)，跟旧逻辑等价
    import utils.exchange_fee as ef
    real_get_history = ef.get_history_data

    def patched_get_history(symbol: str, period: str = "2y", as_of_date=None):
        # 强制传 cutoff，忽略调用方意外的 as_of_date 参数
        return real_get_history(symbol, period, as_of_date=decision_date)

    stack.enter_context(patch.object(ef, "get_history_data", patched_get_history))

    # === 2. analyze_multi_timeframe 内部用 get_history_data，已自动截断 ===

    # === 3. get_macro_snapshot 内部用 get_history_data，已自动截断 ===

    # === 4. query_dreaming_insights：filter insight 写入时间 ===
    import agents.tools as tools

    real_dreaming = tools._impl_query_dreaming_insights

    def patched_dreaming(asset_symbol: str, top_k: int = 3):
        # backtest 阶段先简化为'不返回任何 insight'，避免过去用真实 insight 污染
        # 真实 backtest 应该按 mtime 过滤，但当前 insights/ 数据少，先用空集兜底
        return []

    stack.enter_context(patch.object(tools, "_impl_query_dreaming_insights", patched_dreaming))
    stack.enter_context(patch.dict(tools.TOOL_IMPL,
                                    {"query_dreaming_insights": patched_dreaming}))

    # === 5. get_recent_committee_verdicts：只看 cutoff 前的决议 ===
    real_recent = tools._impl_get_recent_committee_verdicts

    def patched_recent(asset_symbol: str, n: int = 5):
        all_results = real_recent(asset_symbol, 100)  # 拿全部
        return [r for r in all_results if r.get("date", "9999") < decision_date][:n]

    stack.enter_context(patch.object(tools,
                                      "_impl_get_recent_committee_verdicts", patched_recent))
    stack.enter_context(patch.dict(tools.TOOL_IMPL,
                                    {"get_recent_committee_verdicts": patched_recent}))

    return stack


def run_one_day(decision_date: str, asset_symbols: List[str]) -> Dict[str, Any]:
    """对单个历史日期跑一次完整 committee（每个资产）"""
    from core.committee import (
        run_macro_view, run_committee, parse_cio_memo, _persist
    )
    from utils.exchange_fee import get_macro_data, analyze_multi_timeframe, get_history_data

    store = MemoryStore()
    out_dir_base = store.root / ".backtest" / decision_date
    out_dir_base.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Any] = {"date": decision_date, "verdicts": {}}

    print(f"\n📅 [{decision_date}] 跑 backtest...")

    with _patch_tools_to_date(decision_date):
        # Macro 一次跨资产共享
        try:
            macro_data = get_macro_data()
            macro_view = run_macro_view(macro_data)
        except Exception as e:
            macro_view = f"[backtest macro failed: {e}]"

        for symbol in asset_symbols:
            asset = {
                "symbol": symbol,
                "display_name": symbol,
                "currency": "AUD" if symbol == "NDQ.AX" else "CNY",
            }
            try:
                df = get_history_data(symbol, "2y")
                market_data = analyze_multi_timeframe(df, symbol) if not df.empty else "(no data)"

                # 极简 portfolio_summary（backtest 不知道当时用户持仓）
                portfolio_summary = (
                    f"# Backtest Mode\n"
                    f"该日期为历史回测，用户持仓上下文无法精确还原。\n"
                    f"假设用户持仓中性（无极端集中度），focus 在技术 + 宏观信号。"
                )

                # max_debate_rounds 从环境变量读（Optuna 训练用）
                import os as _os
                max_rounds = int(_os.getenv("INVEST_MAX_DEBATE_ROUNDS", "1"))
                result = run_committee(
                    asset=asset,
                    market_data=market_data,
                    macro_view=macro_view,
                    portfolio_summary=portfolio_summary,
                    prior_insights="",  # backtest 不用 insights 防穿越
                    persist_to_memory=False,  # 我们手动 persist 到 .backtest/
                    max_debate_rounds=max_rounds,
                )

                # 手动 persist 到 .backtest/<date>/
                report = result["report"]
                verdict = result["verdict"]
                _persist(report, verdict, output_dir=out_dir_base, date_override=decision_date)

                results["verdicts"][symbol] = {
                    "verdict": verdict["verdict"],
                    "confidence": verdict["confidence"],
                    "alloc_cny": verdict["alloc_cny"],
                }
                print(f"  ✓ {symbol}: {verdict['verdict']} (conf {verdict['confidence']:.2f})")
            except Exception as e:
                print(f"  ✗ {symbol}: failed {type(e).__name__}: {e}")
                results["verdicts"][symbol] = {"error": str(e)[:200]}

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD（包含），默认昨天")
    parser.add_argument("--days", type=int, help="或指定最近 N 天（覆盖 --start/--end）")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS),
                        help="逗号分隔，默认 NDQ.AX,GC=F")
    parser.add_argument("--step", type=int, default=1, help="日期步长（默认 1=每天）")
    parser.add_argument("--limit", type=int, help="最多跑 N 个日期（debug 用）")
    parser.add_argument(
        "--allow-lookahead", action="store_true",
        help="（不推荐）允许 backtest 日期超过 DeepSeek 训练数据截止（约 2024-06-30）。"
             "默认拒绝，因为模型已经'见过'那段历史，命中率会虚高、不可信。",
    )
    args = parser.parse_args()

    # 解析时间范围
    today = datetime.now().date()
    if args.days:
        end = today - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start \
                else (today - timedelta(days=30))
        end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end \
              else (today - timedelta(days=1))

    asset_symbols = [s.strip() for s in args.assets.split(",") if s.strip()]

    # 生成日期列表（跳过周末，市场闭市天没数据）
    dates: List[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 周一到周五
            dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=args.step)

    if args.limit:
        dates = dates[:args.limit]

    # 防 LLM 训练数据穿越：DeepSeek-Chat 训练数据估算截止 2024-06-30，
    # 之后的 backtest 模型已经"见过"市场走势，命中率虚高不可信
    LLM_TRAINING_CUTOFF = "2024-06-30"
    if not args.allow_lookahead and dates:
        too_late = [d for d in dates if d > LLM_TRAINING_CUTOFF]
        if too_late:
            print(
                f"\n❌ Refused：{len(too_late)} 个日期 (e.g. {too_late[0]}) 超过 "
                f"DeepSeek 训练数据截止 {LLM_TRAINING_CUTOFF}。\n"
                f"   LLM 已经'见过'这段市场，命中率会虚高、不可信。\n"
                f"   选项：\n"
                f"   - 把 --end 改到 {LLM_TRAINING_CUTOFF} 之前（推荐）\n"
                f"   - 加 --allow-lookahead flag 强制跑（仅作上限估计）"
            )
            return

    print(f"🔬 Backtest plan: {len(dates)} 个交易日 × {len(asset_symbols)} 资产 = "
          f"{len(dates) * len(asset_symbols)} 次 committee")
    print(f"   日期范围: {dates[0]} → {dates[-1]}")
    print(f"   资产: {asset_symbols}")
    print(f"   预估 LLM 调用: ~{len(dates) * len(asset_symbols) * 6} 次")

    all_results: List[Dict[str, Any]] = []
    for d in dates:
        r = run_one_day(d, asset_symbols)
        all_results.append(r)

    print(f"\n✅ Backtest 完成，{len(all_results)} 个交易日已写入 memory/.backtest/")
    print(f"\n下一步：python -m jobs.verdict_review 算命中率")


if __name__ == "__main__":
    main()
