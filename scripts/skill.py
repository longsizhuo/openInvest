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

from core.memory_store import MemoryStore  # noqa: E402


def _safe_close(symbol: str) -> float:
    from utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------- status ----------

def cmd_status(_: argparse.Namespace) -> None:
    from utils.gold_price import get_gold_snapshot
    store = MemoryStore()
    user = store.read("user")
    portfolio = store.read("portfolio")

    cash_cny = float(portfolio.get("cash_cny", 0))
    aud_cash = float(portfolio.get("aud_cash", 0))
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    gold_grams = float(portfolio.get("gold_grams", 0))
    gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0))

    ndq_price = _safe_close("NDQ.AX")
    audcny = _safe_close("AUDCNY=X")
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_now = snap.spot_cny_per_gram if snap else 0.0

    out = {
        "user": {
            "name": user.get("display_name") if user else "unknown",
            "risk_tolerance": user.get("risk_tolerance") if user else None,
        },
        "cash": {
            "cny": round(cash_cny, 2),
            "aud": round(aud_cash, 2),
            "aud_in_cny": round(aud_cash * audcny, 2),
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
    from utils.gold_price import get_gold_snapshot
    store = MemoryStore()
    portfolio = store.read("portfolio")
    if portfolio is None:
        _print_json({"error": "portfolio.md missing"})
        return

    cash_cny = float(portfolio.get("cash_cny", 0))
    aud_cash = float(portfolio.get("aud_cash", 0))
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    gold_grams = float(portfolio.get("gold_grams", 0))
    gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0))

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
        _print_json({"error": f"asset {args.symbol} not in strategy.target_assets"})
        return

    market = analyze_multi_timeframe(
        get_history_data(target["symbol"], "2y"),
        f"{target.get('display_name', target['symbol'])} ({target['symbol']})",
    )
    macro_data = get_macro_data()
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_ctx = format_gold_report(snap) if (snap and target.get("type") == "metal") else ""

    # 详细的 portfolio 上下文给 Risk Officer
    cash_cny = float(pm.portfolio.get("cash_cny", 0))
    aud_cash = float(pm.portfolio.get("aud_cash", 0))
    ndq_shares = float(pm.portfolio.get("ndq_shares", 0))
    ndq_cost = float(pm.portfolio.get("ndq_avg_cost_aud_per_share", 0))
    gold_grams = float(pm.portfolio.get("gold_grams", 0))
    gold_cost = float(pm.portfolio.get("gold_avg_cost_cny_per_gram", 0))
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
            "请依次扮演 6 段输出，用以下分隔符：\n"
            "=== MACRO ===\n=== QUANT_R1 ===\n=== RISK_R1 ===\n"
            "=== QUANT_R2 ===\n=== RISK_R2 ===\n=== CIO ===\n"
            f"全部完成后通过 stdin 喂给 save_committee {args.symbol}"
        ),
    }
    _print_json(out)


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


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(prog="skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("strategy").set_defaults(func=cmd_strategy)
    sub.add_parser("live_prices").set_defaults(func=cmd_live_prices)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
