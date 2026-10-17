"""Decision Accounting（issue #133 Decision 9）— 决议 ↔ 执行 ↔ 结果的读时 join。

三份既有数据从未关联：committee md（决议）、trades.db（执行）、
verdict_review.jsonl / interventions.jsonl（结果/干预）。本模块补上 join，
不物化新视图文件——唯一新增持久化是 executions.jsonl（用户执行/拒绝记录）。

decision_id 口径 = "<date>/<symbol>"（如 "2026-07-03/GC=F"），即
memory/.committee/<date>/<sym>.md 的天然主键；trades.db 现有 verdict_id
列填同一格式即完成硬关联，无 schema 变更。

executions.jsonl 是追加账本（ADR-016）：重放/重试跳过完全相同的最新记录；
内容不同则 append 新行，读方取每 decision_id 最后一条（允许改口）。
"""
from __future__ import annotations

# 聚合纯核已迁 decision_calc（ADR-026）——导回保持历史导出面
from openinvest.core.decision_calc import (  # noqa: F401
    MATCH_WINDOW_DAYS,
    _VERDICT_DIRECTION,
    _match_trades,
    _safe_stem,
    _vid_matches,
    history_as_trades,
    summarize_decisions,
)

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.utils.symbols import safe_symbol

log = logging.getLogger(__name__)



_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_committee_file(path: Path) -> Optional[Dict[str, Any]]:
    """从 committee md 抽 verdict + macro snapshot（单一可信源，原 jobs/verdict_review 私有）。"""
    if not path.exists():
        return None
    return parse_committee_text(path.read_text(encoding="utf-8"))


def parse_committee_text(text: str) -> Optional[Dict[str, Any]]:
    """parse_committee_file 的文本版——远端模式拿到的是 hub 回传的 md 正文。"""
    verdict_m = re.search(r"\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s+([\d.]+)\)", text)
    if not verdict_m:
        return None
    macro_m = re.search(r"## Macro Context Snapshot.*?```json\n(.*?)\n```", text, re.DOTALL)
    macro = {}
    if macro_m:
        try:
            macro = json.loads(macro_m.group(1))
        except json.JSONDecodeError:
            pass
    # Input Health 块（2026-10 计分板诚实化）：哪些输入为空 / 行情最后一根 / 规则改写 / 强制 HOLD。
    # 老存档没有这块 → {}（读方按"未知"处理，不猜）。
    health: Dict[str, Any] = {}
    health_m = re.search(r"## Input Health.*?```json\n(.*?)\n```", text, re.DOTALL)
    if health_m:
        try:
            parsed_health = json.loads(health_m.group(1))
            if isinstance(parsed_health, dict):
                health = parsed_health
        except json.JSONDecodeError:
            pass
    run_m = re.search(r"\*\*Run at\*\*:\s*(\S+)", text)
    # CIO memo 正文：_persist 版 "## CIO Memo (Round 3)" 到下一个 "---\n\n## " 分节；
    # Coordinator 版 "### CIO" 到下一个 "### ROLE" 小节（任意全大写角色名，不写死——
    # 以后 CIO 后面加别的小节也不会被吞进 memo）。取不到 → None（缓存命中时让 agent 不必再 explain_decision）。
    # 版式由 tests/test_run_committee_cached.py 的落盘回读测试守——改了分节标题那里会红。
    memo_m = (re.search(r"## CIO Memo[^\n]*\n(.*?)(?=\n---\s*\n\s*## |\Z)", text, re.DOTALL)
              or re.search(r"\n### CIO\n(.*?)(?=\n### [A-Z][A-Z0-9_]*\n|\Z)", text, re.DOTALL))
    sym_m = re.search(r"\*\*Symbol\*\*:\s*(\S+)", text)
    # 标签带基准币种（"Suggested allocation CNY" / "… AUD"），老存档都是 CNY
    alloc_m = re.search(r"\*\*Suggested allocation ([A-Z]{3})\*\*:\s*(-?[\d,.]+)", text)
    alloc = None
    if alloc_m:
        try:
            alloc = float(alloc_m.group(2).replace(",", ""))
        except ValueError:
            pass
    return {
        "verdict": verdict_m.group(1).upper(),
        "confidence": float(verdict_m.group(2)),
        "macro_at_decision": macro,
        "symbol": sym_m.group(1) if sym_m else None,
        "alloc_cny": alloc,
        "alloc_currency": alloc_m.group(1) if alloc_m else None,
        "input_health": health,
        "run_at": run_m.group(1) if run_m else None,
        "cio_memo": memo_m.group(1).strip() if memo_m else None,
    }


def parse_decision_id(decision_id: str) -> tuple[str, str]:
    """"<date>/<symbol>" → (date, symbol)，不合法抛 ValueError。
    date 段必须是日期字面量——否则 "../.." 之类会拼进路径逃出 .committee/。"""
    if "/" not in decision_id:
        raise ValueError(f'decision_id must be "<date>/<symbol>", got {decision_id!r}')
    date, symbol = decision_id.split("/", 1)
    if not _DATE_DIR.fullmatch(date):
        raise ValueError(f"invalid date segment in decision_id: {date!r}")
    return date, symbol


def explain_decision(decision_id: str) -> Dict[str, Any]:
    """单条决议完整回放：辩论全文 + CIO memo + 决策时点 path snapshot。

    MCP `explain_decision` / CLI `explain_decision` 共用（同 record_execution 模式）。
    错误返回 {"status": "error", "error": ...}，不抛。
    """
    from openinvest.core.memory_store import MemoryStore
    from openinvest.jobs.review_calc import confidence_display
    from openinvest.jobs.verdict_review import load_confidence_lookup

    try:
        date, symbol = parse_decision_id(decision_id)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    safe = safe_symbol(symbol)
    base = MemoryStore().root / ".committee" / date
    md = base / f"{safe}.md"
    parsed = parse_committee_file(md)
    if not parsed:
        return {"status": "error",
                "error": f"decision {decision_id} not found ({md} missing or has no verdict)"}
    path_json = base / f"{safe}_path.json"
    path_snapshot = None
    if path_json.exists():
        try:
            path_snapshot = json.loads(path_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "decision_id": decision_id,
        **{k: parsed[k] for k in ("verdict", "confidence", "alloc_cny", "alloc_currency")},
        "confidence_lookup": confidence_display(parsed, load_confidence_lookup(), with_raw=False),
        "transcript_markdown": md.read_text(encoding="utf-8"),
        "path_snapshot": path_snapshot,
    }


def _executions_path() -> Path:
    from openinvest.core.memory_store import MemoryStore
    return MemoryStore().root / ".dreams" / "executions.jsonl"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record_execution(
    decision_id: str,
    executed: bool,
    reason: Optional[str] = None,
    trade_ids: Optional[List[int]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """记录用户对某决议的执行/拒绝（宿主 Agent 通过 CLI/API/MCP 回写）。

    幂等（ADR-016）：与该 decision_id 现存最新记录内容相同 → 跳过 append。
    check-then-append 全程持 fcntl 排他锁——HTTP 重试 / agent 重发 / CLI 与 MCP
    并发同写时不会双记（ADR-016 的原子幂等闸要求）。
    """
    import fcntl

    if "/" not in decision_id:
        raise ValueError(f"decision_id 应为 '<date>/<symbol>'，收到: {decision_id!r}")
    rec = {
        "schema": 1,
        "decision_id": decision_id,
        "executed": bool(executed),
        "reason": reason or None,
        "trade_ids": trade_ids or None,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    p = path or _executions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        latest = None
        for line in f.read().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("decision_id") == decision_id:
                latest = r
        if latest is not None and all(
            latest.get(k) == rec[k] for k in ("executed", "reason", "trade_ids")
        ):
            return latest  # 重放/重试，幂等跳过（锁随 with 释放）
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec








def list_decisions(days: int = 90) -> List[Dict[str, Any]]:
    """统一决策视图：每条委员会决议 join 干预 / 执行 / 事后结果。最新在前。

    全部读时 join——数据源是四份既有账本，不新增物化文件：
      memory/.committee/<date>/<sym>.md   决议（含被规则改写后的最终 verdict）
      .dreams/interventions.jsonl         规则干预（原始 verdict → 最终 verdict）
      trades.db                           实际成交（显式 verdict_id 或窗口匹配）
      memory/portfolio_history.jsonl      MCP/CLI buy|sell 记的成交（只在这里，不写 trades.db）
      .dreams/verdict_review.jsonl        事后 returns/hits（verdict_review cron 回填）
      .dreams/executions.jsonl            用户执行/拒绝声明（本模块写入）
    """
    from openinvest.core.memory_store import MemoryStore
    from openinvest.db.trades_db import TradesDB

    root = MemoryStore().root / ".committee"
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    interventions = _load_jsonl(MemoryStore().root / ".dreams" / "interventions.jsonl")
    reviews = _load_jsonl(MemoryStore().root / ".dreams" / "verdict_review.jsonl")
    executions: Dict[str, Dict[str, Any]] = {}
    for r in _load_jsonl(_executions_path()):
        if r.get("decision_id"):
            executions[r["decision_id"]] = r  # 后写覆盖 → 最新记录生效

    try:
        trades = TradesDB().list_trades(limit=10000)
    except Exception as e:  # noqa: BLE001  账本读失败不挡视图
        log.warning(f"decision_ledger 读 trades.db 失败 graceful：{e}")
        trades = []
    # agent 路径（MCP/CLI → PortfolioManager.buy|sell）只写 portfolio_history.jsonl；
    # 不并进来，wiki 承诺的"7 天同标的同向自动匹配"对 agent 记的成交永远不成立
    trades = trades + history_as_trades(_load_jsonl(MemoryStore().root / "portfolio_history.jsonl"))

    # sanitized stem → 真实 symbol 回退映射（Coordinator/旧版 md 无 **Symbol**: 行时用；
    # 否则 decision_id 退化成 "GC_F" 与 interventions/trades 的 "GC=F" 永远 join 不上）
    known_symbols = ({i.get("asset") for i in interventions}
                     | {r.get("asset") for r in reviews}
                     | {t.get("symbol") for t in trades})
    stem_to_symbol = {_safe_stem(s): s for s in known_symbols if s}

    window_open_after = (datetime.now() - timedelta(days=MATCH_WINDOW_DAYS)).strftime("%Y-%m-%d")

    decisions: List[Dict[str, Any]] = []
    if not root.exists():
        return decisions
    for day_dir in sorted(root.iterdir(), reverse=True):
        if not day_dir.is_dir() or not _DATE_DIR.match(day_dir.name):
            continue  # 跳过 <task_id>/status.json 等非日期目录
        if day_dir.name < cutoff:
            continue
        for md in sorted(day_dir.glob("*.md")):
            parsed = parse_committee_file(md)
            if not parsed:
                continue
            date = day_dir.name
            symbol = parsed["symbol"] or stem_to_symbol.get(md.stem, md.stem)
            decision_id = f"{date}/{symbol}"

            iv = next((i for i in interventions
                       if i.get("date") == date and i.get("asset") == symbol), None)
            rv = next((r for r in reviews
                       if r.get("date") == date and r.get("asset") == symbol
                       and r.get("source") == "live"), None)
            matched = _match_trades(trades, decision_id, date, symbol, parsed["verdict"],
                                    run_at=parsed.get("run_at"))
            decl = executions.get(decision_id)

            executed: Optional[bool] = None  # None = 未知（无声明也无匹配依据）
            if decl is not None:
                executed = bool(decl["executed"])
            elif matched:
                executed = True
            elif parsed["verdict"] in _VERDICT_DIRECTION and date < window_open_after:
                # 7 天匹配窗已关、无成交无声明 → 判未执行。
                # 窗还开着（date ≥ 今天-7d）时保持 None——用户可能明天才下单，
                # 过早判 False 会把新决议全算进 not_executed 拉低采纳率
                executed = False

            decisions.append({
                "decision_id": decision_id,
                "date": date,
                "symbol": symbol,
                "verdict": parsed["verdict"],
                "confidence": parsed["confidence"],
                "alloc_cny": parsed["alloc_cny"],
                "alloc_currency": parsed["alloc_currency"],
                "intervention": None if iv is None else {
                    "rule": iv.get("rule"),
                    "rule_family": iv.get("rule_family"),
                    "original_verdict": iv.get("original_verdict"),
                    "original_alloc": iv.get("original_alloc"),
                },
                "executed": executed,
                "execution": decl,
                "matched_trades": [
                    {k: t.get(k) for k in ("id", "ts", "direction", "units", "price", "status")}
                    for t in matched
                ],
                "outcome": None if rv is None else {
                    "actual_returns": rv.get("actual_returns"),
                    "hits": rv.get("hits"),
                    "macro_shock": (rv.get("macro_shock") or {}).get("detected"),
                },
            })
    return decisions




def decisions_view(days: int = 90, symbol: Optional[str] = None,
                   verdict: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """MCP / CLI 共用的 decisions 输出：过滤后的 summary + 最近 limit 条（limit<=0 = 全部）。

    issue #133 差距 #3：全量 join 结果直接灌进 agent context，积累一年会撑爆。
    count / summary 按过滤后的全集算，只有 decisions 列表被截断。
    symbol 不分大小写（LLM 吐的 ticker 大小写会漂，同 #232-4）。
    """
    ds = list_decisions(days=days)
    if symbol:
        ds = [d for d in ds if str(d["symbol"]).upper() == symbol.strip().upper()]
    if verdict:
        ds = [d for d in ds if str(d["verdict"]).upper() == verdict.strip().upper()]
    shown = ds if limit <= 0 else ds[:limit]
    return {"count": len(ds), "returned": len(shown),
            "summary": summarize_decisions(ds), "decisions": shown}


__all__ = [
    "parse_committee_file",
    "record_execution",
    "list_decisions",
    "decisions_view",
    "summarize_decisions",
    "MATCH_WINDOW_DAYS",
]
