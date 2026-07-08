"""persist — 委员会决议落盘 + macro 快照（从 core/committee.py 拆分，逻辑逐字不变）。

职责：`_persist`（写 committee markdown 到 memory/.committee/<date>/ + dream_event）
+ `_capture_macro_context`（快照决议时的 VIX/TNX/汇率，给 verdict_review 事后归因）。
`_capture_macro_context` 仅被 `_persist` 调用，co-locate 在此。
CommitteeReport 仅作类型注解用，TYPE_CHECKING 下 import 以避免 debate<->persist 运行时循环
（debate 运行时 import 本模块的 _persist；本模块运行时不反向 import debate 符号）。
inner import（utils.exchange_fee.get_history_data）保持函数内，测试 patch 仍生效。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from openinvest.core.memory_store import MemoryStore

if TYPE_CHECKING:
    from openinvest.core.committee.debate import CommitteeReport

log = logging.getLogger(__name__)


def safe_symbol(symbol: str) -> str:
    """symbol → 落盘文件名归一（committee/backtest transcript 的 <symbol>.md）。

    单一可信源：_persist 写文件名用它，断点续跑判存在的脚本也 import 它，避免
    两处手抄正则漂移（改了一处另一处仍按旧规则探测 → 找不到文件静默重跑）。
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", symbol or "asset")


def _capture_macro_context(as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """快照决议时的 macro 状态（给 verdict_review 做事后归因用）。

    audit A1: verdict 错时能区分'模型预判错' vs '宏观突变黑天鹅'。
    例：BUY 后 60 天跌 8%，但同期 VIX +60% → 不是模型差，是黑天鹅冲击。

    Args:
        as_of_date: backtest 模式必传。如果不传，captured_at 用 datetime.now()
            + macro 值用最新 close —— 跟 backtest 的 decision_date 脱节，
            verdict_review 事后归因会用错时间窗口的 VIX 算 macro_shock。
            backtest 模式应传 decision_date（ISO 'YYYY-MM-DD'），让 macro
            快照时间戳和数值都对齐到历史那一天。
    """
    # captured_at 用 decision_date（backtest）或当下（实盘）
    if as_of_date:
        snapshot: Dict[str, Any] = {"captured_at": as_of_date}
    else:
        snapshot = {"captured_at": datetime.now().isoformat(timespec="seconds")}
    try:
        from openinvest.utils.exchange_fee import get_history_data
        for sym, label in [("^VIX", "vix"), ("^TNX", "tnx"),
                           ("USDCNY=X", "usdcny"), ("AUDCNY=X", "audcny")]:
            df = get_history_data(sym, "5d", as_of_date=as_of_date)
            if not df.empty:
                snapshot[label] = round(float(df["Close"].iloc[-1]), 4)
    except Exception as e:
        snapshot["_capture_error"] = str(e)[:120]
    return snapshot


def _persist(report: "CommitteeReport", verdict: Dict[str, Any],
             *, output_dir: Optional[Any] = None,
             date_override: Optional[str] = None,
             debate_meta: Optional[Dict[str, Any]] = None) -> None:
    """落盘 committee markdown

    output_dir: 默认 memory/.committee/<date>/；backtest 时覆盖到 memory/.backtest/
    date_override: backtest 穿越历史某天用
    debate_meta: 多轮辩论元信息 + 完整 quant/risk history（v3 新增）
    """
    import json as _json
    store = MemoryStore()
    today = date_override or datetime.now().strftime("%Y-%m-%d")
    out_dir = output_dir if output_dir is not None else (store.root / ".committee" / today)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = safe_symbol(report.asset.get("symbol", "asset"))
    path = out_dir / f"{safe_sym}.md"

    # backtest 时 macro_ctx 必须用 decision_date，不是当下 now()。否则
    # verdict_review 事后归因会用错时间窗口的 VIX 值算 macro_shock。
    macro_ctx = _capture_macro_context(as_of_date=date_override)

    lines = [
        f"# Committee: {report.asset.get('display_name', report.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        # 真实 yfinance symbol（机器可读）。文件名 safe_sym 转义有损（GC=F→GC_F、
        # NDQ.AX→NDQ_AX），verdict_review 事后复盘要靠这行拿回原 symbol 才能拉对行情。
        f"**Symbol**: {report.asset.get('symbol', '')}",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation CNY**: {verdict['alloc_cny']}",
    ]

    # v3: 多轮辩论元信息
    if debate_meta:
        lines.append(
            f"**Debate**: {debate_meta['final_round']} round(s) "
            f"(max {debate_meta['max_rounds']}, "
            f"{'converged' if debate_meta['converged'] else 'hit limit'})"
        )

    lines.extend([
        "\n---\n\n## Macro Context Snapshot (for post-hoc attribution)\n",
        f"```json\n{_json.dumps(macro_ctx, ensure_ascii=False, indent=2)}\n```",
        "\n---\n\n## CIO Memo (Round 3)\n",
        report.cio_memo,
        "\n\n---\n\n## Macro Strategist (shared)\n",
        report.macro_view,
    ])
    # 确定性事实块落盘（审计 + verdict_review 事后归因 + GUI 展示）
    if report.valuation_brief:
        lines.append(
            f"\n\n---\n\n## Valuation (deterministic)\n{report.valuation_brief}"
        )
    if report.sentiment_brief:
        lines.append(
            f"\n\n---\n\n## Market Sentiment (deterministic)\n{report.sentiment_brief}"
        )
    lines.extend([
        "\n\n---\n\n## Round 1 — Independent Briefs\n",
        f"\n### Quant Analyst\n{report.quant_view}",
        f"\n### Risk Officer\n{report.risk_view}",
        "\n\n---\n\n## Round 2 — Cross-Challenge Adjustments\n",
        f"\n### Quant adjusted (after seeing Risk)\n{report.quant_adjusted}",
        f"\n### Risk adjusted (after seeing Quant)\n{report.risk_adjusted}",
    ])

    # v3: 多轮辩论历史（仅 max_debate_rounds > 1 时有 Round 3+）
    if debate_meta and debate_meta.get("final_round", 1) > 2:
        quant_hist = debate_meta.get("quant_history", [])
        risk_hist = debate_meta.get("risk_history", [])
        lines.append("\n\n---\n\n## Extended Debate (Rounds 3+)\n")
        for i in range(2, len(quant_hist)):    # Round 3 起 (idx=2)
            round_no = i + 1
            lines.append(f"\n### Round {round_no} — Quant\n{quant_hist[i]}")
            lines.append(f"\n### Round {round_no} — Risk\n{risk_hist[i]}")

    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "committee_finished",
        "asset": report.asset.get("symbol"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "macro_at_decision": macro_ctx,
        "is_backtest": output_dir is not None,
        "debate_rounds": debate_meta["final_round"] if debate_meta else 2,
        "debate_converged": debate_meta["converged"] if debate_meta else None,
    })


__all__ = [
    "_persist",
    "_capture_macro_context",
    "safe_symbol",
]
