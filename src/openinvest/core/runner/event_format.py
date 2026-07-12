"""事件 brief 格式化纯核（域绑定纯模块，ADR-026）

从 core/runner/event_brief.py 拆出：events 列表 → brief 文本（纯拼接）。
头行格式契约与 calc/sentiment._EVENT_HEADER_RE 互解析（改一边必须同步另一边
+ tests/test_event_rag_resolve.py 回归）。EventStore/RAG 召回留在 event_brief.py。
"""
from __future__ import annotations

from typing import Any, Dict, List

def format_event_brief(events: List[Dict[str, Any]]) -> str:
    """事件列表 → Macro prompt 注入用的结构化文本（人类可读 + 时效冲突显式）

    格式（最新在前；ts 为 DB 原样 ISO 8601，可能含 Z/+00:00 或为空）：
        [2026-05-13T14:32:00+00:00] [risk/high] [NDQ.AX, NVDA] (sources: reuters, bloomberg)
        Nvidia Q1 guidance miss, futures -3% AH.

        [2026-05-12T08:00:00Z] [opportunity/mid] [GC=F] (sources: ft)
        Powell signals dovish pivot; gold up 1.2%.
         ↳ supersedes 2026-05-10 hawkish-fed event

    **头行格式是与 utils/sentiment._parse_event_brief_entries 的互解析契约**
    （EVENT_STANCE 聚合行靠它解析 stance/severity/syms/ts）——改头行结构必须
    同步解析正则 + tests/test_event_rag_resolve.py 的互解析回归测试。
    """
    if not events:
        return ""
    lines: List[str] = []
    for e in events:
        ts = e.get("ts", "")
        stance = e.get("stance", "neutral")
        severity = e.get("severity", "low")
        affected = ", ".join(e.get("affected_symbols") or [])
        # PR #5 Copilot CR: set comprehension 的迭代顺序非确定 → LLM 看到的
        # source 顺序每次跑都不同，影响 token-level cache / replay 稳定性。
        # 改用 dict.fromkeys 保留首次出现顺序 + sorted 兜底，全确定。
        _source_names = [(s.get("src_name") or "").split(":")[0] for s in (e.get("sources") or [])]
        sources = ", ".join(sorted(dict.fromkeys(_source_names)))
        src_part = f" (sources: {sources})" if sources else ""
        lines.append(
            f"[{ts}] [{stance}/{severity}] [{affected}]{src_part}\n"
            f"{e.get('one_line_claim', '')}"
        )
        if e.get("supersedes"):
            lines.append(f" ↳ supersedes event {e['supersedes']}")
        lines.append("")
    return "\n".join(lines).strip()



__all__ = [
    "format_event_brief",
]
