"""Prompt 模板加载器 —— 跟 OpenClaw / Hermes-Agent 一致的 prompt 组织模式

为什么
======
之前 prompt 全在 .py 里写成 f-string，DSPy 优化要 wrap。改成 .md 后：
- prompt 跟代码解耦
- 非 dev 能改（只改 markdown）
- DSPy/GEPA 直接读写 .md 文件（不需要 wrap，参考
  NousResearch/hermes-agent-self-evolution:evolution/skills/skill_module.py）
- 版本控制清楚（commit diff 只显示 prompt 改动）

设计
====
每个 capability 下 `<role>/` 目录自包含 .py + .md：
- <role>.md             ← 主 prompt（opening round 用）
- <role>_<label>.md     ← 可选：其他轮次 prompt（如 rebuttal）

格式：
    ---
    name: <role>
    description: <一句话>
    role: macro|quant|risk|cio
    ---

    <markdown body：纯 prompt 文本>

占位符（运行时替换）：
    {{asset_name}}       → asset.display_name
    {{asset_symbol}}     → asset.symbol
    {{<custom_var>}}     → 调 load_skill(custom_var=...) 时传入

设计原则
========
- **零模板引擎依赖**：纯 str.replace，避免引入 jinja2 等
- **frontmatter 是契约**：name/description/role 必填，让 DSPy 能识别哪个角色
- **fallback 友好**：找不到对应 round_label 的文件回退到 <role>.md
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


CAPABILITIES_ROOT = Path(__file__).parent


def _split_frontmatter(raw: str) -> Tuple[Dict[str, str], str]:
    """解析 YAML-like frontmatter（不依赖 PyYAML，invest 的 schema 已用 ruamel/pydantic
    解析过其他 frontmatter，这里只要简单 key:value 解析就够，因为 SKILL.md
    frontmatter 只有几行）。

    返回 (frontmatter_dict, body)。raw 不含 frontmatter 时 frontmatter_dict 为空。
    """
    if not raw.startswith("---"):
        return {}, raw

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw

    frontmatter_text = parts[1].strip()
    body = parts[2].lstrip("\n")

    meta: Dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("'\"")

    return meta, body


def _render_placeholders(body: str, variables: Dict[str, Any]) -> str:
    """渲染 {{var}} 占位符。未提供的变量会原样保留（不抛错）"""
    for key, value in variables.items():
        body = body.replace("{{" + key + "}}", str(value))
    return body


def apply_lens_blocks(prompt: str, name: str, enabled: bool) -> str:
    """模板里 `<!-- lens:<name> -->` … `<!-- /lens:<name> -->` 包住只在该 lens 开启时成立的规则：
    开 → 去掉标记、内容原样保留；关 → 整块删掉。

    2026-10-11：集中度 lens 关时只在 prompt 前加"忽略下方集中度规则"，模型仍按模板的
    CONCENTRATION_PCT 字段和 PWM 标准自算占比、CIO 引用——规则留在模板里就挡不住。
    """
    tag = re.escape(name)
    pattern = re.compile(rf"<!-- lens:{tag} -->\n?(.*?)<!-- /lens:{tag} -->\n?", re.S)
    return pattern.sub(lambda m: m.group(1) if enabled else "", prompt)


def load_skill(
    role: str,
    round_label: str = "opening",
    capability: str = "committee",
    **variables: Any,
) -> str:
    """读取并渲染 <role>.md，返回完整 prompt 字符串。

    Args:
        role: 角色目录名，如 "cio" / "macro_strategist" / "quant" / "risk_officer"。
              目录下必须有 <role>.py + <role>.md。
        round_label: 轮次。"opening" 读 <role>.md，其他读 <role>_<round_label>.md
        capability: capability 目录名，默认 "committee"
        **variables: 占位符变量，e.g. asset_name="NDQ.AX", asset_symbol="NDQ.AX"

    Returns:
        渲染好的 prompt 字符串（不含 frontmatter）。

    Raises:
        FileNotFoundError: 角色目录 / <role>.md 不存在
    """
    role_dir = CAPABILITIES_ROOT / capability / role
    if not role_dir.is_dir():
        raise FileNotFoundError(f"角色目录不存在: {role_dir}")

    # 找文件：先尝试 <role>_<round>.md，没有就回退 <role>.md
    candidates = []
    if round_label and round_label != "opening":
        candidates.append(role_dir / f"{role}_{round_label}.md")
    candidates.append(role_dir / f"{role}.md")

    prompt_path: Optional[Path] = None
    for p in candidates:
        if p.exists():
            prompt_path = p
            break

    if prompt_path is None:
        raise FileNotFoundError(
            f"未找到 {role}.md（尝试了 {[str(p) for p in candidates]}）",
        )

    raw = prompt_path.read_text(encoding="utf-8")
    _meta, body = _split_frontmatter(raw)

    # 币种占位符（{{BASE}} / {{DRY_POWDER_LOW}} …）统一在此注入：所有角色、两条路径（Direct /
    # Coordinator）都经这里，新模板用到也不会漏传。调用方显式传的同名变量优先。
    if "{{" in body:
        from openinvest.utils.base_currency import prompt_vars
        from dataclasses import asdict
        from openinvest.core.config import load_config
        from openinvest.core.config.tunable import MarketRulesConfig
        rules = asdict(load_config().market_rules)
        per_asset = rules.pop("per_asset")
        override = per_asset.get(str(variables.get("asset_symbol", "")).upper(), {})
        if set(override) - set(rules):
            raise ValueError("Unknown per-asset market rule")
        rules = asdict(MarketRulesConfig(**{**rules, **override}))
        rule_vars = {"RULE_" + k.upper(): format(v, "g") for k, v in rules.items() if k != "per_asset"}
        currency_vars = prompt_vars() if re.search(r"\{\{(?!RULE_)[A-Z_]+\}\}", body) else {}
        variables = {**currency_vars, **rule_vars, **variables}
    return _render_placeholders(body, variables)


__all__ = ["apply_lens_blocks", "load_skill"]
