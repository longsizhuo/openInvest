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

from core.memory_store import MemoryStore

from scripts.skill_cmds._helpers import _print_json

# 本模块必须自有 ROOT：cmd_doctor / cmd_init 在全局读 ROOT，是 test patch 重定向主目标
ROOT = Path(__file__).resolve().parents[2]

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
    from services.skill_views import build_doctor_view
    _print_json(build_doctor_view(ROOT))


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
    description: str,
    api_key: str,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """调 LLM（默认 DeepSeek，可换千问/智谱）把"510300 ETF 3000股 4.2元 + 余额宝 5万"
    这种自然语言转成 v2 持仓 JSON。

    返回 {"cash": {...}, "holdings": [...]}.
    出错时抛异常，让 cmd_init 决定回退策略（不阻塞 onboarding）。

    base_url / model 不传 → 走 utils.llm 默认值（LLM_*，fallback DEEPSEEK_*）
    """
    from openai import OpenAI
    from utils.llm import get_llm_config_safe, needs_thinking_disabled
    _ak, _bu, _m, _p = get_llm_config_safe()
    base_url = base_url or _bu
    model = model or _m

    client = OpenAI(api_key=api_key, base_url=base_url)
    # DeepSeek v4 默认 thinking 模式，关掉走旧 chat 行为；千问/智谱不需要
    extra_body = {}
    if needs_thinking_disabled(model):
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
                holdings_parse_note = f"LLM parse failed ({exc!s}); fell back to v1 fields"
        else:
            holdings_parse_note = (
                "holdings_description 提供了，但 LLM_API_KEY / DEEPSEEK_API_KEY 缺失 —— "
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
    # LLM_API_KEY 或 DEEPSEEK_API_KEY 都算"配齐了"
    _has_llm_key = bool(
        env_data.get("LLM_API_KEY") or env_data.get("DEEPSEEK_API_KEY")
    )
    final_checks_status = "completed_full" if (
        _has_llm_key and env_data.get("EMAIL_SENDER")
    ) else "completed_partial"

    # ---------- next_step 话术组装 ----------
    # 优先级（从高到低）：
    #   1. holdings_description 给了但 key 缺失（v1 fallback）→ 强制降级话术（必说）
    #   2. LLM 解析成功 → 让用户确认解析结果
    #   3. completed_full → 正常 onboarding 完成话术
    #   4. completed_partial（无 holdings_description 场景）→ 告知凭据不完整
    _holdings_desc_given_no_key = (
        bool(holdings_text)
        and not env_data.get("LLM_API_KEY", "").strip()
        and not env_data.get("DEEPSEEK_API_KEY", "").strip()
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
