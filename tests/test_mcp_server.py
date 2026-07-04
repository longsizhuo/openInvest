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


READONLY_TOOLS = {
    "status", "strategy", "history", "live_prices", "what_if", "discipline",
    "decisions", "explain_decision",
}
MONEY_TOOLS = {"buy", "sell", "deposit", "withdraw"}


def test_tool_annotations():
    """危险等级标注精确契约：读工具 readOnlyHint、动钱工具 destructiveHint，
    一个都不能漏——client 的确认弹窗策略全靠这个（issue #133 merge 后差距 #1）。"""
    from openinvest.connectors.mcp_server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name, t in tools.items():
        ann = t.annotations
        assert ann is not None, f"{name} 缺 annotations"
        if name in READONLY_TOOLS:
            assert ann.readOnlyHint is True, name
        elif name in MONEY_TOOLS:
            assert ann.readOnlyHint is False and ann.destructiveHint is True, name
        else:  # record_execution / run_committee：写但幂等（append 账本 / 当日缓存）
            assert ann.readOnlyHint is False and ann.destructiveHint is False \
                and ann.idempotentHint is True, name


def test_error_paths_return_dict_not_raise():
    """错误输入返回 {"error"/"status": ...} 而不是抛异常（MCP 协议层不该收到 traceback）。"""
    from openinvest.connectors.mcp_server import (
        explain_decision, record_execution, sell, withdraw,
    )
    assert "error" in explain_decision("no-slash")
    assert "error" in record_execution("no-slash", True)
    assert sell("GC=F", units=-1, price=1)["status"] == "error"
    assert withdraw("CNY", amount=0)["status"] == "error"
