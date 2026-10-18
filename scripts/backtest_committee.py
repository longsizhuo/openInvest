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
  python -m scripts.backtest_committee --prospective --assets ... [--t2-confidence-arm] [--sealed-arm]
      # 前瞻舰队（cron）；可选试跑臂（均写独立目录，live 不变）：T2 置信度定义 / B4 sealed（每标的 N 次抽样取多数）/
      # debate（rounds=4，= live 日报配置）/ macro-soft（CIO 规则 2：Macro 偏空打折不否决）

注意：
- 每日每资产 1 次 committee = 6 次 LLM 调用 = 30 天 × 2 资产 = 360 次 LLM call
- DeepSeek 按 token 计费，估 ¥3-8/次完整 backtest（看 prompt + tool call 深度）
- 跑时间 30 天 × 2 资产 × ~30s = ~30min
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")  # 让 _create_agent 能拿到 DEEPSEEK_API_KEY

from openinvest.core.memory_store import MemoryStore  # noqa: E402
# 污染 cutoff 单一可信源——落盘 `**Contaminated**` 标记与 verdict_review 分桶共用同一常量，
# 不让 backtest 和聚合两处各自硬编码 "2024-12-31" 漂移（机器强制，见 CLAUDE.md）。
from openinvest.jobs.verdict_review import (  # noqa: E402
    CONTAMINATION_CUTOFF, DEBATE_ARM_SUBDIR, MACROSOFT_ARM_SUBDIR, SEALED_ARM_SUBDIR,
)
from openinvest.jobs.review_calc import EXPECTED_DIRECTION, UNCLEAR_DIRECTION  # noqa: E402

# 默认资产
DEFAULT_ASSETS = ["NDQ.AX", "GC=F"]

# 前瞻舰队要求的行情新鲜度：该标的最新 bar 必须是"今天"，否则跳过。
# 决策必须基于当日数据；缓存老化时静默用陈旧收盘价出 verdict、却盖当天日期，
# 会让 verdict_review 拿真实后市给错标的样本打分（CR 2026-07-24 首条发现）。
# 2026-10-07：原先"≤5 天"的阈值放过了周末（周五 bar 只旧 1 天）→ 7/25 起每个
# 周末 50 标的用周五收盘价各出两份重复样本（22 天 ≈1100 份）。cron 在 22:30 UTC
# （美盘收盘 + backfill 22:00 之后），当日开市的标的都已有当日 bar；休市（周末/
# 各市场假日）自然没有 → 跳过；加密 7×24 照常。

# D11 P2 舰队试跑：CIO 的 CONFIDENCE 改用 T2 定义（verdict_review 30 天命中概率）。
# 只由 `--prospective --t2-confidence-arm` 打开，作为同日同标的的第二臂（对照臂 =
# 原 memory/.backtest/ 不变）。live 委员会不 import 本脚本 → live prompt 逐字节不变。
# T2 臂写独立目录：verdict_review / dreaming / #141 样本计数都只读 .backtest，碰不到它。
T2_ARM_SUBDIR = ".backtest_t2conf"
_CIO_085_RULE = "1. **三方一致**: confidence ≥ 0.85，按一致方向给 verdict"
_CIO_T2_ANCHOR = "**Verdict 选项**"


def build_cio_prompt_t2(asset: Dict[str, Any], json_mode: bool = False) -> str:
    """live CIO prompt 的 T2 变体：删"三方一致 ≥0.85"数字规则 + 注入 CONFIDENCE 定义。

    锚点失效（cio.md 改了措辞）直接抛错：T2 臂宁可停也不静默跑出"定义 + 0.85"混合 prompt。
    """
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.i18n import bilingual

    prompt = build_cio_prompt(asset, json_mode=json_mode)
    for anchor in (_CIO_085_RULE, _CIO_T2_ANCHOR):
        if prompt.count(anchor) != 1:
            raise RuntimeError(f"cio.md 已变，T2 变体锚点失效：{anchor!r}")
    definition = bilingual(
        "**📏 CONFIDENCE 的定义（强制）**：\n"
        "CONFIDENCE = 这条 VERDICT 在 30 个日历天后被系统复盘判为「命中」的概率（0.0-1.0），"
        "不是你主观上有多自信。复盘规则是确定性的：\n"
        "- BUY / ACCUMULATE：30 天后价格高于今天 = 命中\n"
        "- TRIM / SELL：30 天后价格低于今天 = 命中\n"
        "- HOLD：30 天涨跌幅的绝对值留在该资产的正常波动带内（≈ 日 ATR% × √30，封顶 8%）= 命中。"
        "所以 HOLD 的 CONFIDENCE 就是你判断「未来 30 天不会走出正常波动带」的概率\n"
        "照这个概率如实填：大约一半可能就写 0.5 左右，不要为显得果断而抬高，也不要为保守而压低。",
        "**📏 Definition of CONFIDENCE (mandatory)**:\n"
        "CONFIDENCE = the probability (0.0-1.0) that this VERDICT is judged a \"hit\" by the system's review "
        "30 calendar days from now -- not how sure you feel. The review rule is deterministic:\n"
        "- BUY / ACCUMULATE: price higher than today after 30 days = hit\n"
        "- TRIM / SELL: price lower than today after 30 days = hit\n"
        "- HOLD: the absolute 30-day return stays inside the asset's normal volatility band "
        "(~ daily ATR% x sqrt(30), capped at 8%) = hit. So a HOLD's CONFIDENCE is your probability that "
        "the next 30 days stay inside that normal band\n"
        "Report that probability honestly: if it is roughly a coin flip, write about 0.5; do not inflate it to "
        "sound decisive or deflate it to sound cautious.",
    )
    prompt = prompt.replace(_CIO_085_RULE, "1. **三方一致**: 按一致方向给 verdict")
    return prompt.replace(_CIO_T2_ANCHOR, f"{definition}\n\n{_CIO_T2_ANCHOR}")


def _t2_arm():
    """T2 臂唯一开关：换掉 run_committee 解析的 build_cio_prompt（钉 debate 命名空间，
    façade patch 无效）。patch 是进程级的，但两臂串行——对照臂跑完才进，退出即还原。"""
    return patch("openinvest.core.committee.debate.build_cio_prompt", build_cio_prompt_t2)


# B4 密封投票臂（评审路线 B4，2026-10）：对照臂 = 原 memory/.backtest/ 不变；实验臂 = 同日同标的、同输入
# （同一份 market_data / regime / 中性持仓 / macro_view），每个标的独立采样 N 次
# run_committee(max_debate_rounds=1)（Quant/Risk 并行互不可见，CIO 只看 Round-1 简报），按票聚合成一条裁决，
# 写 memory/.backtest_sealed/<今天>/。只由 `--prospective --sealed-arm` 打开；live 委员会不 import 本脚本，
# live 行为零改。产物含逐标的逐次裁决 → 只写数据目录（gitignored），不进仓库。
SEALED_DEFAULT_N = 3
SEALED_MIN_N = 3   # 少于 3 次凑不出"多数"，实验臂就和对照臂没区别


def aggregate_votes(votes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """N 次独立抽样 → 一条聚合裁决（纯函数，无 IO）。

    votes: [{"verdict": str, "confidence": float, "alloc": float}, ...]，按抽样顺序。
    - verdict：严格多数（票数 > N/2；N=3 即 ≥2 票相同）。没有多数（N=3 三者各异、N=4 的 2:2 …）
      → HOLD 且 split=True。
    - confidence := 最大票占比（N=3：1.0 / 0.67；三者各异 0.33），即用投票分裂度代替 CIO 自报分。
    - alloc := 多数票那几次金额的中位数；HOLD（含 split）恒 0（HOLD 不带建议金额）。
    - flip：N 次裁决的方向类（EXPECTED_DIRECTION；UNCLEAR=none）不全相同——与 A4
      （experiments/verdict_variance）的翻转口径同一把尺子，数字可直接对比。
    - rep：代表票下标 = 多数票里最早的一次（split 取 0）；落盘的 transcript 正文取它。
    """
    n = len(votes)
    if n < 1:
        raise ValueError("aggregate_votes 需要至少 1 票")
    top_verdict, top = Counter(v["verdict"] for v in votes).most_common(1)[0]
    split = top * 2 <= n
    verdict = "HOLD" if split else top_verdict
    majority = [i for i, v in enumerate(votes) if v["verdict"] == top_verdict]
    alloc = 0 if verdict == "HOLD" else round(statistics.median(votes[i]["alloc"] for i in majority))
    dirs = {EXPECTED_DIRECTION.get(v["verdict"], UNCLEAR_DIRECTION) for v in votes}
    return {
        "verdict": verdict,
        "confidence": round(top / n, 2),
        "alloc": alloc,
        "split": split,
        "flip": len(dirs) > 1,
        "rep": 0 if split else majority[0],
    }


# B4 的另外两臂（同样只加臂，live 不变；输入与对照臂逐字相同，macro_view 借对照臂的）：
# - debate：rounds=4、单次抽样 = live 日报的配置（daily_report.py max_debate_rounds=4），写 .backtest_debate/。
#   回答"辩论有没有害"：对照臂和 sealed 臂都是 rounds=1，没有辩论的对照就测不出辩论的效果。
# - macro-soft：CIO prompt 变体，写 .backtest_macrosoft/。#461：Macro 单一共享信号 risk_off 否决全部买入
#   （cio.md:19 规则 2）；变体把规则 2 在"Macro risk_off 且 STRENGTH < 8"时由否决改为打折。
#   做法同 T2 臂：运行期 patch debate 命名空间里的 build_cio_prompt，live 委员会不 import 本脚本 → live prompt 逐字节不变；
#   cio.md 改了规则 2 的措辞 → 锚点失效直接报错，宁可停也不静默跑出别的 prompt。
DEBATE_ROUNDS = 4
_CIO_RULE2 = "2. **Quant vs Macro 分歧**: 看 Risk Officer 倒向哪边"


def build_cio_prompt_macro_soft(asset: Dict[str, Any], json_mode: bool = False) -> str:
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.i18n import bilingual

    prompt = build_cio_prompt(asset, json_mode=json_mode)
    if prompt.count(_CIO_RULE2) != 1:
        raise RuntimeError(f"cio.md 已变，macro-soft 变体锚点失效：{_CIO_RULE2!r}")
    exception = bilingual(
        "。\n   **例外——Macro 偏空但不否决（macro-soft 试跑变体）**：当 Macro SIGNAL=risk_off 且 Macro STRENGTH < 8、"
        "Quant 看多、Risk Officer 不是 high_risk 时，Macro 的 risk_off **不构成否决**：允许给 ACCUMULATE（仍不允许 BUY），"
        "SUGGESTED_ALLOC 取你按其余规则本来会给的金额 × (10 − Macro STRENGTH) / 10，并在备忘录里写明这个折扣系数；"
        "Macro STRENGTH ≥ 8 的 risk_off 仍然否决（HOLD）。",
        ".\n   **Exception -- Macro bearish but not a veto (macro-soft trial variant)**: when Macro SIGNAL=risk_off with "
        "Macro STRENGTH < 8, Quant is bullish and the Risk Officer is not high_risk, Macro's risk_off is **not a veto**: "
        "ACCUMULATE is allowed (BUY still is not), SUGGESTED_ALLOC = the amount you would otherwise give under the other rules "
        "x (10 - Macro STRENGTH) / 10, and state the discount factor in the memo; a risk_off with Macro STRENGTH >= 8 still vetoes (HOLD).",
    )
    return prompt.replace(_CIO_RULE2, _CIO_RULE2 + exception)


def _macro_soft_arm():
    """macro-soft 臂唯一开关（同 _t2_arm：钉 debate 命名空间，façade patch 无效；进程级但各臂串行，退出即还原）。"""
    return patch("openinvest.core.committee.debate.build_cio_prompt", build_cio_prompt_macro_soft)


def macro_signal_strength(macro_view: str) -> "tuple[str | None, float | None]":
    """从 Macro 输出抽 SIGNAL / STRENGTH（macro_view 是 "[backtest macro failed…]" 之类 → (None, None)）。"""
    import re
    sig = re.search(r"^SIGNAL:\s*(\S+)", macro_view or "", re.M)
    st = re.search(r"^STRENGTH:\s*(\d+(?:\.\d+)?)", macro_view or "", re.M)
    return (sig.group(1) if sig else None), (float(st.group(1)) if st else None)


def _currency_of(symbol: str) -> str:
    """按 yfinance 后缀判币种(CR:原先硬编码 'AUD' if NDQ.AX else 'CNY',
    50 标的舰队里 48 个被错标成 CNY,事实块里金额语义失真)。"""
    s = symbol.upper()
    for suffix, cur in ((".AX", "AUD"), (".HK", "HKD"), (".T", "JPY"),
                        (".L", "GBP"), (".DE", "EUR"), (".PA", "EUR"),
                        (".SS", "CNY"), (".SZ", "CNY")):
        if s.endswith(suffix):
            return cur
    if s.endswith("-USD") or s.startswith("^") or s.endswith("=F") or s.endswith("=X"):
        return "USD"
    return "USD"  # 裸 ticker 默认美股


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
    import openinvest.db.market_store as _ms
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
    import openinvest.utils.exchange_fee as ef
    real_get_history = ef.get_history_data

    def patched_get_history(symbol: str, period: str = "2y", as_of_date=None):
        # 强制传 cutoff，忽略调用方意外的 as_of_date 参数。
        #
        # 第二个 bug 修复（验证时发现）：底层 get_history_data 是先 get_history_df
        # 的 tail(730)（取最新到今天的 730 行）再 _apply_cutoff，对历史决议日只剩
        # cutoff 之前落在"最新 730 行窗口"里的那部分（2024-04-02 只剩 ~186 行）
        # → MA250 永远 None、早期日 MA120 也 None → regime 退化 unknown。
        # 这里按正确顺序重算：取全历史 → 按 decision_date 截断 → 再按 period 截
        # （2026-10 起与 live get_history_data 同一个 _apply_period，回测=实盘口径；
        # 此前 tail(730) 无视 period → 回测 macro "1mo MoM" 同样是 ~3 年涨跌）。
        # 不直接调 real_get_history：它对老 cutoff 会触发 yfinance 2y 拉取写库。
        import pandas as _pd
        df = ef._STORE.get_history_df(symbol, days=100000)
        if df is None or df.empty:
            # 兜底：DB 没数据时退回原实现（含 yfinance/CSV 兜底）
            return real_get_history(symbol, period, as_of_date=decision_date)
        df = df[df.index <= _pd.to_datetime(decision_date)]
        return ef._apply_period(df, period)

    stack.enter_context(patch.object(ef, "get_history_data", patched_get_history))

    # === 2. analyze_multi_timeframe 内部用 get_history_data，已自动截断 ===

    # === 3. get_macro_snapshot 内部用 get_history_data，已自动截断 ===

    # === 4. query_dreaming_insights：filter insight 写入时间 ===
    import openinvest.capabilities.tools as tools

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
                out_subdir: str = ".backtest",
                votes: int = 1,
                macro_view_override: "str | None" = None,
                max_rounds_override: "int | None" = None,
                arm_health: "Dict[str, Any] | None" = None) -> Dict[str, Any]:
    """对单个历史日期跑一次完整 committee（每个资产）

    resume=True（默认，CLI 分片回测用）：已写出 <symbol>.md 的资产跳过，支持断点续跑。
    resume=False（run_walk_forward 用）：无条件跑全部资产——walk_forward 每次从零重建
        PaperTradeSimulator，必须拿到每天真实 verdict 才能正确回放成交，跳过会让权益
        曲线/指标算在残缺成交集上（静默错误），所以它不能用断点续跑。
    votes（B4 密封投票臂）：1 = 原行为（INVEST_MAX_DEBATE_ROUNDS，默认 1，单次抽样）。
        ≥2 = 每个资产独立抽样 votes 次 run_committee(max_debate_rounds=1)，按 aggregate_votes
        聚合成一条裁决落盘，Input Health 块带 arm / votes / flip / split。任何一次抽样抛异常
        → 该资产整体失败、不落盘（下次续跑重来），保证臂内每一行都恰有 votes 票。
    macro_view_override：非 None 时不再重跑 Macro，直接用它（密封臂借对照臂的 macro_view，
        两臂 macro 输入逐字相同；None → 自己跑一次）。返回值里带 "macro_view" 供下一臂借用。
    max_rounds_override：非 None 时代替 INVEST_MAX_DEBATE_ROUNDS（debate 臂传 4）。votes>1 时恒为 1。
    arm_health：非 None = 单次抽样的实验臂（debate / macro-soft）——Input Health 多 arm 标签、macro_signal /
        macro_strength、final_round / converged，并把 debate_meta 一并落盘；None = 对照臂（落盘格式不变）。
    """
    from openinvest.core.committee import (
        run_macro_view, run_committee, parse_cio_memo, _persist, safe_symbol
    )
    # 必须用模块属性引用（ef.xxx），不能 `from utils.exchange_fee import get_history_data`。
    # 后者在 with _patch_tools_to_date 之前就把局部名绑到原始未 patch 函数，导致
    # cutoff patch 逃逸 → 每个决议日都拿最新数据（未来函数泄漏）。走 ef.get_history_data
    # 在调用时按模块属性查找，命中 patch.object(ef, "get_history_data", ...) 的替换。
    import openinvest.utils.exchange_fee as ef
    # 确定性 regime（与 live 路径同款 classify_regime）。纯函数，从已按 decision_date
    # 截断的 df 算，无穿越。
    from openinvest.core.regime import format_regime_brief
    from openinvest.utils.market_metrics import compute_metrics

    store = MemoryStore()
    # out_subdir 让闭环回测写到独立目录(.backtest_closedloop),不覆盖空桩基线(.backtest)
    out_dir_base = store.root / out_subdir / decision_date
    out_dir_base.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Any] = {"date": decision_date, "verdicts": {}}

    # 断点续跑（仅 resume=True）：已写出 <symbol>.md 的资产跳过；全部已跑则连 macro 都
    # 不跑直接返回。文件名口径用 persist.safe_symbol 单一可信源（避免与 _persist 漂移）。
    if resume:
        from openinvest.core.committee import safe_symbol  # 走 façade（与本文件其它 core.committee 导入同口径，import-linter 例外覆盖）
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
        # Macro 一次跨资产共享（密封臂借对照臂的，保证两臂 macro 输入相同）
        if macro_view_override is not None:
            macro_view = macro_view_override
        else:
            try:
                macro_data = ef.get_macro_data()
                macro_view = run_macro_view(macro_data)
            except Exception as e:
                macro_view = f"[backtest macro failed: {e}]"
        results["macro_view"] = macro_view
        macro_sig, macro_str = macro_signal_strength(macro_view)
        macro_keys = {"macro_signal": macro_sig, "macro_strength": macro_str}

        # 当日各资产相互独立(共享同一 macro_view + portfolio 快照),并行跑 = 3× 提速。
        # 全在 _patch_tools_to_date 内(patch 是日期级 process-global,所有线程同一 cutoff);
        # LLM 是 IO-bound(GIL 在 IO 释放);_persist 各资产独立路径;llm_usage append <4096B 原子。
        def _run_symbol(symbol):
            asset = {
                "symbol": symbol,
                "display_name": symbol,
                "currency": _currency_of(symbol),
            }
            try:
                df = ef.get_history_data(symbol, ef.METRICS_PERIOD)
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
                run_kwargs = dict(
                    asset=asset,
                    market_data=market_data,
                    macro_view=macro_view,
                    portfolio_summary=portfolio_summary,
                    prior_insights="",  # backtest 不用 insights 防穿越
                    regime_brief=regime_brief,  # 注入确定性 regime（与 live 一致）
                    persist_to_memory=False,  # 我们手动 persist 到 .backtest/
                )
                persist_extra: Dict[str, Any] = {}
                agg = None
                if votes > 1:
                    # 密封投票臂：同一份输入独立抽样 votes 次（rounds=1：Quant/Risk 互不可见），
                    # 串行（并发已在标的维度，BACKTEST_WORKERS）；任一次抛异常 → 走外层 except，不落盘。
                    runs = [run_committee(**run_kwargs, max_debate_rounds=1) for _ in range(votes)]
                    vote_list = [{"verdict": r["verdict"]["verdict"],
                                  "confidence": r["verdict"]["confidence"],
                                  "alloc": r["verdict"].get("alloc_cny") or 0} for r in runs]
                    agg = aggregate_votes(vote_list)
                    rep = runs[agg["rep"]]
                    report = rep["report"]
                    # 代表票的 verdict dict 当底；split 的 HOLD 是投票结果不是规则改写，丢掉 `_` 溯源键
                    base = {k: v for k, v in rep["verdict"].items()
                            if not (agg["split"] and k.startswith("_"))}
                    verdict = {**base, "verdict": agg["verdict"],
                               "confidence": agg["confidence"], "alloc_cny": agg["alloc"]}
                    if agg["split"]:
                        report = dataclasses.replace(report, cio_memo=(
                            report.cio_memo.rstrip() + "\n\n⚠️ sealed-vote：票数分裂"
                            f"（{'/'.join(v['verdict'] for v in vote_list)}），无多数 → HOLD"))
                    persist_extra = {
                        "debate_meta": rep["debate"],
                        "input_health": {"arm": f"sealed_n{votes}", "votes": vote_list,
                                         "flip": agg["flip"], "split": agg["split"], **macro_keys},
                    }
                else:
                    result = run_committee(
                        **run_kwargs, max_debate_rounds=max_rounds_override or max_rounds)
                    report = result["report"]
                    verdict = result["verdict"]
                    if arm_health is not None:
                        d = result["debate"]
                        persist_extra = {
                            "debate_meta": d,
                            "input_health": {**arm_health, **macro_keys,
                                             "final_round": d["final_round"], "converged": d["converged"]},
                        }

                # 手动 persist 到 .backtest/<date>/（密封臂：.backtest_sealed/<date>/）
                _persist(report, verdict, output_dir=out_dir_base, date_override=decision_date,
                         **persist_extra)

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

                tail = f" votes={'/'.join(v['verdict'] for v in vote_list)}" if agg else ""
                print(f"  ✓ {symbol}: {verdict['verdict']} (conf {verdict['confidence']:.2f}){tail}")
                out = {
                    "verdict": verdict["verdict"],
                    "confidence": verdict["confidence"],
                    "alloc_cny": verdict["alloc_cny"],
                }
                if agg:
                    out.update(votes=vote_list, flip=agg["flip"], split=agg["split"])
                return symbol, out
            except Exception as e:
                print(f"  ✗ {symbol}: failed {type(e).__name__}: {e}")
                return symbol, {"error": str(e)[:200]}

        from concurrent.futures import ThreadPoolExecutor
        # BACKTEST_WORKERS 可调（默认 4 保守值）。已预热的标的走本地 MarketStore 缓存，
        # 上限只受 DeepSeek 并发（2500）与本机线程约束——大规模回填可放开。
        # ⚠️ 未预热的冷标的会走 patched_get_history 的空 DB 兜底 → 真打 yfinance，
        # 高并发下会撞限流：新标的先跑 scripts.backfill_history 预热再放开并发（CR 发现）。
        # 解析容错：坏值(空串/负数/非数字)不该在 macro LLM 已花钱之后才炸整轮。
        try:
            _workers = int(os.environ.get("BACKTEST_WORKERS", "4"))
        except ValueError:
            print("⚠️ BACKTEST_WORKERS 非数字，回落默认 4")
            _workers = 4
        _workers = max(1, min(_workers, 64))
        with ThreadPoolExecutor(max_workers=min(len(pending), _workers)) as ex:
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
    parser.add_argument(
        "--prospective", action="store_true",
        help="前瞻纸面模式（每日舰队，ADR-022 更新节）：只跑【今天】。决策时未来尚不"
             "存在 ⇒ 对任何未来模型都无记忆穿越——唯一不受模型升级影响的干净样本源。"
             "verdict_review 会在 30/90d 后用真实后市回填评分。与 --start/--end/--days"
             "/--holdout 互斥。",
    )
    parser.add_argument(
        "--t2-confidence-arm", action="store_true",
        help="（仅 --prospective）D11 P2 舰队试跑：对照臂跑完后，同一批标的再跑一臂 CIO 用 T2 "
             f"CONFIDENCE 定义的委员会，写 memory/{T2_ARM_SUBDIR}/<今天>/；对照臂不变。",
    )
    parser.add_argument(
        "--sealed-arm", action="store_true",
        help="（仅 --prospective）评审路线 B4 密封投票臂：对照臂跑完后，同一批标的、同一份输入再跑一臂——"
             f"每个标的独立采样 N 次 rounds=1 委员会（--sealed-n，默认 {SEALED_DEFAULT_N}），"
             f"按多数票聚合，写 memory/{SEALED_ARM_SUBDIR}/<今天>/；对照臂不变，live 裁决不变。",
    )
    parser.add_argument(
        "--debate-arm", action="store_true",
        help=f"（仅 --prospective）B4 对照：同一批标的、同一份输入再跑一臂 rounds={DEBATE_ROUNDS} 单次抽样"
             f"（= live 日报的辩论配置），写 memory/{DEBATE_ARM_SUBDIR}/<今天>/；回答\"辩论有没有害\"。",
    )
    parser.add_argument(
        "--macro-soft-arm", action="store_true",
        help="（仅 --prospective）#461：同一批标的、同一份输入再跑一臂，CIO 规则 2 换成\"Macro risk_off 且 STRENGTH<8 "
             f"时不否决、金额按 (10−STRENGTH)/10 打折\"的变体，写 memory/{MACROSOFT_ARM_SUBDIR}/<今天>/；live prompt 不变。",
    )
    parser.add_argument(
        "--sealed-n", type=int, default=SEALED_DEFAULT_N,
        help=f"密封臂每标的抽样次数（默认 {SEALED_DEFAULT_N}，最少 {SEALED_MIN_N}）。"
             "实验开跑后别改：同一目录混不同 N 的行，复盘报告会按 arm 标签分开计数提示。",
    )
    args = parser.parse_args()
    if args.t2_confidence_arm and not args.prospective:
        raise SystemExit("❌ --t2-confidence-arm 只能和 --prospective 一起用（舰队试跑，不碰回填）")
    if args.sealed_arm and not args.prospective:
        raise SystemExit("❌ --sealed-arm 只能和 --prospective 一起用（舰队试跑，不碰回填）")
    for flag, on in (("--debate-arm", args.debate_arm), ("--macro-soft-arm", args.macro_soft_arm)):
        if on and not args.prospective:
            raise SystemExit(f"❌ {flag} 只能和 --prospective 一起用（舰队试跑，不碰回填）")
    if args.sealed_arm and args.sealed_n < SEALED_MIN_N:
        raise SystemExit(f"❌ --sealed-n 至少 {SEALED_MIN_N}（少于 3 次凑不出多数票）")

    # 解析时间范围
    today = datetime.now().date()
    if args.prospective:
        conflicts = [n for n, v in (("--holdout", args.holdout),
                                    ("--allow-lookahead", args.allow_lookahead),
                                    ("--days", args.days is not None),
                                    ("--start", args.start is not None),
                                    ("--end", args.end is not None),
                                    ("--step", args.step != 1)) if v]
        if conflicts:
            raise SystemExit(f"❌ --prospective 与 {'/'.join(conflicts)} 互斥"
                             "（--limit 例外：作成本闸，只跑前 N 个标的）")
        d = today.strftime("%Y-%m-%d")
        asset_symbols = [s.strip() for s in args.assets.split(",") if s.strip()]
        if args.limit:
            asset_symbols = asset_symbols[:args.limit]

        # 新鲜度闸(CR 首条发现)：逐标的检查缓存最新 bar。太旧 = 拿陈旧收盘价出
        # 今天的 verdict，样本会被 verdict_review 系统性错标 → 宁可跳过不产出。
        # 逐标的判定顺带覆盖各市场假日不同步与加密 7×24（周末仍有新 bar）。
        from openinvest.db.market_store import MarketStore
        _store = MarketStore()
        fresh, stale = [], []
        for sym in asset_symbols:
            df = _store.get_history_df(sym, days=5)
            if df is None or df.empty:
                stale.append((sym, "no-data"))
                continue
            last = df.index.max().date()
            if last != today:
                stale.append((sym, f"last={last}"))
            else:
                fresh.append(sym)
        if stale:
            print(f"⏭ 行情过期跳过 {len(stale)} 个：{stale[:8]}{' ...' if len(stale) > 8 else ''}")
            print(f"   修复：uv run python -m scripts.backfill_history {' '.join(s for s, _ in stale[:5])} ...")
        if not fresh:
            print("❌ 没有任何标的行情是新鲜的——前瞻舰队今日不产出"
                  "（先跑 backfill_history 刷新缓存，或检查是否休市）")
            return
        print(f"🛰 PROSPECTIVE 前瞻纸面：{d}（未来尚不存在，任何模型无记忆可穿越）")
        print(f"🔬 {len(fresh)} 资产 × 1 日 = {len(fresh)} 次 committee"
              f"（{len(asset_symbols)} 个中 {len(stale)} 个因行情过期跳过）")
        res = run_one_day(d, fresh)
        ok = sum(1 for v in res.get("verdicts", {}).values() if v and not v.get("error"))
        print(f"\n✅ 前瞻纸面完成：{ok}/{len(fresh)} 成功，写入 memory/.backtest/{d}/")
        failed = ok < len(fresh)
        if args.t2_confidence_arm:
            build_cio_prompt_t2({"symbol": "PREFLIGHT"})  # 锚点失效在花 LLM 钱之前就炸
            with _t2_arm():
                res_t2 = run_one_day(d, fresh, out_subdir=T2_ARM_SUBDIR)
            ok_t2 = sum(1 for v in res_t2.get("verdicts", {}).values() if v and not v.get("error"))
            print(f"\n✅ T2 臂完成：{ok_t2}/{len(fresh)} 成功，写入 memory/{T2_ARM_SUBDIR}/{d}/")
            failed = failed or ok_t2 < len(fresh)
        if args.sealed_arm:
            # 借对照臂这次跑出来的 macro_view（对照臂整天已跑完被断点续跑跳过时没有 → 自己跑一次）
            res_sealed = run_one_day(d, fresh, out_subdir=SEALED_ARM_SUBDIR, votes=args.sealed_n,
                                     macro_view_override=res.get("macro_view"))
            ok_sealed = sum(1 for v in res_sealed.get("verdicts", {}).values() if v and not v.get("error"))
            print(f"\n✅ 密封投票臂完成：{ok_sealed}/{len(fresh)} 成功（每标的 {args.sealed_n} 票），"
                  f"写入 memory/{SEALED_ARM_SUBDIR}/{d}/")
            failed = failed or ok_sealed < len(fresh)
        # 其余试跑臂同样借对照臂的 macro_view；顺序固定 sealed → debate → macro-soft（macro-soft 的锚点失效不会拦住前两臂）
        if args.debate_arm:
            res_d = run_one_day(d, fresh, out_subdir=DEBATE_ARM_SUBDIR, max_rounds_override=DEBATE_ROUNDS,
                                macro_view_override=res.get("macro_view"),
                                arm_health={"arm": f"debate_r{DEBATE_ROUNDS}"})
            ok_d = sum(1 for v in res_d.get("verdicts", {}).values() if v and not v.get("error"))
            print(f"\n✅ debate 臂完成（rounds={DEBATE_ROUNDS}）：{ok_d}/{len(fresh)} 成功，写入 memory/{DEBATE_ARM_SUBDIR}/{d}/")
            failed = failed or ok_d < len(fresh)
        if args.macro_soft_arm:
            try:
                for jm in (False, True):
                    build_cio_prompt_macro_soft({"symbol": "PREFLIGHT"}, json_mode=jm)  # 锚点失效在花 LLM 钱之前就炸
            except RuntimeError as e:
                print(f"\n❌ macro-soft 臂跳过：{e}")
                failed = True
            else:
                with _macro_soft_arm():
                    res_m = run_one_day(d, fresh, out_subdir=MACROSOFT_ARM_SUBDIR,
                                        macro_view_override=res.get("macro_view"),
                                        arm_health={"arm": "macrosoft_r1"})
                ok_m = sum(1 for v in res_m.get("verdicts", {}).values() if v and not v.get("error"))
                print(f"\n✅ macro-soft 臂完成：{ok_m}/{len(fresh)} 成功，写入 memory/{MACROSOFT_ARM_SUBDIR}/{d}/")
                failed = failed or ok_m < len(fresh)
        if failed:
            raise SystemExit(1)   # 让 cron 日志/退出码暴露失败，不静默成功
        return
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
