from __future__ import annotations

from openinvest.core.config import load_config


def get_invest_lang() -> str:
    """归一化委员会产物语言；未知值回退英文。"""
    raw = str(load_config().language.invest_lang or "").strip().lower()
    if raw == "zh-cn":
        return "zh"
    return raw if raw in {"zh", "en"} else "en"


def build_output_language_directive(*, artifact: str, keep_verbatim_headers: bool = False) -> str:
    """为委员会角色生成统一语言指令。"""
    lang = get_invest_lang()
    if lang == "en":
        directive = f"Produce your {artifact} in English."
    else:
        zh_artifact = {
            "analysis": "分析",
            "analysis memo": "分析备忘",
        }.get(artifact, artifact)
        directive = f"请使用中文输出你的{zh_artifact}。"
    if keep_verbatim_headers:
        directive += (
            " Keep all required section headers, field names, enum values, and parser-facing markers verbatim in English "
            "(for example: VERDICT, CONFIDENCE, DOMINANT_VIEW, SUGGESTED_ALLOC_CNY, TRIM_REASON, REENTRY_PRICE, REENTRY_CONDITION, EXPECTED_PATH)."
        )
    return directive


def build_field_value_language_directive() -> str:
    """约束结构化字段后的自然语言值也必须跟随当前语言。"""
    if get_invest_lang() == "en":
        return (
            "All free-text field values after the fixed English field names must also be in English. "
            "Do not mix Chinese into lines such as ONE_LINER, KEY_DATA, KEY_HEADWIND, KEY_TAILWIND, REASONING, PERSONAL_NOTE, stop_loss_trigger, or recovery_estimate."
        )
    return (
        "所有固定英文字段名后的自然语言内容也必须使用中文。"
        "不要在 ONE_LINER、KEY_DATA、KEY_HEADWIND、KEY_TAILWIND、REASONING、PERSONAL_NOTE、stop_loss_trigger、recovery_estimate 等字段值里混入英文段落。"
    )


def bilingual(zh: str, en: str) -> str:
    """按 INVEST_LANG 二选一，收敛调用方到处复制粘贴的 `if get_invest_lang()=="en": ... else: ...`。"""
    return en if get_invest_lang() == "en" else zh


def localize_prompt_output_requirements(prompt: str) -> str:
    """移除模板里写死的「必须中文回复」，避免覆盖运行时 locale 指令；同一行内其余的
    格式/字数约束（如 quant_rebuttal.md 的「严格按下列格式，≤150 字」）原样保留。"""
    lines = []
    for line in prompt.splitlines():
        if "必须中文回复" not in line:
            lines.append(line)
            continue
        stripped = line.replace("必须中文回复，", "").replace("必须中文回复", "")
        if stripped.strip().strip("-").strip():
            lines.append(stripped)
    return "\n".join(lines)
