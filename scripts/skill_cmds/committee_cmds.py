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
import re
import sys
from datetime import datetime
from pathlib import Path

from scripts.skill_cmds._helpers import _print_json

# 本模块需自有 ROOT：cmd_run_committee 在全局读 ROOT 拼 transcript_path
ROOT = Path(__file__).resolve().parents[2]

__all__ = [
    "cmd_prepare_committee",
    "cmd_run_committee",
    "cmd_save_committee",
]


# ---------- prepare_debate ----------

# _gather_relevant_insights 已移到 core/committee_runner.py:load_prior_insights
# 作为 shared loader (2026-05-16 三路径统一; 三 entry 不再各自实现一份)


def cmd_prepare_committee(args: argparse.Namespace) -> None:
    """输出 Investment Committee brief — 含项目原生 prompt + 用户上下文，给 Claude 扮演 4 角色

    计算体在 core/committee_runner.py:prepare_committee_brief（与
    POST /api/committee/prepare 共享，coordinator 路径的 prep 不再由 entry 手搓）
    """
    from core.committee_runner import prepare_committee_brief
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

    from core.committee_runner import run_committee_session
    from core.portfolio_manager import PortfolioManager

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
    report = asset_result.get("report")
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


def cmd_save_committee(args: argparse.Namespace) -> None:
    """读 stdin 上来的 transcript，落到 memory/.committee/<date>/<asset>.md

    解析/落盘在 core/committee_runner.py:save_committee_transcript（与
    POST /api/committee/save 共享）
    """
    raw = sys.stdin.read()
    if not raw.strip():
        _print_json({"error": "empty stdin"})
        return
    from core.committee_runner import save_committee_transcript
    _print_json(save_committee_transcript(args.symbol, raw))
