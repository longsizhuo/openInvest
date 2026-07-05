"""capabilities — openInvest 的能力域

每个 capability 是一个自包含的领域包：prompt 模板 + Python 实现 + 文档。
共享基础设施（loader / tools / SDK agent）在此层暴露。
"""
from openinvest.capabilities.loader import load_skill
from openinvest.capabilities.tools import TOOL_DEFINITIONS, execute_tool_call
from openinvest.capabilities.sdk_agent import SDKAgent

__all__ = [
    "load_skill",
    "TOOL_DEFINITIONS", "execute_tool_call",
    "SDKAgent",
]
