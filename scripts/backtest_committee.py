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
# 污染 cutoff 单一可信源——落盘 `**Contaminated**` 标记与 verdict_review 分桶共用同一常量，
# 不让 backtest 和聚合两处各自硬编码 "2024-12-31" 漂移（机器强制，见 CLAUDE.md）。
from jobs.verdict_review import CONTAMINATION_CUTOFF  # noqa: E402

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

    # === 0. 根上截断 MarketStore.get_history_df ===
    # 不再只 patch ef.get_history_data 包装层（那靠"path-profile/FX 恰好没被调用"才不漏，很脆）。
    # 直接 patch 底层 store 方法：任何路径（get_path_profile / 汇率腿 / 技术面）直读 store 都按
    # decision_date 截断。idempotent：先取全量 → 按 cutoff 过滤 → 再 tail(days)，语义不变。
    import db.market_store as _ms
    import pandas as _pd0
    _real_ghdf = _ms.MarketStore.get_history_df

    def _patched_ghdf(self, symbol, days=730):
        df = _real_ghdf(self, symbol, days=100000)
        if df is None or df.empty:
            return df
        df = df[df.index <= _pd0.to_datetime(decision_date)]
        return df.tail(days)

    stack.enter_context(patch.object(_ms.MarketStore, "get_history_df", _patched_ghdf))

    # === 1. get_history_data：传 as_of_date 让底层 DB cache 也按 cutoff 过滤 ===
    # 注：底层 _apply_cutoff 用 `df.index <= cutoff` (含当日)，跟旧逻辑等价
    import utils.exchange_fee as ef
    real_get_history = ef.get_history_data

    def patched_get_history(symbol: str, period: str = "2y", as_of_date=None):
        # 强制传 cutoff，忽略调用方意外的 as_of_date 参数。
        #
        # 第二个 bug 修复（验证时发现）：底层 get_history_data 是先 get_history_df
        # 的 tail(730)（取最新到今天的 730 行）再 _apply_cutoff，对历史决议日只剩
        # cutoff 之前落在"最新 730 行窗口"里的那部分（2024-04-02 只剩 ~186 行）
        # → MA250 永远 None、早期日 MA120 也 None → regime 退化 unknown。
        # 这里按正确顺序重算：取全历史 → 按 decision_date 截断 → 再 tail(730)，
        # 保证 cutoff 之前有 ≥250 根可算 MA250。仅作用于 backtest 路径，不动 live
        # get_history_data 逻辑。DB 已回填历史（含 OHLC），直接读 store 即可。
        import pandas as _pd
        df = ef._STORE.get_history_df(symbol, days=100000)
        if df is None or df.empty:
            # 兜底：DB 没数据时退回原实现（含 yfinance/CSV 兜底）
            return real_get_history(symbol, period, as_of_date=decision_date)
        df = df[df.index <= _pd.to_datetime(decision_date)]
        return df.tail(730)

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


def run_one_day(decision_date: str, asset_symbols: List[str],
                *, resume: bool = True,
                portfolio_summary_override: "str | None" = None,
                out_subdir: str = ".backtest") -> Dict[str, Any]:
    """对单个历史日期跑一次完整 committee（每个资产）

    resume=True（默认，CLI 分片回测用）：已写出 <symbol>.md 的资产跳过，支持断点续跑。
    resume=False（run_walk_forward 用）：无条件跑全部资产——walk_forward 每次从零重建
        PaperTradeSimulator，必须拿到每天真实 verdict 才能正确回放成交，跳过会让权益
        曲线/指标算在残缺成交集上（静默错误），所以它不能用断点续跑。
    """
    from core.committee import (
        run_macro_view, run_committee, parse_cio_memo, _persist, safe_symbol
    )
    # 必须用模块属性引用（ef.xxx），不能 `from utils.exchange_fee import get_history_data`。
    # 后者在 with _patch_tools_to_date 之前就把局部名绑到原始未 patch 函数，导致
    # cutoff patch 逃逸 → 每个决议日都拿最新数据（未来函数泄漏）。走 ef.get_history_data
    # 在调用时按模块属性查找，命中 patch.object(ef, "get_history_data", ...) 的替换。
    import utils.exchange_fee as ef
    # 确定性 regime（与 live 路径同款 classify_regime）。纯函数，从已按 decision_date
    # 截断的 df 算，无穿越。
    from core.regime import format_regime_brief
    from utils.market_metrics import compute_metrics

    store = MemoryStore()
    # out_subdir 让闭环回测写到独立目录(.backtest_closedloop),不覆盖空桩基线(.backtest)
    out_dir_base = store.root / out_subdir / decision_date
    out_dir_base.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Any] = {"date": decision_date, "verdicts": {}}

    # 断点续跑（仅 resume=True）：已写出 <symbol>.md 的资产跳过；全部已跑则连 macro 都
    # 不跑直接返回。文件名口径用 persist.safe_symbol 单一可信源（避免与 _persist 漂移）。
    if resume:
        from core.committee import safe_symbol  # 走 façade（与本文件其它 core.committee 导入同口径，import-linter 例外覆盖）
        pending = [s for s in asset_symbols
                   if not (out_dir_base / f"{safe_symbol(s)}.md").exists()]
        if not pending:
            print(f"\n⏭ [{decision_date}] 全部资产已跑，跳过（断点续跑）")
            return {"date": decision_date,
                    "verdicts": {s: {"skipped": True} for s in asset_symbols}}
    else:
        pending = list(asset_symbols)

    print(f"\n📅 [{decision_date}] 跑 backtest... (待跑 {len(pending)}/{len(asset_symbols)})")

    with _patch_tools_to_date(decision_date):
        # Macro 一次跨资产共享
        try:
            macro_data = ef.get_macro_data()
            macro_view = run_macro_view(macro_data)
        except Exception as e:
            macro_view = f"[backtest macro failed: {e}]"

        # 当日各资产相互独立(共享同一 macro_view + portfolio 快照),并行跑 = 3× 提速。
        # 全在 _patch_tools_to_date 内(patch 是日期级 process-global,所有线程同一 cutoff);
        # LLM 是 IO-bound(GIL 在 IO 释放);_persist 各资产独立路径;llm_usage append <4096B 原子。
        def _run_symbol(symbol):
            asset = {
                "symbol": symbol,
                "display_name": symbol,
                "currency": "AUD" if symbol == "NDQ.AX" else "CNY",
            }
            try:
                df = ef.get_history_data(symbol, "2y")
                if df is None or df.empty or len(df) < 30:
                    print(f"  ⏭ {symbol}: skipped (insufficient history: {len(df) if df is not None else 0} rows)")
                    return symbol, None
                market_data = ef.analyze_multi_timeframe(df, symbol)

                # 确定性 regime brief（双触发器 crash / recovery / per-asset 阈值），
                # 用同一份已截断的 df 算 → 与 live 路径一致，注入委员会，不再让 LLM 瞎猜 REGIME。
                regime_brief = format_regime_brief(compute_metrics(df), symbol=symbol) if not df.empty else ""

                # 默认中性空桩(ADR-022 T3:委员会看不到持仓→从不减仓→verdict 不可外推 live)。
                # 闭环回测传 portfolio_summary_override(模拟器当前真实仓位:含浮亏/集中度/剩余现金)
                # → 委员会能像 live 一样 de-risk → 才测得出择时 skill。同一份 override 当日各资产共用
                # (对齐 live:日内委员会都看当日开盘时的组合,执行在决策之后)。
                if portfolio_summary_override is not None:
                    portfolio_summary = portfolio_summary_override
                else:
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
                    regime_brief=regime_brief,  # 注入确定性 regime（与 live 一致）
                    persist_to_memory=False,  # 我们手动 persist 到 .backtest/
                    max_debate_rounds=max_rounds,
                )

                # 手动 persist 到 .backtest/<date>/
                report = result["report"]
                verdict = result["verdict"]
                _persist(report, verdict, output_dir=out_dir_base, date_override=decision_date)

                # 机器强制污染标记：决议日 ≤ cutoff = 落在 LLM 训练窗口（记忆穿越非业绩）。
                # 追加一行到 _persist 刚写的 transcript（不改 _persist 共享格式），verdict_review
                # 分桶靠决议日同一常量判定，这行是 transcript 自带的审计留痕。
                md_path = out_dir_base / f"{safe_symbol(symbol)}.md"
                contaminated = decision_date <= CONTAMINATION_CUTOFF
                with md_path.open("a", encoding="utf-8") as f:
                    f.write(
                        f"\n\n---\n\n**Contaminated**: {str(contaminated).lower()} "
                        f"(decision_date {decision_date} "
                        f"{'<=' if contaminated else '>'} cutoff {CONTAMINATION_CUTOFF})\n"
                    )

                print(f"  ✓ {symbol}: {verdict['verdict']} (conf {verdict['confidence']:.2f})")
                return symbol, {
                    "verdict": verdict["verdict"],
                    "confidence": verdict["confidence"],
                    "alloc_cny": verdict["alloc_cny"],
                }
            except Exception as e:
                print(f"  ✗ {symbol}: failed {type(e).__name__}: {e}")
                return symbol, {"error": str(e)[:200]}

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(pending) or 1) as ex:
            for symbol, vd in ex.map(_run_symbol, pending):
                if vd is not None:
                    results["verdicts"][symbol] = vd

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
        help="（不推荐）允许 backtest 日期超过模型训练截止。默认拒绝，因为模型已经'见过'"
             "那段历史，命中率会虚高、不可信（仅作行为一致性检查用，不是干净业绩）。",
    )
    parser.add_argument(
        "--holdout", action="store_true",
        help="干净验证模式：只跑模型训练截止【之后】且留够远期窗口能测实际收益的日期"
             "（MiMo 没见过 → 无记忆穿越 → 这才是唯一可信的预测/业绩验证）。",
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

    # 模型训练截止（防记忆穿越）。2026-06-25 实测 MiMo mimo-v2.5-pro：自报训练到 2024 末，
    # 知 2024-11 大选 + Trump 47 任，但不知 2025 年中金价/真实 2025 市场事件 → cutoff 取 2024-12-31。
    # （旧值 2024-06-30 + "DeepSeek" 注释是没核对过的臆测。）
    # 穿越走绝对价位非日期(ADR-022);别把"prompt 无日期"当"早段干净"。cutoff 是 MiMo 自报非实证,若真实更晚则 holdout 头部被污染。
    LLM_TRAINING_CUTOFF = CONTAMINATION_CUTOFF  # 单一可信源（同 verdict_review 分桶/落盘标记）
    FORWARD_BUFFER_DAYS = 95  # 留够 90d 远期窗口，verdict_review 才能用实际后市收益评分

    if args.holdout:
        # 干净验证：只跑 cutoff 之后（模型没见过）+ 留够远期窗口（能测实际收益）的日期
        hold_end = (today - timedelta(days=FORWARD_BUFFER_DAYS)).strftime("%Y-%m-%d")
        dates = [d for d in dates if d > LLM_TRAINING_CUTOFF and d <= hold_end]
        if not dates:
            print(f"\n❌ holdout 窗口为空：需 {LLM_TRAINING_CUTOFF} < date ≤ {hold_end}")
            return
        print(f"\n🧪 HOLDOUT 干净验证：cutoff {LLM_TRAINING_CUTOFF} 之后 + 留 "
              f"{FORWARD_BUFFER_DAYS}d 远期窗口 → {dates[0]} .. {dates[-1]}（MiMo 未见过，无记忆穿越）")
    elif not args.allow_lookahead and dates:
        too_late = [d for d in dates if d > LLM_TRAINING_CUTOFF]
        if too_late:
            print(
                f"\n❌ Refused：{len(too_late)} 个日期 (e.g. {too_late[0]}) 超过模型训练截止 "
                f"{LLM_TRAINING_CUTOFF}。\n"
                f"   注意：cutoff【之前】= 模型已见过 = 有记忆穿越 → 只能当行为一致性检查，不是干净业绩；\n"
                f"   cutoff【之后】才是干净验证。想跑可信的预测/业绩验证请用 --holdout。\n"
                f"   - 把 --end 改到 {LLM_TRAINING_CUTOFF} 之前（污染，仅一致性检查）\n"
                f"   - 用 --holdout（推荐：唯一可信的预测/业绩验证）\n"
                f"   - 加 --allow-lookahead 强制跑（仅作上限估计）"
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
