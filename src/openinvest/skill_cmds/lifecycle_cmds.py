"""lifecycle_cmds —— onboarding / 健康自检类 skill 子命令

逐字搬运自 scripts/skill.py：
- cmd_doctor：健康自检（计算体在 services/skill_views.py:build_doctor_view）。
- _HOLDINGS_PARSE_SYSTEM_PROMPT / _parse_holdings_with_llm：自然语言持仓 → v2 JSON。
- _write_v2_portfolio：把解析结果覆盖写 memory/portfolio.md（含 2026-05-10 事故防御）。
- cmd_init：交互式 / 半交互式 onboarding 入口。
- _interactive_prompt：CLI 直接 init 的交互输入。

本模块**必须自有 ROOT** —— cmd_doctor / cmd_init 在模块全局读 ROOT，是 test patch
重定向的主目标（patch scripts.skill.ROOT 不再生效，须 patch 本模块的 ROOT）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openinvest.core.memory_store import MemoryStore

from openinvest.services.skill_views import INIT_PAYLOAD_SHAPE
from openinvest.skill_cmds._helpers import _print_json

# 本模块必须自有 ROOT：cmd_doctor / cmd_init 在全局读 ROOT，是 test patch 重定向主目标
from openinvest.paths import INVEST_ROOT
ROOT = INVEST_ROOT

__all__ = [
    "cmd_doctor",
    "_HOLDINGS_PARSE_SYSTEM_PROMPT",
    "_parse_holdings_with_llm",
    "_write_v2_portfolio",
    "cmd_init",
    "_interactive_prompt",
]


# ---------- doctor ----------

def cmd_doctor(_: argparse.Namespace) -> None:
    """健康自检：onboarding 是否完成？所有外部依赖可达？

    给 Claude 看的 JSON：每一项是 ok / missing / unreachable，附 hint 教 Claude
    怎么修。计算体在 services/skill_views.py:build_doctor_view（与 /api/doctor 共享）。
    ROOT 以参数传入——tests/test_onboarding_smoke.py patch scripts.skill.ROOT。
    """
    from openinvest.services.skill_views import build_doctor_view
    _print_json(build_doctor_view(ROOT))


# ---------- init ----------

# 自然语言/CSV 持仓解析已抽到 services/holdings_import.py（单一可信源，onboarding +
# Web API /api/holdings/import + CLI `import` 共用，防 prompt/解析漂移）。这里 re-export
# 保持 cmd_init 的 bare-name 调用 + 历史 monkeypatch(tests/test_skill_init_downgrade)命中。
from openinvest.services.holdings_import import (  # noqa: E402
    _HOLDINGS_PARSE_SYSTEM_PROMPT,
    _parse_holdings_with_llm,
)


def _write_v2_portfolio(
    cash: Dict[str, float], holdings: List[Dict[str, Any]], *, fresh: bool = False,
) -> None:
    """把 LLM 解析出的 v2 schema 直接覆盖写 memory/portfolio.md。

    在 migrate_profile.py 跑完之后调用 —— migrate 写的是 v1 兜底 portfolio.md，
    这里把它替换成包含完整 holdings list 的 v2 版本。

    **2026-05-10 事故防御**：之前测试调 cmd_init 把作者真实持仓覆盖成 fixture
    的 cash 5000 + 空 holdings → 数据丢了。现在加 safety guard：
      1. 如果已有 portfolio.md 含真实持仓（cash 任一币种 > 0 或 holdings 非空），
         **拒绝覆盖**并抛 RuntimeError，让调用方明确传 force=True
      2. 任何成功覆盖前都先备份到 portfolio.md.bak.<timestamp>，事故可恢复

    fresh=True：portfolio.md 不是要保护的真实数据（init 前不存在，或 --force 时仍是 init 兜底），
    由 cmd_init 判定，见 _untouched_init_fallback。fresh 只跳过拒绝，不跳过备份。
    """
    store = MemoryStore()
    # 先备份（不论 fresh 与否）：任何覆盖/拒绝前现有 portfolio.md 都留一份，事故可恢复
    current_path = store.path_of("portfolio")
    backup_path = store.root / f"portfolio.md.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if current_path.exists():
        backup_path.write_bytes(current_path.read_bytes())
    # Safety guard：检查现有 portfolio.md 是否已含真实数据
    existing = None if fresh else store.read("portfolio")
    if existing is not None:
        existing_cash = existing.get("cash") or {}
        existing_holdings = existing.get("holdings") or []
        has_real_data = (
            any(float(v or 0) > 0 for v in existing_cash.values())
            or len(existing_holdings) > 0
        )
        if has_real_data:
            raise RuntimeError(
                f"⚠️ portfolio.md 已含真实数据（cash={existing_cash}, "
                f"{len(existing_holdings)} 条 holdings），拒绝被 cmd_init 覆盖。"
                f"已备份当前到 {backup_path.name}。缺的仓位用 `buy --existing-position` "
                f"逐只补录（不扣现金）。"
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
        "由 onboarding 写入。之后通过 CLI `buy`/`sell` 或 MCP 工具调整，"
        "不要手动编辑 frontmatter。",
    ]
    store.write("portfolio", "state", portfolio_data, "\n".join(body_lines) + "\n")


def _untouched_init_fallback() -> bool:
    """portfolio.md 仍是上次 init 写的纯现金兜底：无 holdings 且 portfolio_history 无记录
    （buy/sell/deposit 和 Web API 写入都会记流水）。读不出来按"有真实数据"处理。"""
    store = MemoryStore()
    try:
        return not (store.read("portfolio") or {}).get("holdings") and not store.read_history()
    except Exception:  # noqa: BLE001 坏文件 → 保护，不覆盖
        return False


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
        "holdings_description": "<自然语言持仓描述，让后端 LLM 解析>",
        "current_assets": {"cash_cny": 0, "aud_cash": 0},   # 无 LLM key 时唯一落库的现金
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

    # 扁平 payload（没包 profile）以前会被静默吃掉：name→Anonymous、现金 0、key 丢，还回 ok（#191）
    if not isinstance(payload, dict) or not isinstance(payload.get("profile"), dict):
        _print_json({
            "status": "error",
            "error": "payload 必须是 {\"profile\": {...}, \"env\": {...}}；顶层缺 \"profile\" 对象",
            "got_top_level_keys": sorted(payload) if isinstance(payload, dict) else type(payload).__name__,
            "expected_shape": INIT_PAYLOAD_SHAPE,
        })
        sys.exit(1)
    profile = payload["profile"]
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

    # 3) 触发 migrate（进程内调用——uvx/wheel 形态数据目录里没有 scripts/，
    # 不能再 subprocess 数据目录下的脚本路径；review #139 finding #1）
    import io
    from types import SimpleNamespace
    _out, _err, _rc = io.StringIO(), io.StringIO(), 0
    # 可覆盖 portfolio.md：init 前不存在（只看路径——坏 YAML 不能把 init 炸成无 JSON），
    # 或 --force 重跑（配 key / LLM 失败后重做）且仍是上次 init 的兜底。有交易流水的照旧拒绝覆盖。
    _portfolio_existed = MemoryStore().path_of("portfolio").exists()
    _portfolio_fresh = not _portfolio_existed or (args.force and _untouched_init_fallback())
    # migrate 只在 user/strategy/portfolio.md 都还没有时跑（新装 / 老 clone 升级）。已有任一个它必然
    # 拒绝，拒绝信息还教 agent 跑 `migrate_profile --force`——那会清空持仓和 target_assets（#172 同症状）。
    _memory_existed = any(MemoryStore().path_of(n).exists() for n in ("user", "strategy", "portfolio"))
    if (ROOT / "user_profile.json").exists() and not _memory_existed:
        try:
            from contextlib import redirect_stderr, redirect_stdout
            from openinvest.migrate_profile import main as _migrate_main
            with redirect_stdout(_out), redirect_stderr(_err):
                _migrate_main()
        except Exception as e:  # noqa: BLE001
            _err.write(f"{type(e).__name__}: {e}")
            _rc = 1
    result = SimpleNamespace(stdout=_out.getvalue(), stderr=_err.getvalue(),
                             returncode=_rc)

    # 3a) --force 重新 onboarding：只把名字/风险偏好合并进已有 user.md（委员会读 risk_tolerance）。
    # strategy/portfolio/流水不动——分配/上限/跟踪走 set_allocations / track_asset。
    profile_note = ""
    _upd = {k: str(profile[src]).strip()
            for src, k in (("name", "display_name"), ("risk_tolerance", "risk_tolerance"))
            if str(profile.get(src) or "").strip()}
    if args.force and _memory_existed and _upd and MemoryStore().path_of("user").exists():
        try:
            import re
            from openinvest.core.schemas import validate_user
            with MemoryStore().transaction("user") as _u:  # 校验抛错 → 不提交
                validate_user({**_u.metadata, **_upd})
                _u.update(**_upd)
                _body = _u.body
                for _label, _k in (("姓名", "display_name"), ("风险偏好", "risk_tolerance")):
                    if _k in _upd:
                        _body = re.sub(rf"(\*\*{_label}\*\*: ).*", lambda m, v=_upd[_k]: m.group(1) + v,
                                       _body, count=1)
                _u.set_body(_body)
            profile_note = "user.md updated: " + ", ".join(f"{k}={v}" for k, v in _upd.items())
        except Exception as exc:  # noqa: BLE001 非法值 / 坏文件：原样保留
            profile_note = f"user.md unchanged: {type(exc).__name__}: {str(exc)[:300]}"
    # 重配时 payload 里的策略不落 strategy.md（有意：不动跟踪列表/上限），要明说，免得 agent 报成"已应用"
    if args.force and _memory_existed and profile.get("investment_strategy"):
        profile_note = ((profile_note + "; ") if profile_note else "") + (
            "strategy.md unchanged — allocations: set_allocations; caps/tracking: track_asset")

    # 3b) v2 持仓覆盖：如果 profile 带了 holdings_description（自然语言）或
    # holdings_v2（结构化），优先用它们生成完整 v2 portfolio.md。这一步在
    # migrate_profile.py 之后跑，结果会覆盖 migrate 写的 v1 兜底版本。
    holdings_v2: Dict[str, Any] = profile.get("holdings_v2") or {}  # 结构化直传
    holdings_text = str(profile.get("holdings_description") or "").strip()
    holdings_parse_note: str = ""
    _v2_write_error = ""
    _v2_written = False
    _parse_failed = False

    if not holdings_v2 and holdings_text:
        # 优先 LLM_API_KEY（通用），兼容 DEEPSEEK_API_KEY（fork 用户老 env）
        api_key = (
            env_data.get("LLM_API_KEY", "").strip()
            or env_data.get("DEEPSEEK_API_KEY", "").strip()
        )
        if api_key:
            try:
                # base_url 同样优先 LLM_BASE_URL，兜底 DEEPSEEK_BASE_URL，再兜底 utils.llm 默认
                base_url = (
                    env_data.get("LLM_BASE_URL")
                    or env_data.get("DEEPSEEK_BASE_URL")
                    or "https://api.deepseek.com"
                )
                holdings_v2 = _parse_holdings_with_llm(
                    holdings_text,
                    api_key=api_key,
                    base_url=base_url,
                )
                holdings_parse_note = "parsed via LLM"
            except Exception as exc:  # noqa: BLE001 LLM 失败不阻塞 onboarding
                _parse_failed = True
                holdings_parse_note = f"LLM parse failed ({exc!s}); fell back to v1 fields"
        else:
            holdings_parse_note = (
                "holdings_description 提供了，但 LLM_API_KEY / DEEPSEEK_API_KEY 缺失 —— "
                "只录了 current_assets 现金。配 key 后跑 init --force 重做（还没补录任何仓位时才会写入）。"
            )

    # 没有解析结果的 --force（无 key / 解析失败）：组合还是没动过的 init 兜底时，把这次
    # current_assets 的现金写进去——migrate 有 run-once 闸不会重跑，否则用户更正的现金不落库。
    if not holdings_v2 and getattr(args, "force", False) and _portfolio_fresh:
        _ca = profile.get("current_assets") or {}
        _force_cash: Dict[str, float] = {}
        for _ccy, _key in (("CNY", "cash_cny"), ("AUD", "aud_cash")):
            try:
                if float(_ca.get(_key) or 0):
                    _force_cash[_ccy] = float(_ca[_key])
            except (TypeError, ValueError):
                pass
        if _force_cash:
            holdings_v2 = {"cash": _force_cash, "holdings": []}

    # 解析只看 holdings_description；用户单独报的现金在 current_assets。覆盖写会整个替换 cash，
    # 所以解析没给（或给 0）的币种用 current_assets 补，否则报了的现金被清空（#191 同症状）。
    # --force 重跑时 migrate 被 run-once 闸跳过，这里也是新 current_assets 唯一落库处。
    # cash 不是 dict（列表/数字/字符串）就不合并，交给下面 try 里的写入校验报 v2 write failed
    if holdings_v2 and isinstance(holdings_v2.get("cash") or {}, dict):
        _ca = profile.get("current_assets") or {}
        _merged_cash = dict(holdings_v2.get("cash") or {})
        for _ccy, _key in (("CNY", "cash_cny"), ("AUD", "aud_cash")):
            try:
                if not float(_merged_cash.get(_ccy) or 0) and float(_ca.get(_key) or 0):
                    _merged_cash[_ccy] = float(_ca[_key])
            except (TypeError, ValueError):
                pass  # 非数字（"5万"）不猜，照解析结果写
        holdings_v2 = {**holdings_v2, "cash": _merged_cash}

    if holdings_v2 and (holdings_v2.get("cash") or holdings_v2.get("holdings")):
        try:
            _write_v2_portfolio(
                holdings_v2.get("cash", {}) or {},
                holdings_v2.get("holdings", []) or [],
                fresh=_portfolio_fresh,
            )
            _v2_written = True
            holdings_parse_note = (holdings_parse_note or "v2 written") + "; portfolio.md overwritten with v2 schema"
        except Exception as exc:  # noqa: BLE001 不阻塞
            _v2_write_error = str(exc)
            holdings_parse_note += f"; v2 write failed: {exc!s}"
    # 写失败时 parsed holdings 只是预览，不能让 agent 当成已入账读给用户确认
    _holdings_written = bool(holdings_v2.get("holdings")) and not _v2_write_error
    # 场外基金净值源全挂 → 0 份入账；点名写进 note，否则 agent 只看到 "parsed via LLM"
    _fund_warnings = list(holdings_v2.get("warnings") or []) if _holdings_written else []
    if _fund_warnings:
        holdings_parse_note += "; " + "; ".join(_fund_warnings)
    _holdings_desc_given_no_key = (
        bool(holdings_text)
        and not env_data.get("LLM_API_KEY", "").strip()
        and not env_data.get("DEEPSEEK_API_KEY", "").strip()
    )
    # init 前已有真实组合（有持仓/流水，或不是 init 写的）且这次没写入：现金和持仓都没动。
    # 不能再说"只录了现金"+ 全量补录——status 里已有的会被重复计数。
    _kept = _portfolio_existed and not _portfolio_fresh and not _v2_written
    if _kept and not _v2_write_error:
        _why = holdings_parse_note if _parse_failed else (
            "没有 LLM key，没解析持仓描述" if _holdings_desc_given_no_key else "没有可写入的持仓")
        holdings_parse_note = (   # 保留原始报错（key 失效 / 连不上），agent 才能告诉用户原因
            "existing portfolio left unchanged: 已有 portfolio.md 没动——这次既没写 current_assets 现金，"
            f"也没写持仓（{_why}）"
        )
    elif _kept:
        holdings_parse_note = "existing portfolio left unchanged; " + holdings_parse_note
    # 话术只说这次实际写进去的现金（payload 没给 current_assets 时现金是空的；组合原样保留时这次没写现金）
    _wrote = _v2_written or (not _portfolio_existed and MemoryStore().path_of("portfolio").exists())
    try:
        _cash = ((MemoryStore().read("portfolio") or {}).get("cash") or {}) if _wrote else {}
        _cash_recorded = {k: v for k, v in _cash.items() if v}
    except Exception:  # noqa: BLE001 坏 portfolio.md：init 照样回 JSON
        _cash_recorded = {}
    _cash_text = (
        "已录现金 " + "、".join(f"{k} {v:,.2f}" for k, v in _cash_recorded.items())
        if _cash_recorded else
        "这次没写现金（portfolio.md 原样保留，现金以 `run.sh status` 为准）" if _portfolio_existed and not _wrote
        else "现金没录上（portfolio 里现金为空）——先跑 `run.sh doctor`，"
        "报 portfolio_schema 就按它的命令转换；否则问用户有多少现金，用 `deposit` 记"
    )
    # 已持有仓位的唯一补录 / 更正方式：不扣现金（普通 buy 会从现金里扣）
    _backfill = (
        "用户用系统前就持有的仓位，逐只用 `buy ... --existing-position`"
        "（MCP: `record_existing_position` 工具）补录——不扣现金；别用普通 `buy`，它会从现金里扣。"
    )
    _fix = (
        "某只数量/成本不对：`run.sh delete_holding --symbol X --force` 后用 "
        "`buy ... --existing-position` 按正确数字重录（都不动现金）"
    )

    # 4) 第一次 init 后跑 doctor 让 Claude 知道还差什么
    # LLM_API_KEY 或 DEEPSEEK_API_KEY 都算"配齐了"
    _has_llm_key = bool(
        env_data.get("LLM_API_KEY") or env_data.get("DEEPSEEK_API_KEY")
    )
    final_checks_status = "completed_full" if (
        _has_llm_key and env_data.get("EMAIL_SENDER")
    ) else "completed_partial"

    # ---------- next_step 话术组装 ----------
    # 优先级（从高到低）：
    #   1. v2 写入被拒/失败，或已有组合这次没写入 → 说没写入，只补 status 里缺的（不重复加已有的）
    #   2. holdings_description 给了但 key 缺失 → 强制降级话术（必说；现金按实际落库说）
    #   3. 有 key 但 LLM 解析失败 → 说没写入 + --existing-position 补录
    #   4. LLM 解析成功并写入 → 让用户确认解析结果
    #   5. completed_full → 正常 onboarding 完成话术
    #   6. completed_partial（无 holdings_description 场景）→ 告知凭据不完整
    # 补录已持有仓位一律走 `buy --existing-position`（不扣现金），不再教 deposit+buy
    if _v2_write_error or _kept:
        next_step_text = (
            ("**组合这次没有写入任何东西**——已有的 portfolio.md 保持原样，current_assets 现金和持仓都没写"
             if _kept else "**这次解析的持仓没有写入**——portfolio.md 保持原样")
            + "（原因见 holdings_parse_note）。告诉用户这一点，别把 `parsed_holdings_for_user_review` 当成已记录的持仓。"
            "先跑 `run.sh status` 看已经记了哪些：**status 里已有的 symbol 绝不要再加**（再 buy 会重复计数）；"
            "只把 status 里没有的，" + _backfill + _fix + "。"
        )
    elif _holdings_desc_given_no_key:
        # 强制话术：告知用户持仓仅记了现金，引导去注册 DeepSeek key
        next_step_text = (
            f"你的持仓我暂时按基础模式记录了——{'只录了现金' if _cash_recorded else '还没录任何东西'}，"
            "没识别你说的具体股票。"
            "想让我自动识别 (510300 → 沪深300ETF 那种)，需要一个免费 DeepSeek API key，"
            f"30 秒去 platform.deepseek.com 注册。要不要现在搞定？（agent：{_cash_text}；"
            f"不配 key 的话，{_backfill}）"
        )
    elif _parse_failed:
        # 有 key 但 LLM 解析失败：持仓没写，别落到 completed_full 指去 adding-assets 的普通 buy
        next_step_text = (
            f"**持仓解析失败，没有写入**（见 holdings_parse_note），{_cash_text}。告诉用户这一点。"
            "补持仓二选一：稍后重跑 `run.sh init --force`（portfolio 仍是 init 兜底时才会写入）；"
            "或现在" + _backfill
        )
    elif _holdings_written:
        # LLM 解析成功路径：先让用户确认解析内容
        next_step_text = (
            "**先让用户确认 LLM 解析的持仓**（读 `parsed_holdings_for_user_review` "
            f"字段给他听，连同{_cash_text}）。" + _fix + "；别重跑 `init --force`（持仓已存在，会被拒绝）。"
            "确认无误后，调 `run.sh status` 验证持仓显示正确。"
        )
        if _fund_warnings:
            next_step_text = (
                f"**{len(_fund_warnings)} 只场外基金没换算成份额，按 0 份入账、市值显示 0**"
                "（holdings_parse_note 里 `fund not converted` 逐只点名 + 补救命令）。先告诉用户这一点。"
                + next_step_text
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
            "不烧任何 token；"
            "想让服务器后台每天自动跑，那时候再去 platform.deepseek.com 注册 key 填 .env。"
        )

    if profile_note.startswith("user.md unchanged"):
        next_step_text = (f"**名字/风险偏好没有更新**（{profile_note}）。告诉用户这一点；风险偏好只能是 "
                          "Conservative / Balanced / Aggressive。" + next_step_text)

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
        "user_review_required": _holdings_written,
        "cash_recorded": _cash_recorded,
        "profile_note": profile_note,
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

    # LLM key 先问 —— 决定后面持仓走自然语言还是手动字段
    # 兼容老用户：变量名仍叫 deepseek_key（写到 .env 也仍是 DEEPSEEK_API_KEY），
    # 但提示语已松绑为"任意 OpenAI 兼容 API"
    deepseek_key = ask(
        "LLM API Key (DeepSeek sk-xxx / 千问 sk-xxx / 智谱 xxx，可留空跳过)", "",
    )

    profile: Dict[str, Any] = {
        "name": ask("姓名 / display name", "Anonymous"),
        "risk_tolerance": ask(
            "风险偏好 (Conservative / Balanced / Aggressive)", "Balanced"
        ),
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
            "留空就跳过；之后补已持有的仓位用 `buy ... --existing-position`（不扣现金）。",
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
            "持仓只问现金；已持有的仓位之后用 `buy ... --existing-position` 补（不扣现金）。",
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
