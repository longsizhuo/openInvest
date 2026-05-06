"""LLM tool definitions for the Investment Committee — 让 4 角色 agent 主动查询数据。

不再是"我们 prepare 好 brief 喂进 prompt"那种 pipeline 模式（hack），改为
LLM 主动决定"我需要什么数据 / 看多深"。每个 tool 是 Python 函数，元数据
（OpenAI/DeepSeek 兼容的 function calling schema）由本文件维护。

5 个 tool：
- get_history_data: yfinance 多周期行情
- analyze_multi_timeframe: 多周期技术指标 (RSI/MA/分位数)
- get_macro_snapshot: VIX/TNX/USDCNY/AUDCNY 当前值
- query_dreaming_insights: Dreaming 长期模式（近 90 天行为聚类）
- get_recent_committee_verdicts: 同资产最近 N 次委员会决策

Hybrid 设计：调用方仍然在 user prompt 里塞 baseline brief（保 backup），
但允许 LLM 通过 tool 补充查询。即使 LLM tool call 失败，brief 能保证有
最小可用数据。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent

# OpenAI/DeepSeek 兼容的 function calling schema
TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_history_data",
            "description": (
                "获取指定 symbol 在指定时间窗口的历史日线数据（OHLCV）。"
                "用于深入分析趋势 / 计算自定义指标。返回最近 10 个交易日 + 起始/结束/最高/最低 4 个 anchor。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "yfinance ticker，如 'NDQ.AX', 'GC=F', 'AUDCNY=X', '^VIX'",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["1mo", "3mo", "6mo", "1y", "2y"],
                        "description": "时间窗口，默认 1y",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_multi_timeframe",
            "description": (
                "对指定 symbol 跑多周期技术分析（1mo/3mo/6mo/1y/2y），"
                "返回 RSI(14) / MA20/60/120/250 / 价格分位数 / 趋势方向。"
                "Quant 角色的核心数据来源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "yfinance ticker"},
                    "label": {"type": "string", "description": "可读名（如 'NDQ.AX')"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macro_snapshot",
            "description": (
                "返回当前宏观指标快照：VIX (恐慌指数) / TNX (10 年期国债收益率) / "
                "USDCNY / AUDCNY。Macro Strategist 的核心数据来源。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_dreaming_insights",
            "description": (
                "查询 OpenClaw Dreaming 长期记忆里的相关 insight（近 90 天行为聚类）。"
                "返回 top-k 条已通过阈值门 (score≥0.8 / count≥3) 的模式。"
                "Risk Officer 用来引用'用户 6 个月前类似情境的过度集中持仓'等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_symbol": {
                        "type": "string",
                        "description": "限定相关资产（如 'GC=F'）。空字符串=不限",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回最相关的 N 条，默认 3",
                    },
                },
                "required": ["asset_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_committee_verdicts",
            "description": (
                "查询同一资产最近 N 次委员会决策（VERDICT / CONFIDENCE / ALLOC_CNY）"
                "+ 各角色的 SIGNAL。CIO 用来检查'我上次给 BUY 但现在又给 BUY'是否一致。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_symbol": {"type": "string", "description": "如 'GC=F' / 'NDQ.AX'"},
                    "n": {"type": "integer", "description": "默认 5 条"},
                },
                "required": ["asset_symbol"],
            },
        },
    },
]


# ---------- tool 实现 ----------

def _impl_get_history_data(symbol: str, period: str = "1y") -> Dict[str, Any]:
    """返回压缩的行情快照（不返回完整 dataframe，token 友好）"""
    from utils.exchange_fee import get_history_data
    df = get_history_data(symbol, period)
    if df.empty:
        return {"error": f"no data for {symbol}@{period}", "symbol": symbol}
    closes = df["Close"].tolist()
    return {
        "symbol": symbol,
        "period": period,
        "start_date": str(df.index[0].date()),
        "end_date": str(df.index[-1].date()),
        "first_close": round(float(closes[0]), 4),
        "last_close": round(float(closes[-1]), 4),
        "max_close": round(float(max(closes)), 4),
        "min_close": round(float(min(closes)), 4),
        "n_days": len(closes),
        "recent_10_closes": [round(float(c), 4) for c in closes[-10:]],
        "cumulative_return_pct": round((float(closes[-1]) / float(closes[0]) - 1) * 100, 2),
    }


def _impl_analyze_multi_timeframe(symbol: str, label: Optional[str] = None) -> str:
    from utils.exchange_fee import analyze_multi_timeframe, get_history_data
    df = get_history_data(symbol, "2y")
    return analyze_multi_timeframe(df, label or symbol)


def _impl_get_macro_snapshot() -> Dict[str, Any]:
    from utils.exchange_fee import get_history_data
    out: Dict[str, Any] = {"as_of": datetime.now().isoformat(timespec="seconds")}
    for sym, label in [("^VIX", "vix"), ("^TNX", "tnx"),
                       ("USDCNY=X", "usdcny"), ("AUDCNY=X", "audcny")]:
        df = get_history_data(sym, "5d")
        if not df.empty:
            out[label] = round(float(df["Close"].iloc[-1]), 4)
        else:
            out[label] = None
    return out


def _impl_query_dreaming_insights(asset_symbol: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """从 memory/insights/*.md 读 top-k insight。Dreaming 三阶段产出的就在那里"""
    from core.memory_store import MemoryStore
    store = MemoryStore()
    insights_dir = store.root / "insights"
    if not insights_dir.exists():
        return []
    matches = []
    sym_lower = asset_symbol.lower().replace("=", "_") if asset_symbol else ""
    for f in sorted(insights_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if not asset_symbol or sym_lower in f.stem.lower() or sym_lower in text.lower()[:1000]:
            matches.append({
                "title": f.stem,
                "content_preview": text[:400],
                "path": str(f.relative_to(store.root)),
            })
        if len(matches) >= top_k:
            break
    return matches


def _impl_get_recent_committee_verdicts(asset_symbol: str, n: int = 5) -> List[Dict[str, Any]]:
    """从 memory/.committee/<date>/<symbol>.md 读最近 N 个决议"""
    from core.memory_store import MemoryStore
    import re as _re
    store = MemoryStore()
    committee_dir = store.root / ".committee"
    if not committee_dir.exists():
        return []
    safe_sym = _re.sub(r"[^a-zA-Z0-9_-]", "_", asset_symbol)
    out: List[Dict[str, Any]] = []
    for date_dir in sorted(committee_dir.iterdir(), reverse=True):
        f = date_dir / f"{safe_sym}.md"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        verdict = _re.search(r"\*\*Verdict\*\*:\s*(\w+)", text)
        confidence = _re.search(r"confidence\s+([\d.]+)", text)
        out.append({
            "date": date_dir.name,
            "verdict": verdict.group(1) if verdict else "?",
            "confidence": float(confidence.group(1)) if confidence else 0.0,
            "summary": text[:300],
        })
        if len(out) >= n:
            break
    return out


# ---------- 调度 ----------

TOOL_IMPL = {
    "get_history_data": _impl_get_history_data,
    "analyze_multi_timeframe": _impl_analyze_multi_timeframe,
    "get_macro_snapshot": _impl_get_macro_snapshot,
    "query_dreaming_insights": _impl_query_dreaming_insights,
    "get_recent_committee_verdicts": _impl_get_recent_committee_verdicts,
}


def execute_tool_call(name: str, arguments: Dict[str, Any]) -> str:
    """统一入口：根据 LLM 给的 tool name + args 调对应实现，结果序列化为 JSON 字符串。

    LLM 失败 / 给错参数时返回 error string，不抛异常（让 LLM 看到错误自己 retry）。
    """
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return json.dumps({"error": f"unknown tool: {name}", "available": list(TOOL_IMPL.keys())})
    try:
        result = impl(**arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError as e:
        return json.dumps({"error": f"bad args: {e}", "schema_hint": "see TOOL_DEFINITIONS"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"})


__all__ = ["TOOL_DEFINITIONS", "TOOL_IMPL", "execute_tool_call"]
