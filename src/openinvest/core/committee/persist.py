"""persist — 委员会决议落盘 + macro 快照（从 core/committee.py 拆分，逻辑逐字不变）。

职责：`_persist`（写 committee markdown 到 memory/.committee/<date>/ + dream_event）
+ `_capture_macro_context`（快照决议时的 VIX/TNX/汇率，给 verdict_review 事后归因）。
`_capture_macro_context` 仅被 `_persist` 调用，co-locate 在此。
CommitteeReport 仅作类型注解用，TYPE_CHECKING 下 import 以避免 debate<->persist 运行时循环
（debate 运行时 import 本模块的 _persist；本模块运行时不反向 import debate 符号）。
inner import（utils.exchange_fee.get_history_data）保持函数内，测试 patch 仍生效。
"""
from __future__ import annotations

from openinvest.core.evidence_eligibility import digest

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from openinvest.core.committee.agent_io import worker_output_status
from openinvest.core.committee.cio_parse import forced_hold, rewrite_tags
from openinvest.core.memory_store import MemoryStore
from openinvest.utils.base_currency import base_currency

if TYPE_CHECKING:
    from openinvest.core.committee.debate import CommitteeReport

log = logging.getLogger(__name__)


# 单一可信源挪到中立层 utils.symbols（issue #179 P1-B④：jobs/connectors 直接
# import core.committee 撞分层契约，9 处手抄由此而来）。此处 re-export 保持
# `from openinvest.core.committee import safe_symbol` 等全部历史路径可用。
from openinvest.utils.symbols import safe_symbol  # noqa: F401


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


RUNS_SUBDIR = "runs"  # 同日重跑的早先 transcript 归档目录（<date>/runs/<sym>_<HHMMSS>.md）


def build_input_health(report: "CommitteeReport", verdict: Dict[str, Any],
                       debate_meta: Optional[Dict[str, Any]] = None,
                       input_health: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合成 Input Health 块：调用方传来的输入健康位 + 本层能看见的事实（纯函数）。

    - 调用方（session）知道：price_last_bar / price_fetch_failed / empty_inputs / macro_data_empty
    - 本层知道：哪些角色输出带 [WORKER_UNAVAILABLE]、模型名、CIO JSON 模式、
      确定性规则改写了什么（rewrite_tags）、是否被强制 HOLD
    所有键都是"事实"不是"判断"：valuation 对黄金本就为空，是否算降级由 verdict_review 决定。
    """
    health: Dict[str, Any] = {"schema": 1, **(input_health or {})}
    unavailable = [role for role, text in (
        ("macro", report.macro_view), ("quant", report.quant_view),
        ("risk", report.risk_view), ("cio", report.cio_memo),
    ) if worker_output_status(text)["unavailable"]]
    if debate_meta is not None and "worker_calls" in debate_meta:
        calls=debate_meta["worker_calls"]
        unavailable=sorted({call["role"] for call in calls if call.get("used",True) and call["unavailable"]})
        health["worker_calls"]=calls
    health["worker_unavailable"] = unavailable
    from openinvest.core.committee.role_health import risk_output_health
    risk_history=(debate_meta or {}).get('risk_history')
    if risk_history is None:
        risk_history=[report.risk_view]
        if getattr(report,'risk_adjusted',''):risk_history.append(report.risk_adjusted)
    health['role_health']=[risk_output_health(text,'RISK_R'+str(i+1)) for i,text in enumerate(risk_history)]
    health['model_output_errors']=[{'role':row['role'],'round':row['round'],
        'output_sha256':row['output_sha256'],'contract_version':row['contract_version'],
        'reason':'model_output_'+row['structure'],'issue_refs':row['issue_refs']}
        for row in health['role_health'] if row['structure'] in {'incomplete','invalid'}]
    scenario=verdict.get('scenario_validation') or {}
    health['scenario_validation']={key:scenario[key] for key in ('status','position_ref','errors','memo_sha256') if key in scenario}
    health['model_output_errors'].extend({'role':'cio','round':'CIO','reason':item['code'],
        'output_sha256':scenario.get('memo_sha256'),'issue_refs':[digest(item)]}
        for item in scenario.get('errors',[]))
    if debate_meta:
        if debate_meta.get("model") is not None:
            health["model"] = debate_meta["model"]
        if "cio_json_mode" in debate_meta:
            health["cio_json_mode"] = bool(debate_meta["cio_json_mode"])
    health["rewritten"] = rewrite_tags(verdict)
    health["forced_hold"] = forced_hold(verdict)
    health["cio_unclear"] = str(verdict.get("verdict") or "").upper() == "UNCLEAR"
    health["decision_status"] = verdict.get("decision_status", "READY")
    review=verdict.get("reasoning_review") or {}
    health["reasoning_review"]={key:review[key] for key in ("status","request_sha256","reason","contract_error") if key in review}
    return health


def _archive_previous_run(path: Any) -> Optional[Any]:
    """同日已有 transcript → 挪到 <date>/runs/<sym>_<HHMMSS>.md（按旧文件 mtime 命名）。

    事件触发的同日重跑过去直接覆盖 10:00 的日报裁决，verdict_review 只看得到最后一次。
    归档目录不在任何读方的 glob("*.md") 范围内（它们都只扫 <date>/ 一层），
    <sym>.md 仍是"当日最新"的唯一可信源，读方零改。
    """
    if not path.exists():
        return None
    stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%H%M%S")
    runs_dir = path.parent / RUNS_SUBDIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"{path.stem}_{stamp}.md"
    n = 1
    while target.exists():  # 同秒重跑：加序号，绝不覆盖
        n += 1
        target = runs_dir / f"{path.stem}_{stamp}_{n}.md"
    try:
        path.replace(target)
    except FileNotFoundError:
        # 同标的并发跑（web 触发撞上 cron）：另一个进程已经把它挪走了。旧行为是直接覆盖，
        # 这里也不能比旧行为更糟——当没有旧件，照常写新文件（CR #431 建议 2）。
        return None
    return target


def _persist(report: "CommitteeReport", verdict: Dict[str, Any],
             *, output_dir: Optional[Any] = None,
             date_override: Optional[str] = None,
             debate_meta: Optional[Dict[str, Any]] = None,
             input_health: Optional[Dict[str, Any]] = None) -> None:
    """落盘 committee markdown

    output_dir: 默认 memory/.committee/<date>/；backtest 时覆盖到 memory/.backtest/
    date_override: backtest 穿越历史某天用
    debate_meta: 多轮辩论元信息 + 完整 quant/risk history（v3 新增）
    input_health: 调用方算好的输入健康位（见 build_input_health）；None 也会写块
        （本层自己知道的 worker_unavailable / rewritten / forced_hold 照记）
    live（output_dir=None）同日重跑：旧 transcript 先归档到 <date>/runs/，不再覆盖丢失。
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
    run_at = date_override or datetime.now().isoformat(timespec="seconds")
    from openinvest.core.committee.scenarios import validate_scenarios,apply_scenario_validation,proposed_scenarios,decision_display_memo
    original_model_memo=getattr(report,'cio_model_memo','') or report.cio_memo
    if 'scenario_validation' not in verdict:
        apply_scenario_validation(verdict,validate_scenarios(getattr(report,'position_facts',None),proposed_scenarios(verdict),
            memo=original_model_memo,context_id=(input_health or {}).get('context_id'),fields=verdict.get('_raw_proposal')))
    health = build_input_health(report, verdict, debate_meta, input_health)
    from openinvest.core.decision_evidence import enforce_input_health, advice_qualified
    enforce_input_health(verdict, health)
    prepared_context=None
    if health.get("context_id") and output_dir is None:
        from openinvest.core.decision_evidence import load_context
        try:
            prepared_context=load_context(store.root, health["context_id"], report.asset.get("symbol", ""))
        except (OSError, ValueError, KeyError, TypeError) as e:
            verdict.setdefault("_original_verdict", verdict.get("verdict"))
            verdict.setdefault("_original_alloc", verdict.get("alloc_cny"))
            verdict.update(verdict="UNCLEAR", alloc_cny=0, confidence=0.0,
                           decision_status="DATA_UNTRUSTED", hold_reason="context_changed")
            health.update(decision_status="DATA_UNTRUSTED", context_error=str(e))
            report.cio_memo = "[QUALITY_ALERT] DATA_UNTRUSTED：输入或规则在分析期间变化，请重新分析。\n\n" + report.cio_memo
    report.cio_memo=("[AUDIT_ONLY] 历史研究原文；不授予当前建议、交易或学习资格。\n\n"+original_model_memo
                     if output_dir is not None else decision_display_memo(verdict,original_model_memo,verdict['scenario_validation']))
    from openinvest.core.decision_evidence import record_decision
    evidence = record_decision(store.root, report.asset.get("symbol", ""),
                               {"cio_memo": report.cio_memo,"cio_raw_response":getattr(report,"cio_raw_response",""),
                                "cio_attempts":getattr(report,"cio_attempts",[]),"original_model_memo":original_model_memo,"fields": verdict.get("_raw_proposal"),
                                "macro": report.macro_view, "quant": report.quant_view,
                                "risk": report.risk_view,"worker_outputs":(debate_meta or {}).get("worker_outputs",[]),
                                "quant_history":(debate_meta or {}).get("quant_history",[]),"risk_history":(debate_meta or {}).get("risk_history",[])}, verdict, health,
                               context_id=health.get("context_id"), historical=output_dir is not None)
    health.update(evidence)
    verdict.update(evidence)
    if verdict.get("decision_status") == "DATA_UNTRUSTED" and "[QUALITY_ALERT]" not in report.cio_memo:
        report.cio_memo = "[QUALITY_ALERT] DATA_UNTRUSTED：当前没有可执行建议，以下仅保留原始意见供排查。\n\n" + report.cio_memo
    elif not advice_qualified(verdict) and "[QUALITY_ALERT]" not in report.cio_memo:
        report.cio_memo="[QUALITY_ALERT] REVIEW_REQUIRED：推理核验或执行资格未通过；原提案仅供审计，不是当前可执行建议。\n\n"+report.cio_memo
    if output_dir is None and verdict.get("review_plan") is not None and advice_qualified(verdict):
        from openinvest.services.review_plans import update_plan
        verdict["review_plan_state"] = update_plan(store, report.asset.get("symbol", ""),
                                                   verdict["review_plan"], evidence_id=evidence["decision_id"])
    if output_dir is None and verdict.get("risk_review_plan") is not None and advice_qualified(verdict):
        from openinvest.services.review_plans import update_plan
        verdict["risk_review_plan_state"] = update_plan(store, report.asset.get("symbol", ""),
            verdict["risk_review_plan"], evidence_id=evidence["decision_id"], purpose="risk")

    lines = [
        f"# Committee: {report.asset.get('display_name', report.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        # 真实 yfinance symbol（机器可读）。文件名 safe_sym 转义有损（GC=F→GC_F、
        # NDQ.AX→NDQ_AX），verdict_review 事后复盘要靠这行拿回原 symbol 才能拉对行情。
        f"**Symbol**: {report.asset.get('symbol', '')}",
        f"**Run at**: {run_at}",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation {base_currency()}**: {verdict['alloc_cny']}",
    ]

    # v3: 多轮辩论元信息
    if debate_meta:
        ending=("stopped: "+debate_meta["termination"]) if debate_meta.get("termination") else ("converged" if debate_meta["converged"] else "hit limit")
        lines.append(
            f"**Debate**: {debate_meta['final_round']} round(s) "
            f"(max {debate_meta['max_rounds']}, "
            f"{ending})"
        )

    quant_hist=(debate_meta or {}).get("quant_history",[])
    risk_hist=(debate_meta or {}).get("risk_history",[])
    quant_second=quant_hist[1] if len(quant_hist)>1 else report.quant_adjusted
    risk_second=risk_hist[1] if len(risk_hist)>1 else report.risk_adjusted
    lines.extend([
        "\n---\n\n## Macro Context Snapshot (for post-hoc attribution)\n",
        f"```json\n{_json.dumps(macro_ctx, ensure_ascii=False, indent=2)}\n```",
        # 输入健康位（事实不判断）：哪些输入为空 / 行情最后一根 / worker 失败 / 规则改写。
        # verdict_review 据此把"残缺输入上做的裁决"和"强制 HOLD"单独标出来，不再混进命中率。
        "\n---\n\n## Input Health (deterministic)\n",
        f"```json\n{_json.dumps(health, ensure_ascii=False, indent=2)}\n```",
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
        f"\n### Quant adjusted (after seeing Risk)\n{quant_second}",
        f"\n### Risk adjusted (after seeing Quant)\n{risk_second}",
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

    # 归档放在一切都拼好之后、写盘前一行：中间任何一步（macro 快照拉行情、拼块）抛异常时，
    # 当天的 <sym>.md 必须原样还在——mcp_server / committee_cmds 靠它判断"今天跑过没"
    # （CR #431 建议 1）。backtest（output_dir 指定）反复重写是有意的，不归档。
    if output_dir is None:
        archived = _archive_previous_run(path)
        if archived is not None:
            log.info("persist: 同日重跑，旧 transcript 归档到 %s", archived)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    from openinvest.core.decision_evidence import _write_once
    immutable = store.root / ".evidence/transcripts" / f"{evidence['decision_id']}.md"
    immutable.parent.mkdir(parents=True, exist_ok=True)
    with immutable.open("x", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines))
    _write_once(immutable.with_suffix(".json"), {"text": "\n".join(lines)})
    if output_dir is None and health.get("context_id"):
        from openinvest.services.agent_queue import acknowledge_context_decision
        review=verdict.get("reasoning_review") or {}
        if prepared_context and review.get("status")!="passed" and verdict.get("proposal_available") is not False:
            from openinvest.services.agent_queue import mark_requests_waiting_review
            mark_requests_waiting_review(store.root,prepared_context["inputs"].get("trigger_requests",[]),
                health["context_id"],review.get("request_sha256","unknown"),review.get("status","unavailable"))
        acknowledge_context_decision(store.root,health["context_id"],evidence["decision_id"])
    store.dream_event({
        "phase": "committee_finished",
        "asset": report.asset.get("symbol"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "macro_at_decision": macro_ctx,
        "is_backtest": output_dir is not None,
        "debate_rounds": debate_meta["final_round"] if debate_meta else 2,
        "debate_converged": debate_meta["converged"] if debate_meta else None,
        "input_health": health,
    })


__all__ = [
    "_persist",
    "_capture_macro_context",
    "build_input_health",
    "_archive_previous_run",
    "RUNS_SUBDIR",
    "safe_symbol",
]
