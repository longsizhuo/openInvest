"""开卷下限验证 — 2024-01~2026-05 每周一次真实委员会，验证 verdict 成功率。

这是一个**确定性 for 循环脚本**（不是 agent / 不是 /goal）。250+ 次跑、8-12 小时、
中途必有 429/超时，所以核心是：断点续跑 + 单点失败不中断 + 进度以落盘行数为准。

────────────────────────────────────────────────────────────────────────────
做什么
────────────────────────────────────────────────────────────────────────────
对 (采样日期 D, asset) ∈ {2024-01-01..2026-05-01 每 7 天} × {GC=F, NDQ.AX}：
  1. 把整条**生产委员会路径**（run_committee_session，与 skill.py run_committee 同款）
     钉到历史日 D —— 决策只看 D 之前的市场数据（行情/技术/macro 都截到 D）。
  2. **开卷**：允许 LLM 看到 D 之后的信息（有意的）。开卷不是脚本喂未来数据，而是
     pre-cutoff 日期落在 DeepSeek 训练数据里 → 模型"记得"后续走势。脚本喂的市场数据
     仍是 as-of-D，否则 forward_return_30d 无意义。所以 open/closed 是 D 相对训练
     cutoff 的自然结果，脚本不切换。
  3. 落盘每条记录（jsonl 一行一条），forward_return_30d / regime 全部来自**与 main 上
     概率表同一个 OHLC 源**（core.regime_probability.compute_regime_return_frame），
     绝不碰旧的 276 条 verdict_review。

────────────────────────────────────────────────────────────────────────────
为保证"历史回测有效 + 零副作用 + 确定性"做的隔离（见 _pin_to_date_and_isolate）
────────────────────────────────────────────────────────────────────────────
钉到 D（只看 D 之前市场数据）:
  • utils.exchange_fee.get_history_data → 全历史读 DB → 截 <= D → tail(730)
    （复刻 scripts/backtest_committee.py 的修复：底层 get_history_data 先 tail(730)
     再 cutoff，对早期决策日只剩末端窗口、MA120/quantile 退化成 unknown。这里先全
     历史取再截，保证 D 之前有足够根算 MA120 / price_quantile_2y。a72678b 只改了
     sweep_runner，没动 get_history_data，所以这个 patch 仍然必要。）
    get_macro_data() 同模块调 get_history_data → 自动一并钉到 D。
隔离副作用 / 未来泄漏:
  • core.committee._persist → no-op：**绝不写 memory/.committee/**（零污染，也不会
    回流进 get_recent_committee_verdicts）。
  • core.committee_runner.load_prior_insights → ""：不读今天的 Dreaming insights。
  • agents.tools query_dreaming_insights / get_recent_committee_verdicts → []：
    LLM 工具调用也拿不到未来 insight / 别的采样点的决议（每点独立）。
  • **概率表 + 买回点参考也钉到 D**：patch db.market_store.MarketStore.get_history_df
    → 截 <= D。这是 look-ahead bug 修复，不是"改概率表逻辑"：
    - 生产 live 跑 D 当天时，概率表本来就只能有 D 之前的数据（那时未来还没发生），
      所以截到 D 才是生产真实；全量不截 D 反而是作弊。
    - 不截会让 D=2024-08 的闭卷段 committee 从概率表后门看到 2024-08 之后实际发生的
      统计 → 闭卷段成功率虚高 → 闭卷段白测（经典 look-ahead bias）。
    - 概率表/买回点的所有 OHLC 读取（build_probability_table_from_ohlc /
      get_reentry_estimate / _ohlc_forward_returns）都经 MarketStore.get_history_df，
      patch 这一处即全覆盖，零生产签名改动。
    注意：forward_return_30d（评分用的"答案"）走的是 build_asset_context 里**全历史**
    的 frame（在隔离 context 之外算），需要 D 之后的实际走势 —— 这是合法的事后评分，
    不是泄漏给决策。两者用途不同：决策输入截到 D，事后评分用全历史。
中性化无法还原的用户上下文（session override，等价 backtest 的中性持仓）:
  • portfolio_summary_override = 中性占位（历史日真实持仓无法还原；也避免
    _build_default_portfolio_summary 拉今天的实时金价 → 未来泄漏 + 非确定）。
  • wealth_view_override = ""、event_brief_override = ""（不注入今天的财富/事件）。

────────────────────────────────────────────────────────────────────────────
跑法
────────────────────────────────────────────────────────────────────────────
  # 看采样计划 + 续跑状态（只读 OHLC，不跑委员会，零成本）
  uv run python -m scripts.full_validation --plan

  # 先跑 5 个点验证落盘/续跑/failed 格式（用单独的 smoke 文件，不污染正式文件）
  uv run python -m scripts.full_validation --limit 5 \
      --results-file /tmp/full_validation_smoke.jsonl

  # 全量后台（断点续跑，可随时 Ctrl-C / 被 kill 后重跑续上）
  uv run python -m scripts.full_validation

  # 全量跑完后出分段统计（开卷/闭卷分两段，绝不合并）
  uv run python -m scripts.full_validation --stats

需要 DEEPSEEK_API_KEY（Direct 路径跑后端 4 角色辩论）。--plan / --stats 不需要。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")  # 让后端 DeepSeek 拿到 DEEPSEEK_API_KEY

# ============================================================================
# 可调常量（顶部单点维护）
# ============================================================================
RESULTS_FILE = "/tmp/full_validation_results.jsonl"
SAMPLE_START = "2024-01-01"
SAMPLE_END = "2026-05-01"
STEP_DAYS = 7
ASSETS = ["GC=F", "NDQ.AX"]
CUTOFF = "2024-06-01"  # is_pre_cutoff = D < CUTOFF（开卷段 / 闭卷段分界）

# 以 live 真实为准：jobs/daily_report.py 生产路径用 max_debate_rounds=4
# （"三路径统一 4 轮，用户已确认"）。测试是给生产系统拍快照，必须跑同样轮数，
# 否则测的是比真实弱的阉割版，成功率没有意义。贵 ~4×、慢、更多 429，但断点续跑
# + 429 退避扛得住。（skill.py CLI 的 --max-rounds 默认 1 只是 CLI 默认，非 live 行为。）
MAX_DEBATE_ROUNDS = 4

FWD_WINDOW = "30d"  # forward return 窗口（compute_regime_return_frame 的列名后缀）
FWD_DAYS = 30  # 给 _flat_band 算 HOLD 波动带用

# 单点失败重试：1 次初跑 + 3 次重试，重试前分别等 5s / 15s / 45s（指数退避）。
RETRY_BACKOFFS_SEC = [5, 15, 45]

# 历史日真实持仓无法还原 → 中性占位（与 scripts/backtest_committee.py 一致的口径）。
NEUTRAL_PORTFOLIO_SUMMARY = (
    "# 历史回测 — 中性持仓上下文\n"
    "该采样点为历史日期，用户当时的真实持仓 / 现金无法精确还原。\n"
    "假设持仓中性（无极端集中度），Risk Officer 请聚焦技术 + 宏观信号，"
    "不要据此做集中度减仓判断。"
)

# 复刻 core.committee.VERDICT_RE，用于从 CIO 原文还原 Sanity 改写前的 verdict_raw。
# 本脚本另在 _pin_to_date_and_isolate 里 import core.committee 以 patch _persist（防污染），
# 这条 scripts.full_validation -> core.committee 已加进 pyproject.toml 的 import-linter
# 例外（同 backtest_committee，研究脚本不共享 production service layer）。
_VERDICT_RAW_RE = re.compile(r"VERDICT:\s*(BUY|ACCUMULATE|HOLD|TRIM|SELL)", re.I)

log = logging.getLogger("full_validation")

# ---- 只读 import：OHLC 源（regime + forward return）、hit 定义、生产委员会入口 ----
from core.regime_probability import compute_regime_return_frame  # noqa: E402
from db.market_store import MarketStore  # noqa: E402
from jobs.verdict_review import (  # noqa: E402  口径一致的 hit 定义，直接复用
    EXPECTED_DIRECTION,
    _atr_pct_cached,
    _flat_band,
    _is_hit,
)


# ============================================================================
# 历史钉日 + 副作用隔离
# ============================================================================
@contextmanager
def _pin_to_date_and_isolate(decision_date: str):
    """把生产委员会路径钉到历史日 D + 隔离所有副作用/未来泄漏。详见模块 docstring。"""
    import pandas as pd

    import agents.tools as tools
    import core.committee as cm
    import core.committee_runner as cr
    import db.market_store as ms
    import utils.exchange_fee as ef

    cutoff_ts = pd.to_datetime(decision_date)
    real_get_history = ef.get_history_data
    real_get_history_df = ms.MarketStore.get_history_df

    def patched_get_history(symbol: str, period: str = "2y", as_of_date=None):
        # 行情/技术/macro：先全历史，再按 D 截，最后 tail(730) —— 保证早期决策日
        # 也有 ≥250 根算 MA（底层 get_history_data 先 tail(730) 再 cutoff 会退化）。
        df = ef._STORE.get_history_df(symbol, days=100000)
        if df is None or df.empty:
            # DB 没这个 symbol（如 ^VIX/^TNX 可能没入库）→ 退回原实现，带 as_of_date 截到 D
            return real_get_history(symbol, period, as_of_date=decision_date)
        df = df[df.index <= cutoff_ts]
        return df.tail(730)

    def patched_get_history_df(self, *a, **k):
        # 概率表 + 买回点参考的 OHLC 读取口：截 <= D，修 look-ahead。
        df = real_get_history_df(self, *a, **k)
        if df is None or getattr(df, "empty", True):
            return df
        return df[df.index <= cutoff_ts]

    def _noop(*_a, **_k):  # 替 _persist：绝不写盘
        return None

    def _empty_str(*_a, **_k):
        return ""

    def _empty_list(*_a, **_k):
        return []

    with ExitStack() as stack:
        # 1. 行情/技术/macro 钉到 D
        stack.enter_context(patch.object(ef, "get_history_data", patched_get_history))
        # 1b. 概率表 + 买回点参考钉到 D（look-ahead 修复：闭卷段不从概率表后门看未来）
        stack.enter_context(patch.object(ms.MarketStore, "get_history_df", patched_get_history_df))
        # 2. 绝不持久化到 memory/.committee/
        stack.enter_context(patch.object(cm, "_persist", _noop))
        # 3. 不读今天的 Dreaming insights（service layer 注入路径）
        stack.enter_context(patch.object(cr, "load_prior_insights", _empty_str))
        # 4. LLM 工具调用也拿不到未来 insight / 别的采样点决议
        stack.enter_context(patch.object(tools, "_impl_query_dreaming_insights", _empty_list))
        stack.enter_context(patch.dict(tools.TOOL_IMPL, {"query_dreaming_insights": _empty_list}))
        stack.enter_context(patch.object(tools, "_impl_get_recent_committee_verdicts", _empty_list))
        stack.enter_context(patch.dict(tools.TOOL_IMPL, {"get_recent_committee_verdicts": _empty_list}))
        yield


# ============================================================================
# OHLC 源：regime_at_decision + forward_return_30d（与 main 概率表同源）
# ============================================================================
def build_asset_context(assets: List[str]) -> Dict[str, Dict[str, Any]]:
    """每个资产预算一次：OHLC frame（regime + fwd_30d 全序列）+ HOLD 波动带。

    frame 来自 compute_regime_return_frame —— 与 build_probability_table_from_ohlc
    走的同一个函数 / 同一个 MarketStore OHLC，保证 regime 分桶和 forward return
    与概率表完全一致。flat_band 用 verdict_review 同款 _flat_band(_atr_pct, 30)，
    在任何 as-of-D patch **之外**预算（= 当前波动，跟 verdict_review 口径一致）。
    """
    ctx: Dict[str, Dict[str, Any]] = {}
    store = MarketStore()
    for sym in assets:
        s = sym.upper()
        df = store.get_history_df(s, days=100000)
        frame = compute_regime_return_frame(df, s, windows=(FWD_WINDOW,))
        try:
            flat = _flat_band(_atr_pct_cached(sym), FWD_DAYS)
        except Exception as e:  # noqa: BLE001  一次 yfinance 抖动不该卡死整跑
            flat = 0.03  # _is_hit 的默认回退带；仅影响 HOLD 命中判定
            log.warning("flat_band(%s) 计算失败，退回默认 0.03（仅影响 HOLD 命中带）: %s", sym, e)
        ctx[sym] = {"frame": frame, "flat": flat}
        log.info(
            "asset_context %s: frame rows=%d, flat_band=%.4f",
            sym, 0 if frame is None or frame.empty else len(frame), flat,
        )
    return ctx


def lookup_regime_and_fwd(frame, D: str) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """取 D（含）之前最后一个交易日的 regime 和 fwd_30d。

    采样日 D 可能是周末/休市 → 用 <= D 的最后一个交易日（与 as-of-D 委员会看到的
    最后一根 K 线对齐）。返回 (regime, forward_return_30d 小数, eff_date 实际交易日)。
    forward_return 在尾部（D+30 超出数据末端）为 None。
    """
    import pandas as pd

    if frame is None or frame.empty:
        return None, None, None
    sub = frame.loc[: pd.to_datetime(D)]
    if sub.empty:
        return None, None, None
    row = sub.iloc[-1]
    eff = str(sub.index[-1].date())
    fwd = row.get(f"fwd_{FWD_WINDOW}")
    fwd = None if (fwd is None or pd.isna(fwd)) else float(fwd)
    regime = row.get("regime")
    return (None if regime is None else str(regime)), fwd, eff


# ============================================================================
# 跑一次真实委员会（生产路径）
# ============================================================================
def run_committee_at_date(symbol: str, D: str) -> Dict[str, Any]:
    """钉到 D 跑 run_committee_session（与 skill.py 同款），返回 parse_cio_memo dict + 元信息。"""
    from core.committee_runner import run_committee_session

    with _pin_to_date_and_isolate(D):
        session = run_committee_session(
            symbols=[symbol],
            max_debate_rounds=MAX_DEBATE_ROUNDS,
            event_brief_override="",  # 不注入今天的事件
            wealth_view_override="",  # 不注入今天的财富上下文
            portfolio_summary_override=NEUTRAL_PORTFOLIO_SUMMARY,
            max_workers=1,  # 单资产串行，确定性，无 ThreadPool 嵌套
            on_asset_error="raise",  # 单资产失败 → 抛 → 上层重试逻辑接住
        )
    res = session["asset_committees"].get(symbol)
    if not isinstance(res, dict) or "error" in (res or {}):
        raise RuntimeError((res or {}).get("error", "no result for symbol"))
    return res


def extract_verdicts(verdict_dict: Dict[str, Any]) -> Tuple[str, str]:
    """(final 改写后, raw 改写前)。

    final = parse_cio_memo 的 verdict（Sanity 0-5 后处理后的最终值）。
    raw   = _original_verdict（Sanity 1/4/5 改写时记录的原值）；没有就用 VERDICT_RE
            重解析 CIO 原文（覆盖 Sanity 3 等不写 _original_verdict 的情况）；再没有
            就等于 final（说明没发生改写）。
    """
    final = str(verdict_dict.get("verdict", "UNCLEAR"))
    raw = verdict_dict.get("_original_verdict")
    if not raw:
        m = _VERDICT_RAW_RE.search(verdict_dict.get("raw", "") or "")
        raw = m.group(1).upper() if m else final
    return final, str(raw)


# ============================================================================
# 落盘 + 断点续跑
# ============================================================================
def load_done_keys(results_path: str) -> set:
    """读已有 jsonl，返回已处理的 (date, asset) 集合（ok 和 failed 都算"已处理"）。

    续跑策略：append-only，进度以行数为准。已落盘的点（无论 ok/failed）一律跳过，
    避免对持续失败的点无限重试。**要重跑 failed 点：先 grep 掉它们的行再重启。**
    """
    done: set = set()
    if not os.path.exists(results_path):
        return done
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "date" in rec and "asset" in rec:
                done.add((rec["date"], rec["asset"]))
    return done


def append_record(results_path: str, rec: Dict[str, Any]) -> None:
    """原子 append 一行 + flush + fsync，保证被 kill 时已落盘的行不丢。"""
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def sample_dates(start: str, end: str, step_days: int) -> List[str]:
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    out: List[str] = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=step_days)
    return out


def canonical_points(
    assets: List[str], start: str, end: str, step_days: int,
    only_from: Optional[str], only_to: Optional[str],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """统一采样网格（永远从 start 按 step_days 生成）→ 可选按 [only_from, only_to]
    切出不相交的日期片（给并行分批用，不改网格原点，保证与单跑同样的采样日）。"""
    dates = sample_dates(start, end, step_days)
    pts = [(d, a) for d in dates for a in assets]
    if only_from:
        pts = [(d, a) for (d, a) in pts if d >= only_from]
    if only_to:
        pts = [(d, a) for (d, a) in pts if d <= only_to]
    return dates, pts


# ============================================================================
# 主跑循环
# ============================================================================
def run(
    assets: List[str],
    start: str,
    end: str,
    step_days: int,
    results_path: str,
    limit: Optional[int],
    only_from: Optional[str] = None,
    only_to: Optional[str] = None,
) -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        log.error("缺 DEEPSEEK_API_KEY —— Direct 路径跑不了 4 角色辩论。退出。")
        sys.exit(2)

    # 采样点顺序：日期升序 × 资产固定顺序 → 完全确定
    dates, points = canonical_points(assets, start, end, step_days, only_from, only_to)
    done = load_done_keys(results_path)
    todo = [(d, a) for (d, a) in points if (d, a) not in done]
    if limit is not None:
        todo = todo[:limit]

    log.info(
        "采样点 %d 个（%d 日期 × %d 资产）；已完成 %d；本次待跑 %d；写入 %s",
        len(points), len(dates), len(assets), len(done), len(todo), results_path,
    )

    if not todo:
        log.info("无待跑采样点（已全部完成）—— 续跑秒退。")
        return

    ctx = build_asset_context(assets)

    for i, (D, sym) in enumerate(todo, 1):
        regime, fwd, eff = lookup_regime_and_fwd(ctx[sym]["frame"], D)
        is_pre = D < CUTOFF
        base: Dict[str, Any] = {
            "date": D,
            "asset": sym,
            "is_pre_cutoff": is_pre,
            "regime_at_decision": regime,
            "forward_return_30d": fwd,
            "eff_date": eff,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }

        verdict_dict, attempts, err = _attempt_with_retry(
            lambda: run_committee_at_date(sym, D)
        )

        if verdict_dict is None:
            rec = {
                **base,
                "status": "failed",
                "error": err,
                "attempts": attempts,
                "verdict": None,
                "verdict_raw": None,
                "confidence": None,
                "committee_regime": None,
                "hit": None,
            }
        else:
            final, raw = extract_verdicts(verdict_dict["verdict"])
            conf = float(verdict_dict["verdict"].get("confidence", 0.0) or 0.0)
            # committee 自己看到的 regime（regime_brief label）——交叉核对 OHLC frame regime
            committee_regime = getattr(
                verdict_dict.get("regime_probability"), "regime", None
            )
            hit = _is_hit(final, fwd, ctx[sym]["flat"]) if fwd is not None else None
            rec = {
                **base,
                "status": "ok",
                "verdict": final,
                "verdict_raw": raw,
                "confidence": conf,
                "committee_regime": committee_regime,
                "hit": hit,
                "attempts": attempts,
            }

        append_record(results_path, rec)
        total_lines = len(load_done_keys(results_path))
        log.info(
            "[%d/%d] %s %s → %s%s | regime=%s fwd=%s hit=%s | 落盘行数=%d",
            i, len(todo), D, sym, rec["status"],
            "" if rec["status"] == "ok" else f"({err})",
            regime,
            "None" if fwd is None else f"{fwd:+.4f}",
            rec["hit"], total_lines,
        )


def _attempt_with_retry(fn) -> Tuple[Optional[Any], int, Optional[str]]:
    """1 次初跑 + len(RETRY_BACKOFFS_SEC) 次重试。返回 (结果或None, 尝试次数, 末次错误)。"""
    waits = [0] + RETRY_BACKOFFS_SEC  # attempt1 不等，之后等 5/15/45
    last_err: Optional[str] = None
    for idx, wait in enumerate(waits, 1):
        if wait:
            log.warning("  第 %d 次尝试前等待 %ds…", idx, wait)
            time.sleep(wait)
        try:
            return fn(), idx, None
        except Exception as e:  # noqa: BLE001  任何异常都重试，仍失败记 failed
            last_err = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("  尝试 %d 失败：%s", idx, last_err)
    return None, len(waits), last_err


# ============================================================================
# --plan：只看采样计划 + 续跑状态（零成本，不跑委员会）
# ============================================================================
def cmd_plan(assets, start, end, step_days, results_path, only_from=None, only_to=None) -> None:
    dates, points = canonical_points(assets, start, end, step_days, only_from, only_to)
    done = load_done_keys(results_path)
    todo = [(d, a) for (d, a) in points if (d, a) not in done]
    pre = sum(1 for d, _ in points if d < CUTOFF)
    post = len(points) - pre
    print(f"采样区间       : {start} → {end}，每 {step_days} 天"
          + (f"（本批切片 {only_from or '…'} → {only_to or '…'}）" if (only_from or only_to) else ""))
    print(f"日期点(全网格) : {len(dates)}")
    print(f"资产           : {assets}")
    print(f"总采样点       : {len(points)}（开卷段<{CUTOFF}: {pre}；闭卷段≥{CUTOFF}: {post}）")
    print(f"结果文件       : {results_path}")
    print(f"已落盘         : {len(done)}")
    print(f"待跑           : {len(todo)}")
    print("\n各资产 OHLC frame regime 分桶（采样区间内，预览将出现的桶）：")
    import pandas as pd
    ctx = build_asset_context(assets)
    for sym in assets:
        f = ctx[sym]["frame"]
        if f is None or f.empty:
            print(f"  {sym}: 无 frame"); continue
        sub = f.loc[pd.to_datetime(start): pd.to_datetime(end)]
        print(f"  {sym}: {sub['regime'].value_counts().to_dict()}  flat_band={ctx[sym]['flat']:.4f}")


# ============================================================================
# --stats：开卷/闭卷分两段统计（绝不合并），含已知缺陷注解
# ============================================================================
def _rate(records: List[Dict[str, Any]]) -> str:
    """对一组记录算 hit 率（只算 hit 非 null 的）。返回 'x/y = z%' 或 'n/a'。"""
    scored = [r for r in records if r.get("hit") is not None]
    if not scored:
        return "n/a（无可评分样本）"
    h = sum(1 for r in scored if r["hit"])
    return f"{h}/{len(scored)} = {100.0 * h / len(scored):.1f}%"


def _breakdown(records: List[Dict[str, Any]], key: str) -> None:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        buckets.setdefault(str(r.get(key)), []).append(r)
    for name in sorted(buckets):
        recs = buckets[name]
        flag = "  ⚠️低样本(n<30)" if len([x for x in recs if x.get("hit") is not None]) < 30 else ""
        print(f"    {name:<14} hit={_rate(recs)}{flag}")


def cmd_stats(results_path: str) -> None:
    if not os.path.exists(results_path):
        print(f"结果文件不存在：{results_path}")
        return
    recs = []
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    ok = [r for r in recs if r.get("status") == "ok"]
    failed = [r for r in recs if r.get("status") == "failed"]
    null_fwd = [r for r in ok if r.get("forward_return_30d") is None]

    print("=" * 72)
    print("开卷下限验证 — 分段统计（开卷/闭卷绝不合并）")
    print("=" * 72)
    print(f"总落盘行 {len(recs)}：ok {len(ok)}，failed {len(failed)}，"
          f"ok 中 forward_return 缺失（尾部 D+30 超数据）{len(null_fwd)}")
    print()

    segments = [
        (f"开卷段  D < {CUTOFF}  —— 下限测试（模型见过未来，<50% = 系统有根本 bug）",
         [r for r in ok if r.get("is_pre_cutoff")]),
        (f"闭卷段  D ≥ {CUTOFF}  —— 真实委员会能力（50% ≠ 系统坏，= 无预测力但系统正常）",
         [r for r in ok if not r.get("is_pre_cutoff")]),
    ]
    for title, seg in segments:
        print("-" * 72)
        print(title)
        print("-" * 72)
        if not seg:
            print("  （无样本）\n")
            continue
        print(f"  整体    hit={_rate(seg)}  (ok 样本 {len(seg)})")
        print("  按 regime 拆：")
        _breakdown(seg, "regime_at_decision")
        print("  按 verdict 类型拆（Sanity 改写后最终 verdict）：")
        _breakdown(seg, "verdict")
        print()

    print("-" * 72)
    print("已知缺陷注解（来自 sweep 发现；本次只读验证，不修分类逻辑）")
    print("-" * 72)
    print("  • crash 桶：当前阈值下几十年 0 次触发，本结果里该桶为空——不是数据问题，"
          "是系统已知局限（classify_regime crash 触发器在历史上从不命中）。")
    print("  • uptrend 桶：MA 滞后会把急跌期误标成 uptrend/range_bound（如 2020-02~03 "
          "COVID 暴跌被标 uptrend）。本采样区间 2024-2026 不含 2020，但同类 MA 滞后误分类"
          "可能仍混入 uptrend 桶——读 uptrend 成功率时知悉此口径，不为此改分类。")
    print("  • recovery 桶：采样区间内极少/为空，低样本，勿过度解读。")
    print("  • 闭卷段早期采样点（2024-07/08）：概率表已截到 D（修 look-ahead），对稀有 regime"
          "（downtrend/recovery）D 之前样本更少、可能 low_confidence。GC=F/NDQ.AX 有 2000/2015"
          "起的长历史，常见 regime 仍样本充足；但读闭卷早期 + 稀有 regime 时知悉"
          "\"概率表参考较弱\"，不是 bug。")
    print()
    if failed:
        print(f"  failed 点 {len(failed)} 个（已重试 {1 + len(RETRY_BACKOFFS_SEC)} 次仍失败）：")
        for r in failed[:20]:
            print(f"    {r.get('date')} {r.get('asset')}: {r.get('error')}")
        if len(failed) > 20:
            print(f"    …还有 {len(failed) - 20} 个")


# ============================================================================
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="开卷下限验证（确定性脚本，断点续跑）")
    p.add_argument("--plan", action="store_true", help="只看采样计划 + 续跑状态（不跑委员会）")
    p.add_argument("--stats", action="store_true", help="读结果文件出分段统计（开卷/闭卷分两段）")
    p.add_argument("--results-file", default=RESULTS_FILE, help=f"jsonl 结果文件（默认 {RESULTS_FILE}）")
    p.add_argument("--assets", default=",".join(ASSETS), help="逗号分隔，默认 GC=F,NDQ.AX")
    p.add_argument("--start", default=SAMPLE_START)
    p.add_argument("--end", default=SAMPLE_END)
    p.add_argument("--step-days", type=int, default=STEP_DAYS)
    p.add_argument("--limit", type=int, help="只跑前 N 个待跑点（5 点验证用）")
    p.add_argument("--only-from", help="只跑 date >= 此日的采样点（并行分批用，切不相交片）")
    p.add_argument("--only-to", help="只跑 date <= 此日的采样点（并行分批用，切不相交片）")
    args = p.parse_args()

    assets = [s.strip() for s in args.assets.split(",") if s.strip()]

    if args.stats:
        cmd_stats(args.results_file)
        return
    if args.plan:
        cmd_plan(assets, args.start, args.end, args.step_days, args.results_file,
                 args.only_from, args.only_to)
        return
    run(assets, args.start, args.end, args.step_days, args.results_file, args.limit,
        args.only_from, args.only_to)


if __name__ == "__main__":
    main()
