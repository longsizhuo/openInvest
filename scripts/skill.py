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
    """v2 通用化：从 PortfolioManager 读 cash + holdings"""
    from utils.gold_price import get_gold_snapshot
    from core.portfolio_manager import PortfolioManager
    try:
        pm = PortfolioManager()
    except FileNotFoundError as e:
        _print_json({"error": str(e)})
        return

    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    ndq_h = pm.holdings.find("NDQ.AX")
    gold_h = pm.holdings.find("GC=F")
    ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    gold_grams = float(gold_h.get("units", 0) or 0) if gold_h else 0.0
    gold_avg = float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0

    snap = get_gold_snapshot(offset_pct=0.0)
    cur_gold = snap.spot_cny_per_gram if snap else 1000.0
    cur_ndq = _safe_close("NDQ.AX")
    cur_audcny = _safe_close("AUDCNY=X") or 4.9

    new_gold = args.gold_price if args.gold_price else cur_gold
    if args.gold_pct is not None:
        new_gold = cur_gold * (1 + args.gold_pct / 100)
    new_ndq = args.ndq_price if args.ndq_price else cur_ndq
    if args.ndq_pct is not None:
        new_ndq = cur_ndq * (1 + args.ndq_pct / 100)
    new_audcny = args.audcny if args.audcny else cur_audcny

    cur_total = (cash_cny + aud_cash * cur_audcny
                 + ndq_shares * cur_ndq * cur_audcny
                 + gold_grams * cur_gold)
    new_total = (cash_cny + aud_cash * new_audcny
                 + ndq_shares * new_ndq * new_audcny
                 + gold_grams * new_gold)
    delta = new_total - cur_total

    _print_json({
        "current": {
            "gold_cny_per_g": round(cur_gold, 2),
            "ndq_aud": round(cur_ndq, 2),
            "audcny": round(cur_audcny, 4),
            "total_cny": round(cur_total, 2),
        },
        "scenario": {
            "gold_cny_per_g": round(new_gold, 2),
            "ndq_aud": round(new_ndq, 2),
            "audcny": round(new_audcny, 4),
            "total_cny": round(new_total, 2),
        },
        "delta_cny": round(delta, 2),
        "delta_pct": round((delta / cur_total) * 100, 2) if cur_total else 0.0,
        "breakdown": {
            "gold_grams": gold_grams,
            "gold_avg_cost": gold_avg,
            "gold_pnl_at_scenario_cny": round((new_gold - gold_avg) * gold_grams, 2),
            "ndq_shares": ndq_shares,
            "ndq_value_at_scenario_cny": round(ndq_shares * new_ndq * new_audcny, 2),
        },
    })


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
    transcript_path = ROOT / "memory" / ".committee" / today / f"{args.symbol}.md"
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

    if has_napcat:
        next_step = (
            "已生成 verdict。如果用户同意：黄金/现金交易告诉用户用 NapCat 命令"
            "（如 `/gold_buy 5g @1040`）；其他 yfinance symbol 走 GUI HoldingDialog "
            "或 `POST/PUT /api/holdings/{symbol}`。**不要直接写 memory/**——所有"
            "状态变更必须走带审计的入口。"
        )
    else:
        next_step = (
            "已生成 verdict。如果用户同意：**通过 Web GUI 录入这笔交易**"
            "（http://127.0.0.1:8765 → 持仓页 → 编辑/新增 holding），或 `POST/PUT "
            "/api/holdings/{symbol}` API 直接写。**不要直接写 memory/**——所有"
            "状态变更必须走带审计的入口。\n\n"
            "用户问'我现在去哪买'：告诉他打开自己的证券 App / 银行 App "
            "（按 verdict 的 alloc_cny 金额 + 资产 symbol 自己去执行）—— openInvest "
            "本身不接交易所，只做决策。"
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
            "向用户问以下信息后调 `run.sh init`：display_name, monthly_income_cny, "
            "monthly_expenses_cny, exchange_buffer_cny, risk_tolerance "
            "(Conservative/Balanced/Aggressive), 当前持仓（cash_cny / aud_cash / "
            "ndq_shares / gold_grams / gold_avg_cost_cny_per_gram），以及 "
            "target_assets 数组（可用默认 NDQ.AX + GC=F）"
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
    model: str = "deepseek-chat",
) -> Dict[str, Any]:
    """调 DeepSeek 把"510300 ETF 3000股 4.2元 + 余额宝 5万"这种自然语言转成 v2 持仓 JSON。

    返回 {"cash": {...}, "holdings": [...]}.
    出错时抛异常，让 cmd_init 决定回退策略（不阻塞 onboarding）。
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _HOLDINGS_PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
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
    """
    store = MemoryStore()
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

    JSON schema (见 user_profile.example.json)：
    {
      "profile": {
        "name": "Loong", "risk_tolerance": "Aggressive",
        "monthly_income_cny": 20000, "monthly_expenses_cny": 8000,
        "exchange_buffer_cny": 5000, "last_run_date": "2026-01-01",
        "current_assets": {"cash_cny": 50000, "aud_cash": 0, "ndq_shares": 0,
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
        "next_step": (
            (
                # 如果走了 LLM 解析路径，先让用户确认再继续
                "**先让用户确认 LLM 解析的持仓**（读 `parsed_holdings_for_user_review` "
                "字段给他听）。确认有错的话用 `POST /api/holdings/{symbol}` 修正或重跑 "
                "`run.sh init --force`。确认无误后，调 `run.sh status` 验证持仓显示正确。"
                if (holdings_v2 and holdings_v2.get("holdings"))
                else
                "Onboarding 完成。建议立刻调 `run.sh status` 验证持仓正确，然后跑 "
                "`run.sh strategy` 看 target_assets。如果你没追踪任何 yfinance symbol，"
                "可以从 references/adding-assets.md 加。"
            )
            if final_checks_status == "completed_full" else
            "Profile 已写入，但 .env 凭据不完整。Coordinator 模式（Claude Code 里"
            "用 prepare_committee）可以立刻跑；Direct/Cron 模式（任意 agent 跑 "
            "run_committee）需要补 DEEPSEEK_API_KEY 才能用。"
        ),
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

    p = sub.add_parser("what_if")
    p.add_argument("--gold-price", type=float)
    p.add_argument("--gold-pct", type=float)
    p.add_argument("--ndq-price", type=float)
    p.add_argument("--ndq-pct", type=float)
    p.add_argument("--audcny", type=float)
    p.set_defaults(func=cmd_what_if)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
