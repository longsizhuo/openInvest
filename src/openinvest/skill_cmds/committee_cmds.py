"""committee_cmds —— 投资委员会相关 skill 子命令

逐字搬运自 scripts/skill.py：
- cmd_prepare_committee：输出 Committee brief（Coordinator 路径，Claude 扮演 4 角色）。
- cmd_run_committee：Direct 路径，调 core.committee_runner 一键跑完拿 verdict。
- cmd_save_committee：把 stdin transcript 落盘。

本模块定义自有 ROOT —— cmd_run_committee 在模块全局读 ROOT 拼 transcript_path，
patch 必须命中本模块的 ROOT（见 monkeypatch 重定向说明）。
core.committee_runner 是 service layer，allow_indirect_imports 合法（不直 import core.committee）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from openinvest.utils.symbols import safe_symbol
from openinvest.skill_cmds._helpers import _print_json
from openinvest.paths import INVEST_ROOT

# 本模块需自有 ROOT：cmd_run_committee 在全局读 ROOT 拼 transcript_path
ROOT = INVEST_ROOT

__all__ = [
    "cmd_autopilot_daily",
    "cmd_daily_report",
    "cmd_ibkr_autopilot",
    "cmd_prepare_committee",
    "cmd_run_committee",
    "cmd_save_committee",
]


def cmd_daily_report(_: argparse.Namespace) -> None:
    """完整日报管道（多资产委员会 + Gemini 第二意见 + 翻译官 + 纪律台账），
    stdout 输出与邮件正文同源的 markdown（assemble_full_report）——宿主 agent
    侧 cron 原样投递用（Hermes `--no-agent --script` / OpenClaw cron）。不发邮件。
    熔断 / no_target_assets 早返回时无报告可发 → 输出结构化 JSON，让 cron
    投递的是失败原因而不是空白。"""
    from openinvest.jobs.daily_report import run

    result = run(send_email=False, include_report=True)
    report = result.get("full_report")
    if report:
        # cli.main 把 sys.stdout 重定向到了 stderr（防 utils noise），真输出走 __stdout__
        real_stdout = getattr(sys, "__stdout__", sys.stdout)
        real_stdout.write(report if report.endswith("\n") else report + "\n")
        real_stdout.flush()
    else:
        _print_json(result)


def cmd_autopilot_daily(_: argparse.Namespace) -> None:
    """模拟账户一站式日流程（给没有 scheduler 的第二个 INVEST_HOME 用 cron 跑，experiments/paper_autopilot/）：
    daily_report（不发邮件）→ paper_autopilot（按当日裁决模拟成交）→ verdict_review（复盘）。
    三段各自隔离失败（一段抛异常不挡后面），输出 JSON；有任何一段抛异常则退出码 1。
    autopilot.enabled 关着时第二段是 no-op。"""
    from openinvest.jobs import daily_report, paper_autopilot, verdict_review

    out, failed = {}, False
    for name, fn in (("daily_report", lambda: daily_report.run(send_email=False)),
                     ("paper_autopilot", paper_autopilot.run),
                     ("verdict_review", verdict_review.run)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            failed = True
    _print_json(out)
    if failed:
        sys.exit(1)


def cmd_ibkr_autopilot(args: argparse.Namespace) -> None:
    """盈透实盘 autopilot（jobs/ibkr_autopilot.py，**下真单**；ibkr.enabled 关着时全是 no-op，dry_run 时只试算）。
    phase=pick / decide / submit / reconcile 单跑一段；status / news / brief 只读；committee = daily_report（不发邮件）→
    verdict_review；daily = pick（周一或名单为空）→ daily_report → decide（LLM）→ submit → verdict_review。
    常驻会话驱动时用 brief 看上下文、pick --picks / decide --orders 交自己写的 JSON（同一套校验），--batch 给盘中追加批次。
    输出 JSON；任一段 error / halted 或抛异常 → 邮件 + 退出码 1。"""
    from openinvest.jobs import ibkr_autopilot

    syms = [x.strip().upper() for x in (args.symbols or "").split(",") if x.strip()] or None
    # 常驻会话交 JSON：优先读文件（--orders-file / --picks-file），免得在 shell 里拼引号
    if args.orders_file:
        args.orders = Path(args.orders_file).read_text(encoding="utf-8")
    if args.picks_file:
        args.picks = Path(args.picks_file).read_text(encoding="utf-8")
    stages = {
        "pick": [("pick", lambda: ibkr_autopilot.pick(force=args.force, picks_json=args.picks))],
        "decide": [("decide", lambda: ibkr_autopilot.decide(orders_json=args.orders, batch=args.batch))],
        "submit": [("submit", lambda: ibkr_autopilot.submit(batch=args.batch))],
        "reconcile": [("reconcile", ibkr_autopilot.reconcile)],
        "status": [("status", ibkr_autopilot.status)],
        "news": [("news", lambda: ibkr_autopilot.news(syms, days=args.days, article=args.article))],
        "news_check": [("news_check", ibkr_autopilot.news_check)],
        "brief": [("brief", lambda: ibkr_autopilot.brief(args.kind))],
        "convert": [("convert", lambda: ibkr_autopilot.convert(args.from_ccy or "", args.to_ccy or "", args.amount))],
    }
    if args.phase in ("daily", "committee"):
        from openinvest.jobs import daily_report, verdict_review
        stages["committee"] = [("daily_report", lambda: daily_report.run(send_email=False)),
                               ("verdict_review", verdict_review.run)]
        stages["daily"] = [("pick", lambda: ibkr_autopilot.pick(force=args.force)),
                           ("daily_report", lambda: daily_report.run(send_email=False)),
                           ("decide", ibkr_autopilot.decide),
                           ("submit", ibkr_autopilot.submit),
                           ("verdict_review", verdict_review.run)]
    out = {}
    for name, fn in stages[args.phase]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    _print_json(out)
    # 返回 error / halted（账户不符、STOP、选股全废……）也算失败：cron 只看退出码，没人盯日志 → 发邮件 + 退出码 1
    problems = {n: r for n, r in out.items() if isinstance(r, dict) and r.get("status") in ("error", "halted")}
    if problems:
        ibkr_autopilot.notify_failure(args.phase, problems)
        sys.exit(1)


# ---------- prepare_debate ----------

# _gather_relevant_insights 已移到 core/committee_runner.py:load_prior_insights
# 作为 shared loader (2026-05-16 三路径统一; 三 entry 不再各自实现一份)


def cmd_prepare_committee(args: argparse.Namespace) -> None:
    """输出 Investment Committee brief — 含项目原生 prompt + 用户上下文，给 Claude 扮演 4 角色

    计算体在 core/committee_runner.py:prepare_committee_brief（与
    POST /api/committee/prepare 共享，coordinator 路径的 prep 不再由 entry 手搓）
    """
    from openinvest.core.committee_runner import prepare_committee_brief
    _print_json(prepare_committee_brief(args.symbol))


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

    from openinvest.core.committee_runner import run_committee_session
    from openinvest.core.portfolio_manager import PortfolioManager

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

    # Stage 0：同日检查（Skill-only 行为，必须在调 session 之前）
    today = datetime.now().strftime("%Y-%m-%d")
    # 注意：backend core/committee.py:_persist_committee_to_memory 用 re.sub
    # 把 symbol 里的非 alnum 字符替换成 _（如 "GC=F" → "GC_F.md"）。这里必须
    # 用同款转换，否则 transcript_path.exists() 永远返回 False，cmd_run_committee
    # 输出的 transcript_path 字段就是空字符串（Fresh Claude 端到端测试发现）
    safe_sym = safe_symbol(args.symbol)
    transcript_path = ROOT / "memory" / ".committee" / today / f"{safe_sym}.md"
    from openinvest.core.decision_ledger import is_unclear, llm_failure_reason, parse_committee_file
    cached_parsed = parse_committee_file(transcript_path) if transcript_path.exists() else None
    # CIO 没出裁决（UNCLEAR）的记录不当缓存，直接重跑；部分角色失败的照常命中但带 degraded
    if transcript_path.exists() and not args.force and not is_unclear(cached_parsed):
        from openinvest.capabilities.committee.i18n import bilingual
        from openinvest.jobs.review_calc import confidence_display
        from openinvest.jobs.verdict_review import load_confidence_lookup
        parsed = cached_parsed
        _print_json({
            "status": "cached",
            "reason": bilingual(
                "今天已经跑过这个资产了；用 --force 重跑",
                "This asset already ran today; pass --force to rerun.",
            ),
            "transcript_path": str(transcript_path),
            "transcript_md": transcript_path.read_text(encoding="utf-8"),
            # 与 fresh 路径 / MCP run_committee cached 同款
            "confidence_lookup": (confidence_display(parsed, load_confidence_lookup(), with_raw=False)
                                  if parsed else None),
            **({"degraded": llm_failure_reason(parsed)} if llm_failure_reason(parsed) else {}),
        })
        return

    # LLM key 闸放在同日 cache 之后：命中缓存只读 transcript，不调 LLM，没 key 也该能读
    # LLM_API_KEY（通用）或 DEEPSEEK_API_KEY（向后兼容）都接受
    if not (os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
        _print_json({
            "status": "error",
            "error": "LLM_API_KEY / DEEPSEEK_API_KEY 未设。Direct 路径必须有 LLM key。",
            "hint": (
                "走 Coordinator 路径（在 Claude Code 里用 prepare_committee + spawn"
                " subagent）不需要 key。或在 .env 里加 LLM_API_KEY（推荐）或"
                " DEEPSEEK_API_KEY 后重试。"
            ),
        })
        sys.exit(1)

    # 三路径统一架构：所有 prep（macro/wealth/event_brief/regime/portfolio_summary/
    # prior_insights）全部委托给 run_committee_session 一处实现，跟 Web/Cron 对齐。
    # 修复 2026-05-16 漂移: 历史上 Skill 直接调 run_committee，自己手搓 prep,
    # 没传 event_brief 给 macro / 没用 multi 召回 / portfolio_summary 是简化版
    session = run_committee_session(
        symbols=[args.symbol],
        max_debate_rounds=args.max_rounds,
        progress_callback=None,  # CLI 不需要 SSE 进度
    )

    asset_result = session["asset_committees"].get(args.symbol, {})
    if "error" in asset_result:
        _print_json({
            "status": "error",
            "error": asset_result["error"],
            "hint": "session 内单资产失败，检查行情数据 / DEEPSEEK_API_KEY",
        })
        sys.exit(1)

    verdict = asset_result.get("verdict", {})
    failure = llm_failure_reason(
        parse_committee_file(transcript_path)
        or {"verdict": str((verdict or {}).get("verdict") or "").upper()})
    if failure and str((verdict or {}).get("verdict") or "").upper() == "UNCLEAR":
        _print_json({
            "status": "error",
            "error": f"委员会没跑成：{failure}",
            "hint": "多半是 LLM 调用失败（key / 额度 / 参数），先跑 doctor；失败记录已存档但不会当缓存。",
            "transcript_path": str(transcript_path) if transcript_path.exists() else "",
        })
        sys.exit(1)
    report = asset_result.get("report")
    cio_memo = report.cio_memo if report is not None else ""
    from openinvest.capabilities.committee.i18n import get_invest_lang
    lang = get_invest_lang()

    # GUI/NapCat 已退役（2026-07-05）——登记入口统一为 CLI 子命令 / MCP 工具
    if lang == "en":
        cio_render_hint = (
            "⚠️ The `cio_memo` field is a Markdown string. Render it directly as Markdown instead of printing the full JSON blob."
        )
        next_step = (
            f"{cio_render_hint}\n\n"
            "A verdict has been generated. If the user agrees, follow these three steps:\n"
            "1) The user opens their broker or banking app and places the real order using the verdict's alloc_cny amount (in the currency named by alloc_currency, CNY if absent) and symbol "
            "(openInvest does not connect to exchanges; it only produces decisions)\n"
            "2) Come back and record the trade with the CLI `buy`/`sell` subcommands or the MCP `buy`/`sell` tools\n"
            "3) Use `record_execution` to link the decision to the execution outcome (record rejections too, with a reason)\n\n"
            "**Do not write to memory/ directly.** All state changes must go through audited entry points."
        )
    else:
        cio_render_hint = (
            "⚠️ `cio_memo` 字段是 Markdown 字符串（含 `## verdict` `**confidence**` 等格式），"
            "**直接当 Markdown 渲染给用户看**，不要把整个 JSON 原样打印。"
        )
        next_step = (
            f"{cio_render_hint}\n\n"
            "已生成 verdict。如果用户同意，按三步走：\n"
            "1) 用户打开自己的证券/银行 App，按 verdict 的 alloc_cny 金额（币种见 alloc_currency，缺省 CNY）+ symbol 真实下单"
            "（openInvest 不接交易所，只做决策）\n"
            "2) 回来登记这笔：CLI `buy`/`sell` 子命令或 MCP `buy`/`sell` 工具\n"
            "3) 用 `record_execution` 关联决议（拒绝执行也记，附原因）\n\n"
            "**不要直接写 memory/**——所有状态变更必须走带审计的入口。"
        )

    from openinvest.jobs.review_calc import confidence_display
    from openinvest.jobs.verdict_review import load_confidence_lookup
    _print_json({
        "status": "ok",
        "asset": target,
        "verdict": verdict,
        # 给用户看这个，不是 verdict.confidence（CIO 自报，只留档）——D10 P1
        "confidence_lookup": confidence_display(verdict, load_confidence_lookup(), with_raw=False),
        "cio_memo": cio_memo,
        "transcript_path": str(transcript_path) if transcript_path.exists() else "",
        "next_step": next_step,
        # 有角色没产出、CIO 被强制 HOLD：照给，但要让 agent 看见并转告用户
        **({"degraded": failure} if failure else {}),
    })


def cmd_save_committee(args: argparse.Namespace) -> None:
    """读 stdin 上来的 transcript，落到 memory/.committee/<date>/<asset>.md

    解析/落盘在 core/committee_runner.py:save_committee_transcript（与
    POST /api/committee/save 共享）
    """
    raw = sys.stdin.read()
    if not raw.strip():
        _print_json({"status": "error", "error": "empty stdin"})
        sys.exit(1)
    from openinvest.core.committee_runner import save_committee_transcript
    provider = getattr(args, "provider", None) or "claude"
    model = getattr(args, "model", None) or ""
    try:
        _print_json(save_committee_transcript(args.symbol, raw, provider=provider, model=model))
    except ValueError as e:   # CIO 解析不出裁决：不存档，退出码 1 让 agent 看见并重交
        _print_json({"status": "error", "error": str(e)})
        sys.exit(1)
