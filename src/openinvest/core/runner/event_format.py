"""事件 brief 格式化纯核（域绑定纯模块，ADR-026）

从 core/runner/event_brief.py 拆出：events 列表 → brief 文本（纯拼接）。
头行格式契约与 calc/sentiment._EVENT_HEADER_RE 互解析（改一边必须同步另一边
+ tests/test_event_rag_resolve.py 回归）。EventStore/RAG 召回留在 event_brief.py。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

# 宿主 agent 自报的身份只允许这几类字符（ingest 边界已按同款清洗；这里是二道防线）：
# 带 "] [risk/high] [GC=F" 或换行的身份串会伪造头行，污染 calc/sentiment 的互解析
_AGENT_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_agent_id(who: Any) -> str:
    return _AGENT_ID_SAFE_RE.sub("_", str(who))[:32]


# 同论点归并（B6 后半）：同一条新闻被多家转载 = 同一个论点，不该被当成多条独立证据。
# event_store 的 event_id 是整句 claim 的 sha256，措辞差一个词就是新 id，挡不住转载 → 这里再聚一次。
#
# 归并条件（全部满足才算同论点）：
#   1. 桶相同：影响标的集合、stance、severity、来源身份（ingested_by；爬虫 = 空）都一样。
#      - stance/severity 不同永不合并：立场相反的两条哪怕措辞几乎一样，也是两条证据（防证据压制）。
#      - agent 投喂的事件只和同一个 agent 自己的合并，永不与爬虫事件合并 → 爬虫的 N 不含 agent，
#        agent 事件也吸收不了爬虫事件（防来源洗白）。保留行沿用 [agent:<who>] 标签。
#   2. 归一化后的**完整 claim** 相似度 ≥ _THESIS_SIM_MIN（difflib.SequenceMatcher，对比簇里第一条），
#      且两句里出现的数字**按出现顺序**完全一致、小数不拆（"加息 25bp" 与 "加息 50bp" 只差一个数字；
#      "CPI 2.5% vs 预期 2.6%" 与 "CPI 2.6% vs 预期 2.5%" 数字相同顺序相反，一个低于预期一个高于预期——都是两个事实，不是转载）。
#      旧版只比前 60 字，开头相同、结尾不同的两句（"……加息 25bp，市场预期再加一次" vs "……加息 25bp，但暗示暂停"）会被误并。
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NON_WORD_RE = re.compile(r"[\W_]+")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_THESIS_SIM_MIN = 0.85


def _norm_claim(claim: Any) -> str:
    return _NON_WORD_RE.sub(" ", _ISO_DATE_RE.sub("", str(claim or "").lower())).strip()


def _numbers(claim: Any) -> Tuple[str, ...]:
    """claim 里的数字，按出现顺序、小数不拆、先剥 ISO 日期。"""
    return tuple(_NUMBER_RE.findall(_ISO_DATE_RE.sub("", str(claim or "").lower())))


def _ts_rank(e: Dict[str, Any], i: int) -> Tuple[int, float, int]:
    """排序键：ts 越新越靠前；解析不了的排最后，同级按输入顺序。naive ts 当 UTC。"""
    try:
        t = datetime.fromisoformat(str(e.get("ts") or "").strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return 0, -t.timestamp(), i
    except ValueError:
        return 1, 0.0, i


def _bucket(e: Dict[str, Any]) -> Tuple[frozenset, Any, Any, str]:
    syms = frozenset(str(x).upper() for x in (e.get("affected_symbols") or []))
    who = _safe_agent_id(e["ingested_by"]) if e.get("ingested_by") else ""
    return syms, e.get("stance"), e.get("severity"), who


def collapse_corroboration(
    events: List[Dict[str, Any]], cap: int,
) -> List[Tuple[Dict[str, Any], int]]:
    """同论点（见上方归并条件）最多保留 cap 条，留 ts 最新的（跨 symbol 合并后输入未必是最新在前）。

    返回 [(event, n)]，保持输入顺序：n>0 表示该论点共 n 条报道、有被折叠的，标在该簇最新的保留条上
    （渲染成 "(×n 来源)"）；n=0 不加标。cap<=0 = 不归并（旧行为）。claim 归一后为空 → 不参与归并。
    """
    if cap <= 0:
        return [(e, 0) for e in events]
    clusters: List[Tuple[Tuple, str, List[int]]] = []   # (桶 + 数字, 代表 claim, 成员下标)
    cluster_of: List[Optional[int]] = [None] * len(events)
    for i, e in enumerate(events):
        claim = _norm_claim(e.get("one_line_claim"))
        if not claim:
            continue
        bucket = (_bucket(e), _numbers(e.get("one_line_claim")))
        for ci, (b, rep, members) in enumerate(clusters):
            # ponytail: 只和簇首比（不传递链式合并）；事件数 <= 几十条，O(n²) 无所谓
            if b == bucket and (claim == rep or SequenceMatcher(None, claim, rep, autojunk=False).ratio() >= _THESIS_SIM_MIN):
                members.append(i)
                cluster_of[i] = ci
                break
        else:
            clusters.append((bucket, claim, [i]))
            cluster_of[i] = len(clusters) - 1
    kept: Dict[int, int] = {}   # 下标 → n（只有簇内最新的保留条可能 >0）
    for _, _, members in clusters:
        ranked = sorted(members, key=lambda i: _ts_rank(events[i], i))
        for r, i in enumerate(ranked[:cap]):
            kept[i] = len(members) if r == 0 and len(members) > cap else 0
    return [(e, kept.get(i, 0)) for i, e in enumerate(events) if cluster_of[i] is None or i in kept]


def format_event_brief(
    events: List[Dict[str, Any]], *, max_corroboration: Optional[int] = None,
) -> str:
    """事件列表 → Macro prompt 注入用的结构化文本（人类可读 + 时效冲突显式）

    格式（最新在前；ts 为 DB 原样 ISO 8601，可能含 Z/+00:00 或为空）：
        [2026-05-13T14:32:00+00:00] [risk/high] [NDQ.AX, NVDA] (sources: reuters, bloomberg)
        Nvidia Q1 guidance miss, futures -3% AH.

        [2026-05-12T08:00:00Z] [opportunity/mid] [GC=F] (sources: ft)
        Powell signals dovish pivot; gold up 1.2%.
         ↳ supersedes 2026-05-10 hawkish-fed event

        [2026-05-12T07:00:00Z] [risk/mid] [510300.SS] [agent:hermes] (sources: agent)
        PBOC ...   ← agent 投喂（events.ingested_by）打 [agent:<who>] 标签

    **头行格式是与 utils/sentiment._parse_event_brief_entries 的互解析契约**
    （EVENT_STANCE 聚合行靠它解析 stance/severity/syms/ts）——改头行结构必须
    同步解析正则 + tests/test_event_rag_resolve.py 的互解析回归测试。

    **同论点归并**（event.max_corroboration，默认 1）：同一论点被多家转载时只出 1 条，
    claim 行末标 "(×N 来源)"；stance/severity 不同、或来源身份不同（agent vs 爬虫）永不合并。EVENT_STANCE 是从这份 brief 的头行解析计数的，所以归并在
    这里做一次，Macro 看到的条数和 stance 计数同步按"不同论点"算。
    max_corroboration 显式传入优先，None 读 config；0 = 不归并。
    """
    if not events:
        return ""
    if max_corroboration is None:
        from openinvest.core.config import load_config
        max_corroboration = load_config().event.max_corroboration
    lines: List[str] = []
    for e, n_reports in collapse_corroboration(events, int(max_corroboration)):
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
        # agent 投喂的事件（MCP/CLI ingest_event → events.ingested_by 非空）打来源标签，
        # 让 Macro 知道这条不是爬虫抓的。放在 [syms] 之后：头行正则只锚前三个方括号，不破契约。
        agent_part = f" [agent:{_safe_agent_id(e['ingested_by'])}]" if e.get("ingested_by") else ""
        corroboration = f" (×{n_reports} 来源)" if n_reports else ""
        lines.append(
            f"[{ts}] [{stance}/{severity}] [{affected}]{agent_part}{src_part}\n"
            f"{e.get('one_line_claim', '')}{corroboration}"
        )
        if e.get("supersedes"):
            lines.append(f" ↳ supersedes event {e['supersedes']}")
        lines.append("")
    return "\n".join(lines).strip()



__all__ = [
    "_safe_agent_id",
    "collapse_corroboration",
    "format_event_brief",
]
