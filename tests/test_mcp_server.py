"""MCP adapter 契约测试 —— 工具集是封闭集合，schema 可生成（不 spawn 子进程）。"""
from __future__ import annotations

import asyncio
import inspect

import pytest

EXPECTED_TOOLS = {
    "status", "strategy", "history", "live_prices", "what_if", "discipline",
    "decisions", "explain_decision", "record_execution", "ingest_event",
    "buy", "sell", "deposit", "withdraw", "run_committee",
    # strategy 写操作（issue #179：读写对等）
    "set_allocations", "track_asset", "untrack_asset",
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
# destructive 桶：动钱 4 件 + untrack_asset（删跟踪标的丢配置字段，client 该弹确认）
MONEY_TOOLS = {"buy", "sell", "deposit", "withdraw", "untrack_asset"}


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
        else:  # record_execution / run_committee / ingest_event：写但幂等
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


# ============================================================================
# 顾问模式（INVEST_ADVISORY_MODE）—— 白名单必须是封闭集合
# ============================================================================
# 黑名单式"逐个工具手动加 _check_advisory()"最大的风险是：以后新增工具忘了加，
# 默认对群聊陌生人开放——漏一个就是一个新的持仓泄露洞。这里反过来从源码结构
# 校验：不在 ADVISORY_ALLOWED_TOOLS 白名单里的工具，函数体必须真的调用
# _check_advisory()；白名单里的工具则不应该调（否则顾问模式下核心分析能力被
# 意外堵死）。新增/修改工具时漏加闸，这条测试直接红。

def test_advisory_mode_gate_is_closed_set():
    """白名单机器强制：非白名单工具必须源码里调 _check_advisory()。"""
    from openinvest.connectors import mcp_server as m

    tool_names = EXPECTED_TOOLS
    assert m.ADVISORY_ALLOWED_TOOLS <= tool_names, (
        "ADVISORY_ALLOWED_TOOLS 里有拼错的工具名，不在真实工具集合里"
    )
    for name in tool_names:
        func = getattr(m, name)
        src = inspect.getsource(func)
        calls_guard = "_check_advisory()" in src
        if name in m.ADVISORY_ALLOWED_TOOLS:
            assert not calls_guard, (
                f"{name} 在顾问模式白名单里，不应该调 _check_advisory()"
                "（会把顾问模式仅剩的分析能力也堵死）"
            )
        else:
            assert calls_guard, (
                f"{name} 不在顾问模式白名单里，必须调 _check_advisory() 拒绝顾问模式访问，"
                "否则群聊陌生人能直接拿到真实持仓/账本数据"
            )


def test_advisory_mode_blocks_non_whitelisted_tools(monkeypatch):
    """INVEST_ADVISORY_MODE=1 时，白名单外的工具必须在业务逻辑之前就抛
    RuntimeError——不是返回 {"status": "error"}，是直接拒绝执行，避免任何一条
    分支意外把真实数据算出来又忘了拦。"""
    monkeypatch.setenv("INVEST_ADVISORY_MODE", "1")
    from openinvest.connectors import mcp_server as m

    calls = {
        "status": {}, "history": {}, "strategy": {}, "discipline": {}, "decisions": {},
        "what_if": {"symbol": "GC=F"},
        "buy": {"symbol": "GC=F", "units": 1, "price": 1},
        "sell": {"symbol": "GC=F", "units": 1, "price": 1},
        "deposit": {"currency": "CNY", "amount": 1},
        "withdraw": {"currency": "CNY", "amount": 1},
        "set_allocations": {"target_allocation_stock": 0.7, "target_allocation_cash": 0.3},
        "track_asset": {"symbol": "GC=F"},
        "untrack_asset": {"symbol": "GC=F"},
        "record_execution": {"decision_id": "2026-01-01/GC=F", "executed": True},
    }
    assert set(calls) == EXPECTED_TOOLS - m.ADVISORY_ALLOWED_TOOLS
    for name, kwargs in calls.items():
        with pytest.raises(RuntimeError, match="advisory mode"):
            getattr(m, name)(**kwargs)


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("0", False), ("false", False), ("False", False), ("no", False),
    ("1", True), ("true", True), ("True", True), ("TRUE", True),
])
def test_advisory_mode_truthiness(monkeypatch, raw, expected):
    """INVEST_ADVISORY_MODE=0/false 不能被当成"已开启"——之前 `.strip()` 判非空即真，
    运维手滑写 =0 会意外把顾问模式打开，暴露真实持仓。"""
    monkeypatch.setenv("INVEST_ADVISORY_MODE", raw)
    from openinvest.utils.advisory import is_advisory_mode
    assert is_advisory_mode() is expected


def test_cli_mcp_subcommand_strips_argv(monkeypatch):
    """`openinvest mcp` 分流必须摘掉 "mcp" token——mcp_server.main() 自己 argparse
    （--http），留着会 `unrecognized arguments: mcp`（0.32.0 打挂过 uvx 部署）。"""
    import sys

    from openinvest import cli
    from openinvest.connectors import mcp_server

    seen: dict = {}
    monkeypatch.setattr(mcp_server, "main", lambda: seen.setdefault("argv", list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["openinvest", "mcp"])
    cli.main()
    assert seen["argv"] == ["openinvest"]
