"""Equibles 可选数据源：A 股 / 港股的最近一期利润表 + 业绩电话会摘要

Yahoo Finance 仍是默认。只有设了 EQUIBLES_API_KEY 才启用——每个用户自己去
https://equibles.com/dashboard/apikeys 免费注册拿 key（免费档 100 次/天，REST 与 MCP
共用额度，00:00 UTC 重置）。没设 key → 一律返回 ""，委员会输入与之前逐字相同。

为什么走 MCP 而不是 REST：Equibles 的 REST 公司数据端点只认美股 ticker，非美上市
（MIC:TICKER，如 XHKG:00005）的财报 / 电话会只有 MCP 工具读得到
（https://equibles.com/docs/mcp/tools#conventions）。服务端无状态，一次 tools/call
POST 即可，不需要 initialize 握手。

署名：Equibles 条款要求数据旁边标明来源并链接 equibles.com，所以输出块末尾固定带
"Data: Equibles - https://equibles.com"。别删。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

MCP_URL = "https://mcp.equibles.com/mcp"
TIMEOUT_S = 15
MAX_CHARS = 3000  # 单个工具输出上限，防 prompt 膨胀
ATTRIBUTION = "Data: Equibles - https://equibles.com"

# yfinance 后缀 → ISO 10383 MIC。北交所（.BJ）MIC 未实测，暂不映射。
_SUFFIX_MIC = {"HK": "XHKG", "SS": "XSHG", "SZ": "XSHE"}
_SYMBOL_RE = re.compile(r"^(\d+)\.(HK|SS|SZ)$", re.I)


def to_mic(symbol: str) -> Optional[str]:
    """yfinance symbol → Equibles MIC:TICKER；不是 A 股 / 港股返回 None。"""
    m = _SYMBOL_RE.match(symbol.strip())
    if not m:
        return None
    code, suffix = m.group(1), m.group(2).upper()
    if suffix == "HK":
        code = code.lstrip("0").zfill(5)  # yfinance 0005.HK → HKEX 00005
    return f"{_SUFFIX_MIC[suffix]}:{code}"


def call_tool(name: str, arguments: dict, api_key: str) -> str:
    """一次无状态 MCP tools/call，返回工具文本；HTTP / 协议 / 工具错误都抛异常。"""
    r = requests.post(
        MCP_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"Authorization": f"Bearer {api_key}",
                 "Accept": "application/json, text/event-stream"},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    # streamable HTTP：SSE 回包时 JSON 在 "data:" 行，否则整个 body 就是 JSON
    body = next((ln[5:] for ln in r.text.splitlines() if ln.startswith("data:")), r.text)
    msg = json.loads(body)
    if "error" in msg:
        raise RuntimeError(f"jsonrpc error: {msg['error']}")
    result = msg["result"]
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise RuntimeError(f"tool error: {text[:200]}")
    return text.strip()


def build_fundamentals_brief(symbol: str) -> str:
    """A 股 / 港股：最近一期利润表 + 最近一次业绩电话会摘要。

    无 key / 非 A 港股 / 两个工具都失败 → ""（graceful loader 契约，可选源绝不阻断委员会）。
    """
    api_key = os.getenv("EQUIBLES_API_KEY", "").strip()
    mic = to_mic(symbol)
    if not api_key or not mic:
        return ""

    # ponytail: 每个 A/港股标的每次委员会 2 次调用、不缓存。免费档 100 次/天够日常用；
    # 标的多到吃紧再加按天的文件缓存（财报一季度才变一次）。
    parts = []
    for name, args in (
        ("GetFinancialStatement", {"ticker": mic, "statement": "income"}),
        ("GetEarningsBrief", {"ticker": mic, "limit": 1}),
    ):
        try:
            text = call_tool(name, args, api_key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"equibles {name} {mic} graceful 跳过: {type(e).__name__}: {e}")
            continue
        if text:
            parts.append(text[:MAX_CHARS])
    if not parts:
        return ""

    from openinvest.capabilities.committee.i18n import bilingual
    header = bilingual(
        f"EQUIBLES_FUNDAMENTALS ({mic}): 最近一期利润表（交易所/监管披露）+ 业绩电话会摘要"
        "（Equibles 生成的 AI 摘要，当观点不当事实）",
        f"EQUIBLES_FUNDAMENTALS ({mic}): latest income statement (exchange/regulator filings) "
        "+ earnings-call brief (AI summary by Equibles: treat as opinion, not fact)",
    )
    return "\n\n".join([header, *parts, ATTRIBUTION])


__all__ = ["build_fundamentals_brief", "call_tool", "to_mic"]
