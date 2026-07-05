"""LLM 配置 helper —— 支持任意 OpenAI 兼容 API（DeepSeek / 千问 / 智谱 / Kimi 等）

# 背景
原项目硬编码 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL`，导致 fork
用户把 base_url 改成 dashscope（千问）后，model name 还是 `deepseek-v4-flash`，
被千问 API 拒掉 400 "model not found"。

# 新 env 变量（通用）
- `LLM_API_KEY`      —— 兜底读 `DEEPSEEK_API_KEY`
- `LLM_BASE_URL`     —— 兜底读 `DEEPSEEK_BASE_URL`，再默认 `https://api.deepseek.com`
- `LLM_MODEL`        —— 兜底读 `DEEPSEEK_MODEL`，再默认 `deepseek-v4-flash`
- `LLM_PROVIDER`     —— litellm 前缀，默认 `openai`（DeepSeek / 千问 / 智谱都用 openai/* 兼容）

# 向后兼容
所有现有 `DEEPSEEK_*` env 都保留作 fallback，已部署的用户零迁移。
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

# 默认值常量（多处可能引用，集中一处方便审计）
_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_PROVIDER = "openai"


def get_llm_config() -> Tuple[str, str, str, str]:
    """读取 LLM 配置，返回 (api_key, base_url, model, provider)

    优先级:
        1. ``LLM_*`` 系列环境变量（通用，新代码推荐用）
        2. ``DEEPSEEK_*`` 系列（向后兼容，最常见 fork 用户）
        3. 默认 deepseek-v4-flash @ https://api.deepseek.com

    Raises:
        SystemExit: API key 既没 ``LLM_API_KEY`` 也没 ``DEEPSEEK_API_KEY``
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("❌ LLM_API_KEY 或 DEEPSEEK_API_KEY 未设")
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
        or _DEFAULT_BASE_URL
    )
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("DEEPSEEK_MODEL")
        or _DEFAULT_MODEL
    )
    provider = os.getenv("LLM_PROVIDER", _DEFAULT_PROVIDER)
    return api_key, base_url, model, provider


def get_llm_config_safe(
    default_model: str = _DEFAULT_MODEL,
    default_base_url: str = _DEFAULT_BASE_URL,
) -> Tuple[Optional[str], str, str, str]:
    """同 ``get_llm_config()`` 但 api_key 缺失时返回 None 而非抛异常。

    给 doctor / fallback 路径用 —— 它们要"key 没设就跳过"，不能让进程挂。

    Returns:
        (api_key_or_none, base_url, model, provider)
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
        or default_base_url
    )
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("DEEPSEEK_MODEL")
        or default_model
    )
    provider = os.getenv("LLM_PROVIDER", _DEFAULT_PROVIDER)
    return api_key, base_url, model, provider


def supports_json_output(model: Optional[str] = None, base_url: Optional[str] = None) -> bool:
    """是否【尝试】response_format={"type":"json_object"}（DeepSeek JSON Output 是 GA，
    且 json_object 本就是 OpenAI 标准参数，多数兼容 provider 都支持）。

    不做 provider == 硬判定——真不支持的由调用方【运行时优雅回退】兜底（发不出 / 不是
    合法 JSON → 退回文本 + regex，见 core.committee.debate 的 CIO 路径）。这里只给一个
    "默认尝试 + 可关" 的旋钮：INVEST_FORCE_JSON_OUTPUT=0 全关（确认自家 provider 不支持
    又不想吃一次回退的双调用成本时）、=1 强开。缺省尝试。
    """
    # model / base_url: reserved for future provider-specific gating（真不支持 json_object 的
    # provider 一旦确认，可在此按 model/base_url 关掉，省一次回退的双调用成本）。当前只读全局旋钮。
    _ = (model, base_url)
    force = os.getenv("INVEST_FORCE_JSON_OUTPUT")
    if force is not None:
        return force == "1"
    return True


def get_dspy_lm(temperature: float = 0.2, max_tokens: int = 1500):
    """返回配置好的 ``dspy.LM`` 实例（DSPy 训练 / sandbox 用）

    litellm 期待 ``provider/model`` 格式。base_url 自动补 ``/v1`` 后缀。
    """
    import dspy
    api_key, base_url, model, provider = get_llm_config()
    api_base = base_url + "/v1" if not base_url.endswith("/v1") else base_url
    return dspy.LM(
        f"{provider}/{model}",
        api_key=api_key,
        api_base=api_base,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def needs_thinking_disabled(model: Optional[str] = None) -> bool:
    """DeepSeek v4 系列 / MiMo v2.5 系列默认 thinking 模式，
    在 invest committee 这种"已有 4 worker 思考"场景下应 disable。

    背景：
    - DeepSeek-v4-flash / deepseek-chat：thinking enabled by default
    - mimo-v2.5-pro / mimo-v2-pro / mimo-v2-omni：thinking enabled by default
    - mimo-v2-flash：thinking disabled by default（不需要再 disable）
    - 千问 / 智谱 / OpenAI：不支持 thinking extra_body，传了可能报错

    MiMo 的 CIO 等 long-prompt 任务 thinking 会吃完 max_tokens
    导致 content 为空（reasoning_tokens 大头 / content 空字符串）。
    所以对 thinking model 一律 disable 走 fast path。
    """
    m = (model or get_llm_config_safe()[2]).lower()
    deepseek_thinking = ("deepseek" in m and "v4" in m) or m == "deepseek-chat"
    mimo_thinking = m.startswith("mimo") and "flash" not in m
    return deepseek_thinking or mimo_thinking


def get_thinking_disable_kwargs(model: Optional[str] = None) -> dict:
    """返回 disable thinking 需要塞到 chat.completions.create 的 kwargs。

    不同 provider extra_body 格式不同:
    - DeepSeek: ``{"thinking": {"type": "disabled"}}``
    - MiMo:     ``{"thinking": "disabled"}``（string 不是 dict）

    返回空 dict 表示不用 disable（model 本身就不 thinking）。
    """
    m = (model or get_llm_config_safe()[2]).lower()
    if not needs_thinking_disabled(m):
        return {}
    if m.startswith("mimo"):
        return {"extra_body": {"thinking": "disabled"}}
    # DeepSeek 格式
    return {"extra_body": {"thinking": {"type": "disabled"}}}


# NOTE 2026-05-19: 之前有个 needs_reasoning_carry(model) helper 按 model 名嗅探
# 是否要 carry reasoning_content，是 hack。现在 SDKAgent 改成 response-driven：
# 上一轮 API response 含 reasoning_content 就 carry，不靠 model name —— MiMo /
# DeepSeek-R1 / 未来 reasoning model 自动 work；千问/智谱/GPT 不返回该字段则零侵入。
