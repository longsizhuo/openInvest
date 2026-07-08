from __future__ import annotations

from openinvest.core.config import load_config


def get_invest_lang() -> str:
    """归一化委员会产物语言；未知值回退英文。"""
    raw = str(load_config().language.invest_lang or "").strip().lower()
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


def localize_prompt_output_requirements(prompt: str) -> str:
    """移除模板里写死的中文回复要求，避免覆盖运行时 locale 指令。"""
    lines = []
    for line in prompt.splitlines():
        if "必须中文回复" in line:
            continue
        lines.append(line)
    return "\n".join(lines)
