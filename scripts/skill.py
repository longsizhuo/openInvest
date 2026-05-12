"""统一 Skill 入口 - 复用 invest 项目本体的 agents/core 模块

设计要点：
- 不再复制 invest 主流程逻辑，所有 prompt / debate 编排都走项目里现有的代码
  (agents.bull, agents.bear, agents.judge, core.debate, core.memory_store)
- Skill 模式下"答辩"的 LLM 不是 DeepSeek，而是 Claude 自己
  → prepare_debate 吐出 prompt 给 Claude 看
  → Claude 在主对话里依次扮演 bull/bear/judge
  → save_debate 把 Claude 的 transcript 落地到 memory/.debate/
- 所有子命令都输出 JSON 或 markdown，给 Claude 读

子命令：
  status                持仓 + 实时价 + 浮盈
  strategy              target_assets + Dreaming insights
  history [-n N]        近期交易 + 近期辩论
  what_if [...]         P&L 情景模拟
  live_prices           ^VIX, ^TNX, USDCNY, AUDCNY, NDQ, GC=F 一次拉齐
  prepare_debate SYM    输出辩论 brief（含项目原生 bull/bear/judge prompt）
  save_debate SYM       把 stdin 上来的 transcript 落到 memory/.debate/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# 让 cmd_doctor 能看到 .env 里的 DEEPSEEK_API_KEY 等（_safe_close 等模块里也会
# 自己 load_dotenv，但 doctor 不依赖 utils 所以这里显式加一道）
try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # dotenv 尚未装时（极少见）跳过

from core.memory_store import MemoryStore  # noqa: E402


def _safe_close(symbol: str) -> float:
    from utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def _print_json(obj: Any) -> None:
    """直接写到原始 stdout，避免被 utils/* 的 print noise 污染"""
    real_stdout = getattr(sys, "__stdout__", sys.stdout)
    real_stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    real_stdout.write("\n")
    real_stdout.flush()


# ---------- status ----------

def cmd_status(_: argparse.Namespace) -> None:
    """v2 通用化：从 cash dict + holdings list 读，对外保持原 JSON 结构兼容老 agent"""
    from utils.gold_price import get_gold_snapshot
    from core.portfolio_manager import PortfolioManager
    pm = PortfolioManager()

    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    ndq_h = pm.holdings.find("NDQ.AX")
    gold_h = pm.holdings.find("GC=F")
    ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    gold_grams = float(gold_h.get("units", 0) or 0) if gold_h else 0.0
    gold_avg = float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0

    ndq_price = _safe_close("NDQ.AX")
    audcny = _safe_close("AUDCNY=X")
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_now = snap.spot_cny_per_gram if snap else 0.0

    out = {
        "user": {
            "name": pm.user.get("display_name") if pm.user else "unknown",
            "risk_tolerance": pm.user.get("risk_tolerance") if pm.user else None,
        },
        "cash": {
            "cny": round(cash_cny, 2),
            "aud": round(aud_cash, 2),
            "aud_in_cny": round(aud_cash * audcny, 2),
            # v2 通用：列出所有币种（其他 agent 想读非 CNY/AUD 时方便）
            "all_currencies": pm.cash,
        },
        "ndq": {
            "shares": ndq_shares,
            "price_aud": round(ndq_price, 2),
            "value_cny": round(ndq_shares * ndq_price * audcny, 2),
        },
        "gold": {
            "grams": gold_grams,
            "avg_cost_cny_per_gram": gold_avg,
            "now_cny_per_gram": round(gold_now, 2),
            "value_cny": round(gold_now * gold_grams, 2),
            "pnl_cny": round((gold_now - gold_avg) * gold_grams, 2) if gold_avg else 0,
            "pnl_pct": round(((gold_now / gold_avg) - 1) * 100, 2) if gold_avg > 0 else 0,
        },
        # v2 新增：完整 holdings 数组（其他 yfinance symbol 也能被 agent 看到）
        "all_holdings": [
            {k: h[k] for k in (
                "symbol", "kind", "units", "unit_label", "avg_cost",
                "cost_currency", "channel", "display_name", "is_tracking_only",
            ) if k in h}
            for h in pm.holdings
        ],
        "total_assets_cny": round(
            cash_cny + aud_cash * audcny
            + ndq_shares * ndq_price * audcny
            + gold_grams * gold_now, 2),
        "fx": {"audcny": round(audcny, 4)},
        "live_prices": {
            "gold_usd_per_oz": snap.gold_usd_per_oz if snap else None,
            "usdcny": snap.usdcny_rate if snap else None,
        },
    }
    _print_json(out)


# ---------- strategy ----------

def cmd_strategy(_: argparse.Namespace) -> None:
    store = MemoryStore()
    strat = store.read("strategy")
    insights_dir = store.root / "insights"
    insights = []
    if insights_dir.exists():
        for f in sorted(insights_dir.glob("*.md")):
            doc = store.read(f"insights/{f.stem}")
            if doc:
                insights.append({
                    "slug": f.stem,
                    **{k: v for k, v in doc.metadata.items()
                       if k not in {"name", "type", "updated"}},
                })
    _print_json({
        "strategy": dict(strat.metadata) if strat else None,
        "long_term_insights": insights,
        "insights_count": len(insights),
    })


# ---------- history ----------

def cmd_history(args: argparse.Namespace) -> None:
    store = MemoryStore()
    n = args.n
    trades = sorted(
        store.read_history(),
        key=lambda t: t.get("ts_origin", t.get("ts", "")),
        reverse=True,
    )[:n]

    debates = []
    debate_dir = store.root / ".debate"
    if debate_dir.exists():
        for date_dir in sorted(debate_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for md in date_dir.glob("*.md"):
                content = md.read_text(encoding="utf-8")
                debates.append({
                    "date": date_dir.name,
                    "asset": md.stem,
                    "summary": "\n".join(content.splitlines()[:8]),
                })
            if len(debates) >= n:
                break

    _print_json({"recent_trades": trades, "recent_debates": debates[:n]})


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
    """
    from utils.gold_price import get_gold_snapshot
    from core.portfolio_manager import PortfolioManager
    try:
        pm = PortfolioManager()
    except FileNotFoundError as e:
        _print_json({
            "status": "error",
            "error": str(e),
            "hint": "memory 还没初始化。先跑 `run.sh init` 完成 onboarding。",
        })
        return

    snap = get_gold_snapshot(offset_pct=0.0)
    cur_gold = snap.spot_cny_per_gram if snap else 1000.0
    cur_audcny = _safe_close("AUDCNY=X") or 4.9

    # 通用价格 dict：每个 holding 的当前价 + 情景价
    cur_prices: Dict[str, float] = {}
    new_prices: Dict[str, float] = {}
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        if str(h.get("kind")) == "metal":
            cur_prices[sym] = cur_gold
        else:
            cur_prices[sym] = _safe_close(sym)
        new_prices[sym] = cur_prices[sym]

    # 应用 --symbol/--pct/--price 通用参数
    if args.symbol:
        sym = args.symbol
        if sym not in cur_prices:
            _print_json({
                "status": "error",
                "error": f"{sym} 不在持仓里",
                "hint": f"用 `run.sh status` 看你有哪些持仓；或先用 GUI / `POST /api/holdings/{sym}` 加进去再跑 what_if",
            })
            return
        if args.price is not None:
            new_prices[sym] = float(args.price)
        elif args.pct is not None:
            new_prices[sym] = cur_prices[sym] * (1 + args.pct / 100)

    # 兼容老 --gold-pct / --gold-price
    if args.gold_price is not None or args.gold_pct is not None:
        new_gold = cur_gold
        if args.gold_price is not None:
            new_gold = args.gold_price
        if args.gold_pct is not None:
            new_gold = cur_gold * (1 + args.gold_pct / 100)
        # 应用到所有 metal holdings
        for h in pm.holdings:
            if str(h.get("kind")) == "metal":
                sym = str(h.get("symbol") or "")
                if sym:
                    new_prices[sym] = new_gold

    # 兼容老 --ndq-pct / --ndq-price
    if args.ndq_price is not None or args.ndq_pct is not None:
        if "NDQ.AX" in cur_prices:
            new_ndq = cur_prices["NDQ.AX"]
            if args.ndq_price is not None:
                new_ndq = args.ndq_price
            if args.ndq_pct is not None:
                new_ndq = cur_prices["NDQ.AX"] * (1 + args.ndq_pct / 100)
            new_prices["NDQ.AX"] = new_ndq

    new_audcny = args.audcny if args.audcny else cur_audcny

    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")

    def _value_in_cny(holding: Dict[str, Any], price: float, fx: float) -> float:
        units = float(holding.get("units", 0) or 0)
        ccy = str(holding.get("cost_currency", "CNY"))
        if ccy == "CNY":
            return units * price
        if ccy == "AUD":
            return units * price * fx
        return units * price  # 其他币种暂当 1:1（v3 多币种再处理）

    cur_total = cash_cny + aud_cash * cur_audcny
    new_total = cash_cny + aud_cash * new_audcny
    breakdown: Dict[str, Any] = {}
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        cur_v = _value_in_cny(h, cur_prices.get(sym, 0.0), cur_audcny)
        new_v = _value_in_cny(h, new_prices.get(sym, 0.0), new_audcny)
        cur_total += cur_v
        new_total += new_v
        breakdown[sym] = {
            "units": float(h.get("units", 0) or 0),
            "cur_price": round(cur_prices.get(sym, 0.0), 4),
            "scenario_price": round(new_prices.get(sym, 0.0), 4),
            "cur_value_cny": round(cur_v, 2),
            "scenario_value_cny": round(new_v, 2),
            "delta_cny": round(new_v - cur_v, 2),
        }

    delta = new_total - cur_total
    _print_json({
        "status": "ok",
        "current_total_cny": round(cur_total, 2),
        "scenario_total_cny": round(new_total, 2),
        "delta_cny": round(delta, 2),
        "delta_pct": round((delta / cur_total) * 100, 2) if cur_total else 0.0,
        "fx": {"audcny_cur": round(cur_audcny, 4), "audcny_scenario": round(new_audcny, 4)},
        "breakdown": breakdown,
    })


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
        import os
        if not os.getenv("DEEPSEEK_API_KEY"):
            out["llm_summary"] = "(--with-llm 需要 DEEPSEEK_API_KEY)"
        else:
            try:
                from openai import OpenAI
                client = OpenAI(
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                )
                ctx = json.dumps({
                    "symbols": symbols, "corr": out["correlation_matrix"],
                    "sectors": sectors, "macro_corr": macro_corr,
                }, ensure_ascii=False)
                resp = client.chat.completions.create(
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
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
                    extra_body={"thinking": {"type": "disabled"}} if "v4" in os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash") else None,
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


# ---------- prepare_debate ----------

def _gather_relevant_insights(store: MemoryStore, asset: Dict[str, Any]) -> str:
    insights_dir = store.root / "insights"
    if not insights_dir.exists():
        return ""
    sym = asset.get("symbol", "").lower().replace("=", "_")
    matches = []
    for f in sorted(insights_dir.glob("*.md")):
        if sym in f.stem.lower() or any(
            tok in f.stem.lower() for tok in ["gold", "ndq"] if tok in sym
        ):
            matches.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')[:600]}")
    return "\n\n".join(matches)


def cmd_prepare_committee(args: argparse.Namespace) -> None:
    """输出 Investment Committee brief — 含项目原生 prompt + 用户上下文，给 Claude 扮演 4 角色"""
    from agents.cio import build_cio_prompt
    from agents.macro_strategist import PROMPT_MACRO_STRATEGIST
    from agents.quant import build_quant_prompt
    from agents.risk_officer import build_risk_officer_prompt
    from core.portfolio_manager import PortfolioManager
    from utils.exchange_fee import (
        analyze_multi_timeframe, get_history_data, get_macro_data
    )
    from utils.gold_price import format_gold_report, get_gold_snapshot

    pm = PortfolioManager()
    target = next(
        (a for a in pm.strategy.get("target_assets", []) if a["symbol"] == args.symbol),
        None,
    )
    if target is None:
        _print_json({
            "status": "error",
            "error": f"asset {args.symbol} not in strategy.target_assets",
            "hint": (
                f"先把 {args.symbol} 加进 strategy.md target_assets 再重试。"
                "GUI 策略页可以加，或参考 references/adding-assets.md 手动编辑。"
            ),
        })
        return

    # 算 metrics + regime 一次，给 analyze_multi_timeframe 和 format_regime_brief 共用
    from core.regime import format_regime_brief
    from utils.market_metrics import compute_metrics

    df_target = get_history_data(target["symbol"], "2y")
    metrics = compute_metrics(df_target)
    market = analyze_multi_timeframe(
        df_target,
        f"{target.get('display_name', target['symbol'])} ({target['symbol']})",
    )
    # P1-2: 传 symbol 让 regime 用 per-asset 阈值
    regime_brief = format_regime_brief(metrics, symbol=target["symbol"])
    macro_data = get_macro_data()
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_ctx = format_gold_report(snap) if (snap and target.get("type") == "metal") else ""

    # 详细的 portfolio 上下文给 Risk Officer (v2 通用化读)
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    ndq_h = pm.holdings.find("NDQ.AX")
    gold_h = pm.holdings.find("GC=F")
    ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    ndq_cost = float(ndq_h.get("avg_cost", 0) or 0) if ndq_h else 0.0
    gold_grams = float(gold_h.get("units", 0) or 0) if gold_h else 0.0
    gold_cost = float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0))
    dry_powder = max(0.0, cash_cny - buffer_cny)
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))

    audcny = _safe_close("AUDCNY=X")
    gold_now = snap.spot_cny_per_gram if snap else 0.0
    total_cny = (
        cash_cny + aud_cash * audcny
        + ndq_shares * _safe_close("NDQ.AX") * audcny
        + gold_grams * gold_now
    )

    ndq_now = _safe_close("NDQ.AX")
    ndq_pnl_pct = ((ndq_now / ndq_cost) - 1) * 100 if ndq_cost > 0 else 0
    gold_pnl_pct = ((gold_now / gold_cost) - 1) * 100 if gold_cost > 0 else 0

    portfolio_summary = (
        f"用户风险偏好: {risk_level}\n"
        f"总资产估算: ¥{total_cny:,.0f}\n"
        f"  - CNY 现金: ¥{cash_cny:,.0f} (应急金 ¥{buffer_cny:,} 不可投)\n"
        f"  - 可投子弹 (dry_powder): ¥{dry_powder:,.0f}\n"
        f"  - AUD 现金: ${aud_cash:,.0f}\n"
        f"  - **NDQ.AX**: {ndq_shares} 股, 均价 ${ndq_cost:.4f}, 现价 ${ndq_now:.2f}, "
        f"浮盈 {ndq_pnl_pct:+.2f}%\n"
        f"  - **黄金 (浙商)**: {gold_grams:.4f}g, 均价 ¥{gold_cost:.2f}/g, "
        f"现价 ¥{gold_now:.2f}/g, 浮盈 {gold_pnl_pct:+.2f}%"
    )
    insights = _gather_relevant_insights(pm.store, target)

    out = {
        "asset": target,
        "portfolio_summary": portfolio_summary,
        "macro_data": macro_data,
        "market_data": market,
        "regime_brief": regime_brief,  # Claude worker 必须把这个塞进 Quant Round 1/2 prompt
        "gold_snapshot": gold_ctx,
        "prior_insights": insights,
        "prompts": {
            "macro_strategist": PROMPT_MACRO_STRATEGIST,
            "quant_round1": build_quant_prompt(target, "opening"),
            "risk_round1": build_risk_officer_prompt(target, "opening"),
            "quant_round2_after_risk": build_quant_prompt(target, "rebuttal"),
            "risk_round2_after_quant": build_risk_officer_prompt(target, "rebuttal"),
            "cio": build_cio_prompt(target),
        },
        "save_command": (
            f"~/.claude/skills/invest/run.sh save_committee {args.symbol}"
        ),
        "instructions": (
            "Claude: 这是 Investment Committee 的 3 轮流程：\n"
            "  Round 1 - 独立陈述: Macro (跨资产共享) + Quant + Risk Officer 各自看自己的数据\n"
            "  Round 2 - 横向交流: Quant 看到 Risk 报告后调整 + Risk 看到 Quant 报告后调整\n"
            "  Round 3 - CIO 综合 4 份输出 + portfolio_summary，输出完整 memo\n"
            "**重要**: 召唤 Quant Round 1 worker 时，必须把 regime_brief 字段塞进 prompt:\n"
            '  "<paste prompts.quant_round1>\\n\\n# 市场 Regime:\\n<paste regime_brief>'
            '\\n\\n# 市场数据:\\n<paste market_data>"\n'
            "Quant Round 2 worker 同样把 regime_brief 重新塞一次，确保它在 cross-challenge 时\n"
            "仍然受 REGIME 硬保护规则约束（防止震荡市底部被 Risk 带跑改 SIGNAL）。\n"
            "请依次扮演 6 段输出，用以下分隔符：\n"
            "=== MACRO ===\n=== QUANT_R1 ===\n=== RISK_R1 ===\n"
            "=== QUANT_R2 ===\n=== RISK_R2 ===\n=== CIO ===\n"
            f"全部完成后通过 stdin 喂给 save_committee {args.symbol}"
        ),
    }
    _print_json(out)


# ---------- run_committee (Direct path — 给非 Claude agent 用) ----------

def cmd_run_committee(args: argparse.Namespace) -> None:
    """一键跑完委员会，返回最终 verdict JSON。

    与 `prepare_committee` + Claude spawn 4 subagent 的 Coordinator 路径不同：
    这个命令直接调 backend `core.committee.run_committee`（DeepSeek-Chat 跑 4 角色），
    任何 agent（Cursor / Cline / Codex / DeepSeek-based / 普通 Python 脚本）一次
    调用就能拿到完整 verdict。**需要 DEEPSEEK_API_KEY**——同 daily_report cron。

    特性：
    - Stage 0 同日检查：今天跑过了直接读历史 transcript 不重跑（可加 --force 强跑）
    - 整个委员会落盘到 memory/.committee/<date>/<asset>.md（带 Provider: deepseek (skill direct)）
    - 输出 JSON：verdict + confidence + 完整 CIO memo + transcript 路径
    """
    import os

    if not os.getenv("DEEPSEEK_API_KEY"):
        _print_json({
            "status": "error",
            "error": "DEEPSEEK_API_KEY 未设。Direct 路径必须有 DeepSeek key。",
            "hint": (
                "走 Coordinator 路径（在 Claude Code 里用 prepare_committee + spawn"
                " subagent）不需要 key。或在 .env 里加 DEEPSEEK_API_KEY 后重试。"
            ),
        })
        sys.exit(1)

    from core.committee import run_committee, run_macro_view
    from core.portfolio_manager import PortfolioManager
    from core.regime import format_regime_brief
    from utils.exchange_fee import (
        analyze_multi_timeframe, get_history_data, get_macro_data,
    )
    from utils.market_metrics import compute_metrics

    pm = PortfolioManager()
    target = next(
        (a for a in pm.strategy.get("target_assets", []) if a["symbol"] == args.symbol),
        None,
    )
    if target is None:
        _print_json({
            "status": "error",
            "error": f"asset {args.symbol} not in strategy.target_assets",
            "hint": "先把 symbol 加进 strategy.md target_assets，见 references/adding-assets.md",
        })
        sys.exit(1)

    # Stage 0：同日检查
    today = datetime.now().strftime("%Y-%m-%d")
    # 注意：backend core/committee.py:_persist_committee_to_memory 用 re.sub
    # 把 symbol 里的非 alnum 字符替换成 _（如 "GC=F" → "GC_F.md"）。这里必须
    # 用同款转换，否则 transcript_path.exists() 永远返回 False，cmd_run_committee
    # 输出的 transcript_path 字段就是空字符串（Fresh Claude 端到端测试发现）
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", args.symbol)
    transcript_path = ROOT / "memory" / ".committee" / today / f"{safe_sym}.md"
    if transcript_path.exists() and not args.force:
        _print_json({
            "status": "cached",
            "reason": "今天已经跑过这个资产了；用 --force 重跑",
            "transcript_path": str(transcript_path),
            "transcript_md": transcript_path.read_text(encoding="utf-8"),
        })
        return

    # 1) Macro view（跨资产共享，但 Direct 单 symbol 调用就跑一次）
    macro_view = run_macro_view(get_macro_data())

    # 2) market data + regime
    df = get_history_data(args.symbol, "2y")
    metrics = compute_metrics(df)
    market_data = analyze_multi_timeframe(
        df, f"{target.get('display_name', args.symbol)} ({args.symbol})",
    )
    regime_brief = format_regime_brief(metrics, symbol=args.symbol)

    # 3) portfolio_summary（复用 prepare_committee 的逻辑做精简版）
    cash_cny = pm.cash_amount("CNY")
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0))
    dry_powder = max(0.0, cash_cny - buffer_cny)
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    holdings_lines = []
    for h in pm.holdings:
        sym = h.get("symbol", "?")
        units = h.get("units", 0)
        avg = h.get("avg_cost", 0)
        ccy = h.get("cost_currency", "CNY")
        holdings_lines.append(f"  - {sym}: {units} @ avg {avg} {ccy}")
    holdings_block = "\n".join(holdings_lines) if holdings_lines else "  - (无)"
    portfolio_summary = (
        f"用户风险偏好: {risk_level}\n"
        f"CNY 现金: ¥{cash_cny:,.0f} (应急金 ¥{buffer_cny:,} 不可投)\n"
        f"可投子弹 (dry_powder): ¥{dry_powder:,.0f}\n"
        f"持仓:\n{holdings_block}"
    )
    prior_insights = _gather_relevant_insights(pm.store, target)

    # 4) 跑！persist_to_memory=True 让 backend 自动落盘 transcript
    result = run_committee(
        asset=target,
        market_data=market_data,
        macro_view=macro_view,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
        regime_brief=regime_brief,
        persist_to_memory=True,
        max_debate_rounds=args.max_rounds,
    )

    verdict = result.get("verdict", {})
    report = result.get("report")
    cio_memo = report.cio_memo if report is not None else ""

    # 检测用户是否配了 NapCat（白名单 QQ 不为 0）—— 没配的话别推 NapCat 命令，
    # 改走 Web GUI / API 路径。多数小白用户没装 NapCat，硬塞会让他们一脸懵
    napcat_qq = os.getenv("INVEST_WHITELIST_QQ", "0").strip()
    has_napcat = napcat_qq and napcat_qq != "0"

    # 顺带把 cio_memo 是 Markdown 这件事再叮嘱一句，agent 拿到后必须 render
    cio_render_hint = (
        "⚠️ `cio_memo` 字段是 Markdown 字符串（含 `## verdict` `**confidence**` 等格式），"
        "**直接当 Markdown 渲染给用户看**，不要把整个 JSON 原样打印。"
    )
    gui_url = f"http://{os.getenv('INVEST_WEB_HOST', '127.0.0.1')}:{os.getenv('INVEST_WEB_PORT', '8765')}"
    gui_troubleshoot = (
        f"如果 {gui_url} 打不开（端口没起 / 网页加载不出来），告诉用户另开终端跑 "
        "`~/.claude/skills/invest/scripts/run.sh gui` 启动后端，然后浏览器刷新。"
    )

    if has_napcat:
        next_step = (
            f"{cio_render_hint}\n\n"
            "已生成 verdict。如果用户同意：黄金/现金交易告诉用户用 NapCat 命令"
            "（如 `/gold_buy 5g @1040`）；其他 yfinance symbol 走 GUI HoldingDialog "
            f"（{gui_url}）或 `POST/PUT /api/holdings/{{symbol}}`。**不要直接写 "
            f"memory/**——所有状态变更必须走带审计的入口。\n\n{gui_troubleshoot}"
        )
    else:
        next_step = (
            f"{cio_render_hint}\n\n"
            "已生成 verdict。如果用户同意，告诉他按下面三步走：\n"
            f"1) 在 openInvest 里登记这笔（最方便：浏览器开 {gui_url} → 持仓页 → "
            "编辑/新增 holding；或 `POST/PUT /api/holdings/{symbol}` API）\n"
            "2) 打开他自己的证券 App / 银行 App，按 verdict 里的 alloc_cny 金额 + "
            "资产 symbol 真实下单（openInvest 本身不接交易所，只做决策）\n"
            f"3) 回 openInvest 标记成交\n\n{gui_troubleshoot}\n\n"
            "**不要直接写 memory/**——所有状态变更必须走带审计的入口。"
        )

    _print_json({
        "status": "ok",
        "asset": target,
        "verdict": verdict,
        "cio_memo": cio_memo,
        "transcript_path": str(transcript_path) if transcript_path.exists() else "",
        "next_step": next_step,
    })


SECTION_RE = re.compile(
    r"^===\s*(MACRO|QUANT_R1|RISK_R1|QUANT_R2|RISK_R2|CIO|QUANT|RISK)\s*===\s*$",
    re.MULTILINE,
)


def cmd_save_committee(args: argparse.Namespace) -> None:
    """读 stdin 上来的 4 段 transcript，落到 memory/.committee/<date>/<asset>.md"""
    raw = sys.stdin.read()
    if not raw.strip():
        _print_json({"error": "empty stdin"})
        return

    parts = SECTION_RE.split(raw)
    sections: Dict[str, str] = {}
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            role = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            sections[role] = content

    cio_text = sections.get("CIO", raw if not sections else "")

    # 解析 CIO 输出
    from core.committee import parse_cio_memo
    verdict = parse_cio_memo(cio_text)

    store = MemoryStore()
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = store.root / ".committee" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", args.symbol)
    path = out_dir / f"{safe_sym}.md"

    lines = [
        f"# Committee: {args.symbol}",
        f"\n**Date**: {today}",
        f"**Provider**: claude (skill mode)",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation CNY**: {verdict['alloc_cny']}",
        "\n\n---\n\n## Reports\n",
    ]
    for role in ["MACRO", "QUANT_R1", "RISK_R1",
                 "QUANT_R2", "RISK_R2", "CIO", "QUANT", "RISK"]:
        if role in sections:
            lines.append(f"\n### {role}\n\n{sections[role]}\n")
    if not sections:
        lines.append(f"\n### RAW (未分段)\n\n{raw}\n")

    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "committee_finished_skill",
        "asset": args.symbol,
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "provider": "claude",
    })
    _print_json({"saved": str(path), "verdict": verdict})


# ============================================================
# 写操作子命令 ——  CLAUDE.md 产品哲学：Agent 必须拥有全部功能（不能只读不写）
# ============================================================

def _resolve_pm():
    """统一拿 PortfolioManager，避免每个 cmd 重复 import"""
    from core.portfolio_manager import PortfolioManager
    return PortfolioManager()


def _now_iso_local():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def cmd_deposit(args: argparse.Namespace) -> None:
    """存入现金（任意币种）。等价 web_api POST /api/cash/{currency}/deposit。

    示例:
        skill deposit --currency CNY --amount 5000
        skill deposit -c AUD -a 300
    """
    ccy = args.currency.upper()
    amount = float(args.amount)
    if amount <= 0:
        _print_json({"status": "error", "error": "amount 必须 > 0"})
        sys.exit(1)
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        _print_json({"status": "error", "error": f"非法币种 {ccy}"})
        sys.exit(1)

    pm = _resolve_pm()
    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) + amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso_local(), "action": "deposit",
        "symbol": ccy, "units": amount, "currency": ccy, "source": "skill_cli",
    })
    _print_json({
        "status": "ok", "currency": ccy, "amount_deposited": amount,
        "new_balance": new_balance, "cny_total": pm.cash_amount("CNY"),
    })


def cmd_withdraw(args: argparse.Namespace) -> None:
    """取出现金（任意币种）。余额不足返回 error。

    示例:
        skill withdraw -c CNY -a 1000
    """
    ccy = args.currency.upper()
    amount = float(args.amount)
    if amount <= 0:
        _print_json({"status": "error", "error": "amount 必须 > 0"})
        sys.exit(1)

    pm = _resolve_pm()
    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        current = float(cash.get(ccy, 0) or 0)
        if current < amount:
            raise SystemExit(json.dumps({
                "status": "error", "error": f"{ccy} 余额 {current} < 取出 {amount}",
            }, ensure_ascii=False))
        cash[ccy] = round(current - amount, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso_local(), "action": "withdraw",
        "symbol": ccy, "units": -amount, "currency": ccy, "source": "skill_cli",
    })
    _print_json({
        "status": "ok", "currency": ccy, "amount_withdrawn": amount,
        "new_balance": current - amount, "cny_total": pm.cash_amount("CNY"),
    })


def cmd_buy(args: argparse.Namespace) -> None:
    """加仓（已有 symbol 增加 units + 加权平均成本；新 symbol 直接建仓）。

    示例:
        skill buy --symbol 510300.SS --units 1000 --price 4.2 --currency CNY --kind etf
        skill buy --symbol AAPL --units 10 --price 175.5 --currency USD --kind equity
        skill buy --symbol GC=F --units 5 --price 700 --currency CNY --kind metal --unit-label 克
    """
    symbol = args.symbol
    units = float(args.units)
    price = float(args.price)
    ccy = args.currency.upper()
    kind = args.kind  # equity/etf/metal/crypto/bond/fund/other
    if units <= 0 or price <= 0:
        _print_json({"status": "error", "error": "units / price 必须 > 0"})
        sys.exit(1)

    cost_cny = units * price  # 简化：fork 用户若用非 CNY 自己换算后再传 price=CNY 价
    pm = _resolve_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        existing = next((h for h in holdings if h.get("symbol") == symbol), None)
        if existing:
            # 加权平均成本
            old_units = float(existing.get("units", 0) or 0)
            old_avg = float(existing.get("avg_cost", 0) or 0)
            new_units = old_units + units
            new_avg = (old_units * old_avg + units * price) / new_units
            existing["units"] = round(new_units, 6)
            existing["avg_cost"] = round(new_avg, 6)
            action_kind = "add"
        else:
            holdings.append({
                "symbol": symbol, "kind": kind, "units": units, "avg_cost": price,
                "unit_label": args.unit_label, "cost_currency": ccy, "proxy_kind": "direct",
            })
            action_kind = "new"
        p["holdings"] = holdings

        # 同步扣现金（保证账本一致：买 X 元股 = 扣 X 元现金）
        cash = dict(p.get("cash") or {})
        cash[ccy] = round(float(cash.get(ccy, 0) or 0) - units * price, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso_local(), "action": "buy",
        "symbol": symbol, "units": units, "price": price,
        "currency": ccy, "source": "skill_cli",
    })
    _print_json({
        "status": "ok", "action": action_kind, "symbol": symbol,
        "units_added": units, "price": price, "currency": ccy,
        "cost_cny_estimate": cost_cny,
    })


def cmd_sell(args: argparse.Namespace) -> None:
    """减仓（units 减少，cost_avg 不变）。units 减完后保留为 0（用 skill delete_holding 真删）。

    示例:
        skill sell --symbol 510300.SS --units 500 --price 4.5
    """
    symbol = args.symbol
    units = float(args.units)
    price = float(args.price)
    if units <= 0 or price <= 0:
        _print_json({"status": "error", "error": "units / price 必须 > 0"})
        sys.exit(1)

    pm = _resolve_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise SystemExit(json.dumps({
                "status": "error", "error": f"symbol {symbol} 不在持仓里",
            }, ensure_ascii=False))
        old_units = float(target.get("units", 0) or 0)
        if old_units < units:
            raise SystemExit(json.dumps({
                "status": "error", "error": f"{symbol} 持仓 {old_units} < 卖出 {units}",
            }, ensure_ascii=False))
        target["units"] = round(old_units - units, 6)
        p["holdings"] = holdings

        # 卖出收回现金（cost_currency = target 的 cost_currency）
        ccy = str(target.get("cost_currency", "CNY")).upper()
        cash = dict(p.get("cash") or {})
        cash[ccy] = round(float(cash.get(ccy, 0) or 0) + units * price, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso_local(), "action": "sell",
        "symbol": symbol, "units": units, "price": price,
        "currency": ccy, "source": "skill_cli",
    })
    _print_json({
        "status": "ok", "symbol": symbol, "units_sold": units, "price": price,
        "proceeds_cny_estimate": units * price,  # 简化估计
        "remaining_units": old_units - units,
    })


def cmd_delete_holding(args: argparse.Namespace) -> None:
    """删除持仓行（units 必须为 0，否则拒绝，让你先 sell 或 set 到 0）。

    示例:
        skill delete_holding --symbol 510300.SS
        skill delete_holding --symbol 510300.SS --force   # 强删（units > 0 也删，慎用）
    """
    symbol = args.symbol
    pm = _resolve_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise SystemExit(json.dumps({
                "status": "error", "error": f"symbol {symbol} 不在持仓",
            }, ensure_ascii=False))
        units = float(target.get("units", 0) or 0)
        is_tracking = bool(target.get("is_tracking_only", False))
        if not is_tracking and units > 0 and not args.force:
            raise SystemExit(json.dumps({
                "status": "error",
                "error": f"{symbol} 持仓 {units} > 0，请先 sell 或加 --force 强删",
            }, ensure_ascii=False))
        p["holdings"] = [h for h in holdings if h.get("symbol") != symbol]
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso_local(), "action": "delete_holding",
        "symbol": symbol, "source": "skill_cli",
    })
    _print_json({"status": "ok", "deleted": symbol})


# ---------- doctor ----------

def cmd_doctor(_: argparse.Namespace) -> None:
    """健康自检：onboarding 是否完成？所有外部依赖可达？

    给 Claude 看的 JSON：每一项是 ok / missing / unreachable，附 hint 教 Claude
    怎么修。让 Claude 第一次帮用户跑 status 失败时，先 doctor 看到底差什么，
    再决定走 AskUserQuestion 还是直接 init。
    """
    import os

    checks: List[Dict[str, Any]] = []

    # 1) memory/ 是否已 onboarding
    store = MemoryStore()
    user_doc = store.read("user")
    portfolio_doc = store.read("portfolio")
    strategy_doc = store.read("strategy")
    memory_ok = bool(user_doc and portfolio_doc and strategy_doc)
    checks.append({
        "name": "memory_initialized",
        "status": "ok" if memory_ok else "missing",
        "detail": (
            "memory/{user,strategy,portfolio}.md 全部就绪"
            if memory_ok else
            "缺 memory/user.md（或 strategy / portfolio）—— 用户还没 onboarding"
        ),
        "hint": (
            None if memory_ok else
            "向用户问以下信息后调 `run.sh init --from-stdin`（详细流程见 "
            "skill/references/onboarding.md）：display_name, risk_tolerance "
            "(Conservative/Balanced/Aggressive), monthly_income_cny / "
            "monthly_expenses_cny / exchange_buffer_cny（都可填 0 跳过），"
            "holdings_description（自由描述持仓，例如 '510300 沪深300ETF "
            "3000 股 4.2 元，余额宝 5 万 CNY'），DEEPSEEK_API_KEY（可选，"
            "Coordinator 路径不需要）。target_assets 留空也行，onboarding "
            "完用户可以通过 GUI 或 references/adding-assets.md 加任意 yfinance symbol。"
        ),
    })

    # 2) .env 凭据
    env_path = ROOT / ".env"
    has_deepseek = bool(os.getenv("DEEPSEEK_API_KEY"))
    has_email_sender = bool(os.getenv("EMAIL_SENDER"))
    has_email_pass = bool(os.getenv("EMAIL_PASSWORD"))
    checks.append({
        "name": ".env_file",
        "status": "ok" if env_path.exists() else "missing",
        "detail": str(env_path) if env_path.exists() else f"{env_path} 不存在",
        "hint": (
            None if env_path.exists() else
            "调 `run.sh init` 时把 deepseek_api_key / email_sender / email_password "
            "写在 stdin JSON 里，或者直接 cp .env.example .env 后用户自己填"
        ),
    })
    checks.append({
        "name": "deepseek_key",
        "status": "ok" if has_deepseek else "missing",
        "detail": "DEEPSEEK_API_KEY 已设" if has_deepseek else "DEEPSEEK_API_KEY 缺失",
        "hint": (
            None if has_deepseek else
            "向用户引导：去 https://platform.deepseek.com 注册 → API keys 页面创建 "
            "→ 把 sk-xxxx 通过 init 的 stdin 传入。失败时仍可用 Claude skill 模式"
            "（不需要 DeepSeek key），但 cron 模式无法跑。"
        ),
    })
    checks.append({
        "name": "gmail_credentials",
        "status": "ok" if (has_email_sender and has_email_pass) else "missing",
        "detail": (
            f"sender={os.getenv('EMAIL_SENDER')}, password set"
            if (has_email_sender and has_email_pass) else
            "EMAIL_SENDER 或 EMAIL_PASSWORD 缺失"
        ),
        "hint": (
            None if (has_email_sender and has_email_pass) else
            "Gmail 必须用 App Password（不是登录密码），需先开 2FA 然后去 "
            "https://myaccount.google.com/apppasswords 生成 16 位 App Password。"
            "未配置时 daily_report 仍能跑完，只是不发邮件。"
        ),
    })

    # 3) DeepSeek key 实测可达（audit PM Major: 失败前置）
    deepseek_reachable = "skipped"
    deepseek_detail = "DEEPSEEK_API_KEY 未设，跳过实测"
    if has_deepseek:
        try:
            import requests
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            r = requests.get(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}"},
                timeout=8,
            )
            if r.status_code == 200:
                deepseek_reachable = "ok"
                deepseek_detail = "DeepSeek API 响应 200，key 有效"
            elif r.status_code == 401:
                deepseek_reachable = "auth_failed"
                deepseek_detail = "DeepSeek 返回 401，key 无效或已过期"
            else:
                deepseek_reachable = "unreachable"
                deepseek_detail = f"DeepSeek 返回 HTTP {r.status_code}"
        except Exception as e:
            deepseek_reachable = "network_error"
            deepseek_detail = f"无法连接 DeepSeek: {type(e).__name__}: {e}"
    checks.append({
        "name": "deepseek_reachable",
        "status": deepseek_reachable if deepseek_reachable in ("ok", "skipped") else "missing",
        "detail": deepseek_detail,
        "hint": (
            None if deepseek_reachable in ("ok", "skipped") else
            "去 https://platform.deepseek.com 检查 key 是否被禁用 / 余额不足。"
            "失败时仍可用 Claude skill 模式跑 prepare_committee。"
        ),
    })

    # 4) 行情数据库 / cache_data 目录
    db_path = ROOT / "db" / "market_data.db"
    cache_dir = ROOT / "cache_data"
    checks.append({
        "name": "data_dirs",
        "status": "ok",  # Dockerfile 里 mkdir 过，本地脚本也兜底
        "detail": (
            f"db={'exists' if db_path.exists() else 'will_be_created'}, "
            f"cache={'exists' if cache_dir.exists() else 'will_be_created'}"
        ),
        "hint": None,
    })

    # 4) Python venv（skill 本身能跑到这里就证明 venv ok，但报告上有更友好）
    checks.append({
        "name": "python_venv",
        "status": "ok",
        "detail": f"running on {sys.version.split()[0]}",
        "hint": None,
    })

    # 5) GUI dist + 是否在跑
    # GUI 是面向**小白用户**的主入口（CLI/skill 是给 agent 和极客的）。
    # 设计原则：
    #   - dist 在 bootstrap 阶段自动拉好（skill/scripts/run.sh 第一次跑就 sync）
    #   - agent 必须主动告知用户 GUI 入口在哪，否则小白找不到
    #   - 但同一会话只在第一次回答提一次 URL，之后别复读
    # 高级用户（明确不想看 GUI 提示）可以 `INVEST_GUI_NUDGE=off`
    gui_nudge_off = os.getenv("INVEST_GUI_NUDGE", "").strip().lower() in {"off", "0", "false", "no"}
    gui_index = ROOT / "static" / "index.html"
    gui_dist_ready = gui_index.exists()
    gui_port = int(os.getenv("INVEST_WEB_PORT", "8765"))
    gui_host = os.getenv("INVEST_WEB_HOST", "127.0.0.1")
    gui_running = False
    try:
        # 不发 HTTP 请求（怕被 hang），用 socket connect 测端口
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            gui_running = (s.connect_ex((gui_host, gui_port)) == 0)
    except Exception:  # noqa: BLE001
        gui_running = False

    if gui_running:
        gui_status = "ok"
        gui_detail = f"GUI 在跑：http://{gui_host}:{gui_port}"
        gui_hint = None if gui_nudge_off else (
            f"⭐ GUI 在跑，URL: http://{gui_host}:{gui_port}。这是面向小白用户的"
            "主面板（持仓 / 委员会直播 / 历史决议 / LLM 用量）。**第一次回答用户**"
            f"时附一句：顺便：浏览器开 http://{gui_host}:{gui_port} 可以看完整面板。"
            "同会话之后别再复读。"
        )
    elif gui_dist_ready:
        gui_status = "ok"
        gui_detail = f"GUI dist 已装但未启动（端口 {gui_port} 没人监听）"
        gui_hint = None if gui_nudge_off else (
            f"⭐ GUI dist 已就绪但 uvicorn 没起。这是面向**小白用户**的主面板，"
            "不告诉他他就找不到。**第一次回答用户**时附一句：「顺便：项目自带 "
            "Web GUI，想看持仓/委员会面板的话另开终端跑 "
            f"`~/.claude/skills/invest/scripts/run.sh gui`，浏览器开 "
            f"http://{gui_host}:{gui_port}」。同会话别复读。"
        )
    else:
        # dist 没装是 bootstrap 失败的信号——主动帮用户装，不是丢锅给用户
        gui_status = "missing"
        gui_detail = "static/index.html 不存在 — bootstrap 阶段 GUI dist 没拉成功"
        gui_hint = (
            "GUI dist 应该在 skill 第一次跑时自动拉，没拉到大概率是 GitHub 网络问题。"
            "**直接帮用户跑** `cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist` "
            "把 dist 装好（不要让用户自己 google 怎么修）；装好后告诉他 GUI 入口在 "
            f"http://{gui_host}:{gui_port}。"
        )

    checks.append({
        "name": "web_gui",
        "status": gui_status,
        "detail": gui_detail,
        "hint": gui_hint,
        "gui_url": f"http://{gui_host}:{gui_port}" if gui_dist_ready else None,
        "gui_running": gui_running,
        "gui_dist_ready": gui_dist_ready,
        "gui_nudge_off": gui_nudge_off,
    })

    overall = "ready" if all(c["status"] == "ok" for c in checks) else "needs_setup"

    # ready_for_subcommands 兼容旧字段；新增分路径就绪标志：
    # - coordinator_ready：Claude Code 走 prepare_committee + spawn 4 subagent，
    #   不需要 DeepSeek key（用 Claude 订阅扮演 worker）
    # - direct_ready：任意 agent 走 run_committee，需要 DeepSeek key 跑 4 角色
    # 旧 ready_for_subcommands 之前要求 has_deepseek，会让 Coordinator 用户被
    # 误判"还没就绪" → agent 反复引导去注册 DeepSeek，体验糟糕
    _print_json({
        "status": overall,
        "ready_for_subcommands": memory_ok,  # 等价于 coordinator_ready
        "coordinator_ready": memory_ok,
        "direct_ready": memory_ok and has_deepseek,
        "next_step": (
            "用户已就绪。Claude Code 用户直接调 status / prepare_committee；"
            "其他 agent（Cursor/Cline/Codex）走 run_committee（需 DEEPSEEK_API_KEY）"
            if overall == "ready" else
            "调 run.sh init 完成 onboarding，缺什么字段看 checks 里 status='missing' 的项"
        ),
        "checks": checks,
    })


# ---------- init ----------

# 自然语言持仓解析的 system prompt —— 喂 DeepSeek-Chat
_HOLDINGS_PARSE_SYSTEM_PROMPT = """你是金融数据解析助手。把用户的自然语言持仓描述解析成严格 JSON。

输出 schema（无 markdown 无解释）：
{
  "cash": {"<currency_code>": <number>},
  "holdings": [
    {
      "symbol": "<yfinance ticker>",
      "kind": "<stock|etf|fund|metal|crypto|bond|other>",
      "units": <number>,
      "unit_label": "<股|份|克|个|盎司>",
      "avg_cost": <number>,
      "cost_currency": "<currency_code>",
      "channel": "<券商/银行渠道，没说就 '未指定'>",
      "display_name": "<易读名>"
    }
  ]
}

Symbol 映射规则：
- 沪市股票/ETF: 6 位代码 + .SS  (510300 → 510300.SS, 600519 → 600519.SS)
- 深市: 6 位 + .SZ
- 港股: 5 位 + .HK
- 美股: 直接 ticker (AAPL, TSLA)
- 澳股: ticker + .AX (NDQ.AX)
- 加密: 大写 + -USD (BTC-USD, ETH-USD)
- 黄金/纸黄金/积存金: GC=F (浙商/工行/招行积存金都映射到 GC=F，渠道写银行名)
- 货币基金/余额宝/朝朝宝/银行理财: 不放 holdings，并入 cash

币种规则：
- 用户没说币种 → CNY
- 美元/USD → USD; 澳元/AUD → AUD; 港元/HKD → HKD

数值规则：
- 用户没说均价 → avg_cost: 0
- 用户没说渠道 → channel: "未指定"
- 缺字段就用合理默认，不要抛错

只输出 JSON 对象本身。"""


def _parse_holdings_with_llm(
    description: str, api_key: str, base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
) -> Dict[str, Any]:
    """调 DeepSeek 把"510300 ETF 3000股 4.2元 + 余额宝 5万"这种自然语言转成 v2 持仓 JSON。

    返回 {"cash": {...}, "holdings": [...]}.
    出错时抛异常，让 cmd_init 决定回退策略（不阻塞 onboarding）。
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    # v4-flash 默认 thinking 模式，关掉走旧 chat 行为
    extra_body = {}
    if "v4" in model.lower() or model == "deepseek-chat":
        extra_body["thinking"] = {"type": "disabled"}
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _HOLDINGS_PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        extra_body=extra_body or None,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    # 兜底归一化：保证两个顶层 key 都在
    parsed.setdefault("cash", {})
    parsed.setdefault("holdings", [])
    return parsed


def _write_v2_portfolio(cash: Dict[str, float], holdings: List[Dict[str, Any]]) -> None:
    """把 LLM 解析出的 v2 schema 直接覆盖写 memory/portfolio.md。

    在 migrate_profile.py 跑完之后调用 —— migrate 写的是 v1 兜底 portfolio.md，
    这里把它替换成包含完整 holdings list 的 v2 版本。

    **2026-05-10 事故防御**：之前测试调 cmd_init 把作者真实持仓覆盖成 fixture
    的 cash 5000 + 空 holdings → 数据丢了。现在加 safety guard：
      1. 如果已有 portfolio.md 含真实持仓（cash 任一币种 > 0 或 holdings 非空），
         **拒绝覆盖**并抛 RuntimeError，让调用方明确传 force=True
      2. 任何成功覆盖前都先备份到 portfolio.md.bak.<timestamp>，事故可恢复
    """
    store = MemoryStore()
    # Safety guard：检查现有 portfolio.md 是否已含真实数据
    existing = store.read("portfolio")
    if existing is not None:
        existing_cash = existing.get("cash") or {}
        existing_holdings = existing.get("holdings") or []
        has_real_data = (
            any(float(v or 0) > 0 for v in existing_cash.values())
            or len(existing_holdings) > 0
        )
        if has_real_data:
            # 备份当前 portfolio.md（事故时可恢复）
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = store.root / f"portfolio.md.bak.{ts}"
            current_path = store.root / "portfolio.md"
            if current_path.exists():
                backup_path.write_text(
                    current_path.read_text(encoding="utf-8"), encoding="utf-8",
                )
            raise RuntimeError(
                f"⚠️ portfolio.md 已含真实数据（cash={existing_cash}, "
                f"{len(existing_holdings)} 条 holdings），拒绝被 cmd_init 覆盖。"
                f"已备份当前到 {backup_path.name}。如确实想重置，删 portfolio.md "
                f"再重跑 init，或调 web_api 的 holdings/cash 端点逐项修改。"
            )
    portfolio_data: Dict[str, Any] = {
        "schema_version": 2,
        "cash": {k: float(v) for k, v in cash.items() if v},
        "holdings": [],
    }
    for h in holdings:
        sym = str(h.get("symbol") or "").strip()
        if not sym:
            continue  # 跳过 LLM 漏 symbol 的脏行
        portfolio_data["holdings"].append({
            "symbol": sym,
            "kind": str(h.get("kind") or "other"),
            "units": float(h.get("units", 0) or 0),
            "unit_label": str(h.get("unit_label") or ""),
            "avg_cost": float(h.get("avg_cost", 0) or 0),
            "cost_currency": str(h.get("cost_currency") or "CNY"),
            "channel": str(h.get("channel") or "未指定"),
            "display_name": str(h.get("display_name") or sym),
        })

    body_lines = ["# 当前持仓", ""]
    body_lines.append("## 现金")
    if not portfolio_data["cash"]:
        body_lines.append("- (无)")
    else:
        for ccy, amount in portfolio_data["cash"].items():
            body_lines.append(f"- **{ccy}**: {amount:,.2f}")
    body_lines += ["", "## 持仓"]
    if not portfolio_data["holdings"]:
        body_lines.append("- (无)")
    else:
        for h in portfolio_data["holdings"]:
            label = h["unit_label"] or ""
            avg = h["avg_cost"]
            ccy = h["cost_currency"]
            body_lines.append(
                f"- **{h['symbol']}** ({h['display_name']}): "
                f"{h['units']} {label} @ avg {avg} {ccy} "
                f"[{h['channel']}]"
            )
    body_lines += [
        "",
        "## 说明",
        "",
        "由 onboarding 写入。之后通过 GUI / NapCat / `POST /api/holdings` 调整，"
        "不要手动编辑 frontmatter。",
    ]
    store.write("portfolio", "state", portfolio_data, "\n".join(body_lines) + "\n")


def cmd_init(args: argparse.Namespace) -> None:
    """交互式 / 半交互式 onboarding 入口。

    两种调用方式：

    1. Claude 模式：从 stdin 喂 JSON，全自动写文件
       $ echo '{"profile": {...}, "env": {...}}' | run.sh init --from-stdin

    2. CLI 模式：用户直接跑，走标准的 input()
       $ run.sh init                        # 交互式问 5 个问题

    JSON schema（仅作字段格式示意，agent 必须用**用户实际值**填，不要照抄数字）：
    {
      "profile": {
        "name": "<display_name>", "risk_tolerance": "Conservative|Balanced|Aggressive",
        "monthly_income_cny": 0, "monthly_expenses_cny": 0,
        "exchange_buffer_cny": 0, "last_run_date": "YYYY-MM-DD",
        "holdings_description": "<自然语言持仓描述，让后端 LLM 解析>",
        "current_assets": {"cash_cny": 0, "aud_cash": 0, "ndq_shares": 0,
                           "gold_grams": 0, "gold_avg_cost_cny_per_gram": 0},
        "investment_strategy": {
          "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
          "max_single_invest_cny": 10000
        }
      },
      "env": {
        "DEEPSEEK_API_KEY": "sk-...", "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "EMAIL_SENDER": "x@gmail.com", "EMAIL_PASSWORD": "xxxx xxxx xxxx xxxx"
      }
    }
    """
    import os
    import shutil
    import subprocess

    if args.from_stdin:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            _print_json({"status": "error", "error": f"invalid JSON on stdin: {e}"})
            sys.exit(1)
    else:
        payload = _interactive_prompt()

    profile = payload.get("profile", {}) or {}
    env_data = payload.get("env", {}) or {}

    # 1) 写 user_profile.json
    profile_path = ROOT / "user_profile.json"
    if profile_path.exists() and not args.force:
        _print_json({
            "status": "skipped",
            "reason": "user_profile.json 已存在，传 --force 覆盖",
            "path": str(profile_path),
        })
        sys.exit(0)
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2) 写 .env（合并已存在的，不覆盖未提供字段）
    env_path = ROOT / ".env"
    existing_env: Dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                existing_env[k.strip()] = v.strip()
    merged_env = {**existing_env, **{k: str(v) for k, v in env_data.items() if v}}
    env_lines = [
        "# Auto-generated by run.sh init — 后续手动修改请直接编辑此文件",
    ]
    for k, v in merged_env.items():
        env_lines.append(f"{k}={v}")
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # 3) 触发 migrate_profile.py
    migrate_script = ROOT / "scripts" / "migrate_profile.py"
    venv_python = ROOT / ".venv" / "bin" / "python"
    py = str(venv_python) if venv_python.exists() else sys.executable
    result = subprocess.run(
        [py, str(migrate_script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )

    # 3b) v2 持仓覆盖：如果 profile 带了 holdings_description（自然语言）或
    # holdings_v2（结构化），优先用它们生成完整 v2 portfolio.md。这一步在
    # migrate_profile.py 之后跑，结果会覆盖 migrate 写的 v1 兜底版本。
    holdings_v2: Dict[str, Any] = profile.get("holdings_v2") or {}  # 结构化直传
    holdings_text = str(profile.get("holdings_description") or "").strip()
    holdings_parse_note: str = ""

    if not holdings_v2 and holdings_text:
        api_key = env_data.get("DEEPSEEK_API_KEY", "").strip()
        if api_key:
            try:
                holdings_v2 = _parse_holdings_with_llm(
                    holdings_text,
                    api_key=api_key,
                    base_url=env_data.get(
                        "DEEPSEEK_BASE_URL", "https://api.deepseek.com",
                    ),
                )
                holdings_parse_note = "parsed via DeepSeek"
            except Exception as exc:  # noqa: BLE001 LLM 失败不阻塞 onboarding
                holdings_parse_note = f"LLM parse failed ({exc!s}); fell back to v1 fields"
        else:
            holdings_parse_note = (
                "holdings_description 提供了，但 DEEPSEEK_API_KEY 缺失 —— "
                "已回退到 v1 cash/ndq_shares 字段。配 key 后跑 init --force 重做。"
            )

    if holdings_v2 and (holdings_v2.get("cash") or holdings_v2.get("holdings")):
        try:
            _write_v2_portfolio(
                holdings_v2.get("cash", {}) or {},
                holdings_v2.get("holdings", []) or [],
            )
            holdings_parse_note = (holdings_parse_note or "v2 written") + "; portfolio.md overwritten with v2 schema"
        except Exception as exc:  # noqa: BLE001 不阻塞
            holdings_parse_note += f"; v2 write failed: {exc!s}"

    # 4) 第一次 init 后跑 doctor 让 Claude 知道还差什么
    final_checks_status = "completed_full" if (
        env_data.get("DEEPSEEK_API_KEY") and env_data.get("EMAIL_SENDER")
    ) else "completed_partial"

    # ---------- next_step 话术组装 ----------
    # 优先级（从高到低）：
    #   1. holdings_description 给了但 key 缺失（v1 fallback）→ 强制降级话术（必说）
    #   2. LLM 解析成功 → 让用户确认解析结果
    #   3. completed_full → 正常 onboarding 完成话术
    #   4. completed_partial（无 holdings_description 场景）→ 告知凭据不完整
    _holdings_desc_given_no_key = (
        bool(holdings_text) and not env_data.get("DEEPSEEK_API_KEY", "").strip()
    )

    if _holdings_desc_given_no_key:
        # 强制话术：告知用户持仓仅记了现金，引导去注册 DeepSeek key
        next_step_text = (
            "你的持仓我暂时按基础模式记录了——只录了现金，没识别你说的具体股票。"
            "想让我自动识别 (510300 → 沪深300ETF 那种)，需要一个免费 DeepSeek API key，"
            "30 秒去 platform.deepseek.com 注册。要不要现在搞定？"
        )
    elif holdings_v2 and holdings_v2.get("holdings"):
        # LLM 解析成功路径：先让用户确认解析内容
        next_step_text = (
            "**先让用户确认 LLM 解析的持仓**（读 `parsed_holdings_for_user_review` "
            "字段给他听）。确认有错的话用 `POST /api/holdings/{symbol}` 修正或重跑 "
            "`run.sh init --force`。确认无误后，调 `run.sh status` 验证持仓显示正确。"
        )
    elif final_checks_status == "completed_full":
        # 完整 onboarding 完成
        next_step_text = (
            "Onboarding 完成。建议立刻调 `run.sh status` 验证持仓正确，然后跑 "
            "`run.sh strategy` 看 target_assets。如果你没追踪任何 yfinance symbol，"
            "可以从 references/adding-assets.md 加。"
        )
    else:
        # 凭据不完整（无 holdings_description、无 DeepSeek key 的普通场景）
        next_step_text = (
            "Profile 已写入，但 .env 凭据不完整。**告诉用户**：你现在还能在 Claude "
            "Code 对话里直接说 '看看我的持仓' / '该不该加仓 X' —— Claude 帮你跑分析"
            "不烧任何 token；之后想用网页/手机看面板，再跑 `run.sh gui` 启动；"
            "想让服务器后台每天自动跑，那时候再去 platform.deepseek.com 注册 key 填 .env。"
        )

    _print_json({
        "status": "ok",
        "completion": final_checks_status,
        "user_profile_path": str(profile_path),
        "env_path": str(env_path),
        "memory_initialized": (ROOT / "memory" / "user.md").exists(),
        "migrate_stdout": result.stdout[-500:] if result.stdout else "",
        "migrate_stderr": result.stderr[-500:] if result.stderr else "",
        "migrate_returncode": result.returncode,
        "holdings_parse_note": holdings_parse_note or "no holdings_description provided",
        "holdings_count": len((holdings_v2 or {}).get("holdings", [])),
        "parsed_holdings_for_user_review": (
            # 把 LLM 解析出来的 holdings 原样回放给 agent，让 agent 把它读给用户确认
            # 一遍："我理解你持有：A 3000 股 4.2 元、B 5 万现金。对吗？"——避免
            # LLM symbol 映射错（比如把宁德时代猜成 300750.SZ 但用户实际买的是 3750.HK）
            holdings_v2 if holdings_v2 else None
        ),
        "user_review_required": bool(holdings_v2 and holdings_v2.get("holdings")),
        "next_step": next_step_text,
    })


def _interactive_prompt() -> Dict[str, Any]:
    """CLI 直接 init 时的交互式输入（Claude 模式从 stdin 喂 JSON，不走这里）"""
    print("=== invest onboarding (CLI mode) ===", file=sys.stderr)
    print(
        "提示：用 Claude Code 的 invest skill 走 Coordinator 路径更友好；"
        "或者把答案拼成 JSON 走 `run.sh init --from-stdin`。",
        file=sys.stderr,
    )

    def ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        v = input(f"{prompt}{suffix}: ").strip()
        return v or default

    # DeepSeek key 先问 —— 决定后面持仓走自然语言还是手动字段
    deepseek_key = ask("DeepSeek API Key (sk-... 可留空跳过)", "")

    profile: Dict[str, Any] = {
        "name": ask("姓名 / display name", "Anonymous"),
        "risk_tolerance": ask(
            "风险偏好 (Conservative / Balanced / Aggressive)", "Balanced"
        ),
        "monthly_income_cny": float(ask("月收入 (CNY，填 0 跳过)", "0")),
        "monthly_expenses_cny": float(ask("月支出 (CNY，填 0 跳过)", "0")),
        "exchange_buffer_cny": float(ask("换汇周转金 (CNY，填 0 表示无)", "0")),
        "last_run_date": "1970-01-01",
        # 给 migrate_profile.py 兜底；如果走自然语言路径，3b 步骤会覆盖
        "current_assets": {"cash_cny": 0.0, "aud_cash": 0.0, "ndq_shares": 0.0},
        "investment_strategy": {
            "target_allocation_stock": 0.7,
            "target_allocation_cash": 0.3,
            "max_single_invest_cny": float(ask("单次入场上限 (CNY)", "10000")),
        },
    }

    if deepseek_key:
        print(
            "\n--- 持仓自然语言录入（推荐）---\n"
            "用一句话描述当前所有持仓 + 现金。例：\n"
            "  '510300 沪深300ETF 3000 股 4.2 元，工行积存金 50 克 750 均价，"
            "余额宝 5 万，AUD 现金 800'\n"
            "留空就跳过，之后用 GUI 或 NapCat 命令补。",
            file=sys.stderr,
        )
        desc = ask("持仓描述（留空跳过）", "")
        if desc:
            profile["holdings_description"] = desc
        else:
            # 没填自然语言也至少问下现金，避免 portfolio.md 全空
            profile["current_assets"]["cash_cny"] = float(
                ask("CNY 现金（用于跑委员会算 dry_powder）", "0")
            )
    else:
        print(
            "\n--- 持仓字段（手动模式 —— 没给 DeepSeek key 没法解析自然语言）---\n"
            "持仓只问现金；新加 yfinance symbol 之后用 GUI / `POST /api/holdings` 补。",
            file=sys.stderr,
        )
        profile["current_assets"]["cash_cny"] = float(ask("CNY 现金", "0"))
        profile["current_assets"]["aud_cash"] = float(ask("AUD 现金", "0"))

    env = {
        "DEEPSEEK_API_KEY": deepseek_key,
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "EMAIL_SENDER": ask("Gmail 发件人地址（可留空跳过邮件）", ""),
        "EMAIL_PASSWORD": ask("Gmail App Password（16 位，可留空）", ""),
    }
    return {"profile": profile, "env": env}


# ---------- main ----------

def main() -> None:
    # 把 sys.stdout 重定向到 stderr，让 utils/* 里的 print() noise 走 stderr。
    # _print_json 用 sys.__stdout__ 写真正的 JSON。
    sys.stdout = sys.stderr

    parser = argparse.ArgumentParser(prog="skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("strategy").set_defaults(func=cmd_strategy)
    sub.add_parser("live_prices").set_defaults(func=cmd_live_prices)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("init")
    p.add_argument("--from-stdin", action="store_true",
                   help="读 stdin 上的 JSON（Claude 模式），否则走交互 input()")
    p.add_argument("--force", action="store_true",
                   help="user_profile.json 已存在时也覆盖")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("history")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("what_if",
        help="P&L 情景模拟（任意 yfinance symbol 涨跌）",
    )
    # 通用参数（推荐）：--symbol + --pct/--price 配对使用
    p.add_argument("--symbol", help="持仓里的 yfinance symbol，如 510300.SS / AAPL / BTC-USD")
    p.add_argument("--pct", type=float, help="该 symbol 涨跌百分比，如 -5 表示跌 5%%")
    p.add_argument("--price", type=float, help="该 symbol 的情景绝对价（与 --pct 二选一）")
    # 旧参数（兼容）：仅对 NDQ.AX 黄金生效
    p.add_argument("--gold-price", type=float, help="兼容旧参数：黄金克价绝对值 CNY/g")
    p.add_argument("--gold-pct", type=float, help="兼容旧参数：黄金涨跌百分比")
    p.add_argument("--ndq-price", type=float, help="兼容旧参数：NDQ.AX 价格 AUD")
    p.add_argument("--ndq-pct", type=float, help="兼容旧参数：NDQ.AX 涨跌百分比")
    p.add_argument("--audcny", type=float, help="情景 AUDCNY 汇率")
    p.set_defaults(func=cmd_what_if)

    p = sub.add_parser("correlate",
        help="跨资产关联分析（任意 yfinance symbol，无金额建议）"
             "—— 用户问'X 和 Y 有啥关联'、'X 趋势像 Y 吗'")
    p.add_argument("--symbols", required=True,
                   help="逗号分隔，至少 2 个，如 'NDQ.AX,0700.HK,510300.SS'")
    p.add_argument("--period", default="6mo",
                   help="yfinance period, e.g. 3mo/6mo/1y/2y，默认 6mo")
    p.add_argument("--with-llm", action="store_true",
                   help="可选：调 DeepSeek 给一句话中文总结（需要 DEEPSEEK_API_KEY）")
    p.set_defaults(func=cmd_correlate)

    p = sub.add_parser("prepare_committee")
    p.add_argument("symbol")
    p.set_defaults(func=cmd_prepare_committee)

    p = sub.add_parser("save_committee")
    p.add_argument("symbol")
    p.set_defaults(func=cmd_save_committee)

    p = sub.add_parser(
        "run_committee",
        help="Direct 路径：调 DeepSeek 一键跑完委员会（任意 agent 可用，"
             "不依赖 Claude Code 的 Agent 工具）。需要 DEEPSEEK_API_KEY。",
    )
    p.add_argument("symbol")
    p.add_argument(
        "--force", action="store_true",
        help="即使今天已经跑过也重新跑（默认会读 cache 不重复消耗 token）",
    )
    p.add_argument(
        "--max-rounds", type=int, default=1, dest="max_rounds",
        help="cross-challenge 最大轮数，默认 1（同 daily_report cron）",
    )
    p.set_defaults(func=cmd_run_committee)

    # ============ 写操作子命令（Agent 用） ============
    p = sub.add_parser("deposit", help="存入现金（任意币种）")
    p.add_argument("--currency", "-c", required=True, help="币种 (CNY/AUD/USD/HKD/...)")
    p.add_argument("--amount", "-a", type=float, required=True, help="存入金额（正数）")
    p.set_defaults(func=cmd_deposit)

    p = sub.add_parser("withdraw", help="取出现金（任意币种），余额不足报错")
    p.add_argument("--currency", "-c", required=True, help="币种")
    p.add_argument("--amount", "-a", type=float, required=True, help="取出金额（正数）")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("buy", help="加仓 / 建新仓（已有 symbol → 加权平均成本；新 → 建仓）")
    p.add_argument("--symbol", required=True, help="yfinance symbol（如 510300.SS / AAPL / BTC-USD）")
    p.add_argument("--units", type=float, required=True, help="买入数量")
    p.add_argument("--price", type=float, required=True, help="单价（与 currency 同币种）")
    p.add_argument("--currency", "-c", default="CNY", help="计价币种，默认 CNY")
    p.add_argument("--kind", choices=["equity", "etf", "metal", "crypto", "bond", "fund", "other"],
                   default="equity", help="资产类型，默认 equity")
    p.add_argument("--unit-label", default="股", help="单位（股/克/oz/coin），默认 '股'")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("sell", help="减仓（units 减，cost_avg 不变，按 holding 的 cost_currency 还现金）")
    p.add_argument("--symbol", required=True)
    p.add_argument("--units", type=float, required=True, help="卖出数量")
    p.add_argument("--price", type=float, required=True, help="卖出单价")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("delete_holding", help="删除持仓行（units 必须 0 或加 --force）")
    p.add_argument("--symbol", required=True)
    p.add_argument("--force", action="store_true", help="units > 0 也强删")
    p.set_defaults(func=cmd_delete_holding)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
