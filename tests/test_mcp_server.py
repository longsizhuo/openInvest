"""MCP adapter 契约测试 —— 工具集是封闭集合，schema 可生成（不 spawn 子进程）。"""
from __future__ import annotations

import asyncio

EXPECTED_TOOLS = {
    "status", "strategy", "history", "live_prices", "what_if", "discipline",
    "decisions", "explain_decision", "record_execution",
    "buy", "sell", "deposit", "withdraw", "run_committee",
}


def test_tool_set_is_closed():
    """工具名集合精确快照——加/删工具必须有意识地改这里（防 adapter 无序膨胀）。"""
    from openinvest.connectors.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    # 每个工具都有非空描述（MCP client 靠它选工具）+ 合法 inputSchema
    for t in tools:
        assert t.description and t.inputSchema.get("type") == "object", t.name


def test_error_paths_return_dict_not_raise():
    """错误输入返回 {"error"/"status": ...} 而不是抛异常（MCP 协议层不该收到 traceback）。"""
    from openinvest.connectors.mcp_server import (
        explain_decision, record_execution, sell, withdraw,
    )
    assert "error" in explain_decision("no-slash")
    assert "error" in record_execution("no-slash", True)
    assert sell("GC=F", units=-1, price=1)["status"] == "error"
    assert withdraw("CNY", amount=0)["status"] == "error"
