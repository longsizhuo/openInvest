"""analysis_cmds —— 只读分析类 skill 子命令

逐字搬运自 scripts/skill.py：
- cmd_status / cmd_strategy / cmd_history / cmd_what_if：薄封装 services/skill_views.py
  的计算体（与对应 /api/skill/* 端点共享，防 local/remote 漂移）。
- cmd_correlate：跨资产关联分析（无金额建议），pandas/yfinance/openai 延迟 import 保留在函数体。
- cmd_live_prices：一次拉齐 ^VIX / ^TNX / USDCNY / AUDCNY / NDQ / GC=F。
- cmd_event_check：事件层端到端 CLI 入口（db/services/jobs 延迟 import）。

本模块定义自有 ROOT 仅为承接 tests/test_onboarding_smoke.py 对 cmd_status 的
ROOT patch（cmd_status 实际不读 ROOT，此 patch 历史上是 no-op，定义 ROOT 是为了
patch 时不抛 AttributeError）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from scripts.skill_cmds._helpers import _print_json, _safe_close

# 本模块需自有 ROOT 以承接 test_onboarding 对 cmd_status 的 ROOT patch（no-op 但需符号存在）
ROOT = Path(__file__).resolve().parents[2]

__all__ = [
    "cmd_status",
    "cmd_strategy",
    "cmd_history",
    "cmd_what_if",
    "cmd_correlate",
    "cmd_live_prices",
    "cmd_discipline",
    "cmd_event_check",
]


# ---------- status ----------

def cmd_status(_: argparse.Namespace) -> None:
    """v2 通用化：从 cash dict + holdings list 读，对外保持原 JSON 结构兼容老 agent

    计算体在 services/skill_views.py（与 /api/skill/status 共享，防 local/remote 漂移）
    """
    from services.skill_views import build_status_view
    _print_json(build_status_view())


# ---------- strategy ----------

def cmd_strategy(_: argparse.Namespace) -> None:
    from services.skill_views import build_strategy_view
    _print_json(build_strategy_view())


# ---------- history ----------

def cmd_history(args: argparse.Namespace) -> None:
    from services.skill_views import build_history_view
    _print_json(build_history_view(args.n))


# ---------- what_if ----------

def cmd_what_if(args: argparse.Namespace) -> None:
    """通用化情景模拟：支持任意 yfinance symbol 涨跌 X% 后总市值变化

    新接口 (P1-F 修复)：
        run.sh what_if --symbol 510300.SS --pct -5      # 任意持仓涨跌
        run.sh what_if --symbol BTC-USD --price 65000   # 任意持仓变绝对价
    旧接口（兼容）：
        run.sh what_if --gold-pct -5
        run.sh what_if --ndq-pct -3
        run.sh what_if --audcny 5.0

    计算体在 services/skill_views.py（与 /api/skill/what_if 共享）
    """
    from services.skill_views import build_what_if_view
    _print_json(build_what_if_view(
        symbol=args.symbol, pct=args.pct, price=args.price,
        gold_price=args.gold_price, gold_pct=args.gold_pct,
        ndq_price=args.ndq_price, ndq_pct=args.ndq_pct,
        audcny=args.audcny,
    ))


# ---------- correlate（通用市场分析，无金额建议） ----------

def cmd_correlate(args: argparse.Namespace) -> None:
    """跨资产关联分析 — 用户问"X 和 Y 有啥关联"、"X 趋势像 Y 吗"、"板块对比"

    跟 run_committee 不同:
    - 不需要 symbol 在 strategy.target_assets 里（任何 yfinance symbol 都行）
    - 不需要用户已持有
    - **不输出 BUY/SELL/alloc**（纯分析，无金额建议）
    - 输出: 相关系数矩阵 + sector/industry + 跟 macro 的关联

    示例:
        skill correlate --symbols NDQ.AX,0700.HK,510300.SS
        skill correlate --symbols AAPL,GOOGL,MSFT --period 1y --with-llm
    """
    import pandas as pd
    import yfinance as yf

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if len(symbols) < 2:
        _print_json({"status": "error", "error": "至少 2 个 symbol 才能算相关性"})
        sys.exit(1)
    period = args.period or "6mo"

    # 1. 拉每个 symbol 历史价（直接走 yfinance 拿真 period 长度数据）
    closes: Dict[str, pd.Series] = {}
    for sym in symbols:
        df = yf.Ticker(sym).history(period=period)
        if df is None or df.empty:
            _print_json({"status": "error", "error": f"{sym} 拿不到历史数据"})
            sys.exit(1)
        closes[sym] = df["Close"]

    # 2. 算 pairwise pearson correlation（基于日收益率，不是绝对价）
    # 跨市场（港股 / A 股 / 美股）交易日对齐后会很多 NaN，所以归一化到日期（去掉时区）
    # + 用 pd.DataFrame.corr() 自动按 pairwise 可用日期算（不要求所有 symbol 同时有值）
    closes_normalized = {}
    for sym, s in closes.items():
        s2 = s.copy()
        # 去掉时区（不同市场 tzinfo 不同）+ 归一化到日期
        s2.index = pd.to_datetime(s2.index).tz_localize(None).normalize()
        closes_normalized[sym] = s2.pct_change()

    returns_df = pd.DataFrame(closes_normalized)
    # corr() 自动按 pairwise non-NaN 算，min_periods 防小样本噪音
    n_per_sym = {sym: int(returns_df[sym].notna().sum()) for sym in symbols}
    min_pairwise = min(n_per_sym.values())
    if min_pairwise < 20:
        _print_json({
            "status": "error",
            "error": f"最少 symbol 只有 {min_pairwise} 天数据，样本太少",
            "per_symbol_days": n_per_sym,
        })
        sys.exit(1)
    corr_matrix = returns_df.corr(min_periods=20).round(3)

    # 3. 拉 sector / industry（yfinance .info）
    import yfinance as yf
    sectors: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info
            sectors[sym] = {
                "sector": info.get("sector") or info.get("quoteType") or "—",
                "industry": info.get("industry") or "—",
                "name": info.get("longName") or info.get("shortName") or sym,
            }
        except Exception:
            sectors[sym] = {"sector": "—", "industry": "—", "name": sym}

    # 4. 跟 macro 因子的 correlation（VIX, TNX, USDCNY）
    macro_corr: Dict[str, Dict[str, float]] = {}
    for macro_sym, label in [("^VIX", "vix"), ("^TNX", "tnx"), ("USDCNY=X", "usdcny")]:
        try:
            macro_df = yf.Ticker(macro_sym).history(period=period)
            if macro_df is None or macro_df.empty:
                continue
            macro_returns = macro_df["Close"].copy()
            macro_returns.index = pd.to_datetime(macro_returns.index).tz_localize(None).normalize()
            macro_returns = macro_returns.pct_change()
            for sym in symbols:
                sym_returns = returns_df[sym].dropna()
                aligned = pd.concat([sym_returns, macro_returns], axis=1, join="inner").dropna()
                if len(aligned) < 20:
                    continue
                c = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
                macro_corr.setdefault(sym, {})[label] = round(c, 3)
        except Exception:
            continue

    # 5. 找最强 pairwise 相关 + 共同 macro 因子
    pairs: List[Tuple[str, str, float]] = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            pairs.append((a, b, float(corr_matrix.loc[a, b])))
    pairs.sort(key=lambda x: -abs(x[2]))

    out: Dict[str, Any] = {
        "status": "ok",
        "symbols": symbols,
        "period": period,
        "n_trading_days": len(returns_df),
        "correlation_matrix": {a: {b: float(corr_matrix.loc[a, b]) for b in symbols} for a in symbols},
        "strongest_pair": {
            "a": pairs[0][0], "b": pairs[0][1], "correlation": pairs[0][2],
        } if pairs else None,
        "sector_industry": sectors,
        "macro_correlation": macro_corr,
        "interpretation_hint": (
            "correlation > 0.7: 强正相关（同涨同跌）；0.3~0.7 中等；< 0.3 弱相关。"
            "macro_correlation 正 = 跟该 macro 因子同向。**无金额建议**——用户没持有，"
            "纯分析市场关系。"
        ),
    }

    # 6. 可选 LLM 给一句话总结
    if args.with_llm:
        from utils.llm import get_llm_config_safe, needs_thinking_disabled
        api_key, base_url, model_name, _provider = get_llm_config_safe()
        if not api_key:
            out["llm_summary"] = "(--with-llm 需要 LLM_API_KEY 或 DEEPSEEK_API_KEY)"
        else:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url=base_url)
                ctx = json.dumps({
                    "symbols": symbols, "corr": out["correlation_matrix"],
                    "sectors": sectors, "macro_corr": macro_corr,
                }, ensure_ascii=False)
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": (
                            "你是金融关联分析师。给定多个资产的相关性矩阵 + sector + macro 关联，"
                            "用 ≤100 字中文说明它们的关系。"
                            "**禁止给买卖建议** —— 用户只问关联，没问该不该交易。"
                            "重点：共同驱动因子是什么？什么时候联动？"
                        )},
                        {"role": "user", "content": ctx},
                    ],
                    temperature=0.2,
                    timeout=30,
                    # DeepSeek v4 需要 disable thinking；千问/智谱/OpenAI 不需要
                    extra_body={"thinking": {"type": "disabled"}} if needs_thinking_disabled(model_name) else None,
                )
                out["llm_summary"] = resp.choices[0].message.content
            except Exception as e:
                out["llm_summary"] = f"(LLM 失败: {e})"

    _print_json(out)


# ---------- live_prices ----------

def cmd_live_prices(_: argparse.Namespace) -> None:
    from utils.gold_price import get_gold_snapshot
    snap = get_gold_snapshot(offset_pct=0.0)
    out = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "GC_F_usd_per_oz": snap.gold_usd_per_oz if snap else None,
        "gold_cny_per_gram_spot": round(snap.spot_cny_per_gram, 2) if snap else None,
        "USDCNY": snap.usdcny_rate if snap else None,
        "AUDCNY": _safe_close("AUDCNY=X"),
        "NDQ_AX": _safe_close("NDQ.AX"),
        "VIX": _safe_close("^VIX"),
        "TNX": _safe_close("^TNX"),
    }
    _print_json(out)


def cmd_discipline(_: argparse.Namespace) -> None:
    """委员会纪律台账(只读,零 LLM):默认不作为率(HOLD 占比)+ 拦截冲动操作次数 + 反事实省/费钱。
    对齐 ADR-023——委员会可证价值是纪律/透明,不是 alpha。等价 GET /api/discipline。"""
    from services.discipline import discipline_summary, render_discipline_md
    s = discipline_summary()
    _print_json({"summary": s, "markdown": render_discipline_md(s)})


def cmd_event_check(args: argparse.Namespace) -> None:
    """CLI 入口：事件层端到端。

    - 默认 dry-run：拉源 + 归一化 + 入库，不发邮件不触委员会
    - --live：发邮件 + 触委员会
    - --recall SYM：测 RAG 召回（不动新闻）
    """
    import json as _json

    if args.recall:
        from db.event_store import EventStore
        from services.embeddings import DEFAULT_DIM, embed_text
        store = EventStore(embedding_dim=DEFAULT_DIM)
        q_embed = embed_text(args.recall) if store.vec_loaded else None
        events = store.recall(args.recall, query_embedding=q_embed)
        print(_json.dumps(events, ensure_ascii=False, indent=2, default=str))
        return

    from jobs.event_watch import run as event_watch_run
    out = event_watch_run(dry_run=not args.live)
    print(_json.dumps(out, ensure_ascii=False, indent=2, default=str))
