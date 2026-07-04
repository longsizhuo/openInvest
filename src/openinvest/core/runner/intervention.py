"""intervention — 干预账本 + 黄金防御 DCA 闸门 + rule 归桶（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

def _extract_regime_label(regime_brief: str) -> str:
    """从 format_regime_brief 输出提取 regime label（第一行 'REGIME: xxx'）"""
    for line in regime_brief.splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip()
    return ""


def rule_family(rule: str) -> str:
    """干预 rule → 粗粒度家族（聚合钱口径用，single source）。

    纯字符串映射，同时吃 live（defense_*/sanity4_*/sanity5_*）和 reconstruct
    （reconstructed_trim_blocked/_defense_downgrade）两套命名，让"同一底层规则
    拦截"在 live 行与历史重建行之间并桶——否则按细粒度 rule 聚合会拆成两组。
    - trim_blocked：sanity4(集中度)/sanity5(买回点)/config 关 lens/重建的 TRIM 被拦，都是"拦减仓"
    - buy_defense：快崩防御对买侧降级，"拦加仓"
    """
    r = rule or ""
    if "trim" in r or "sanity4" in r or "sanity5" in r or "concentration_lens" in r:
        return "trim_blocked"
    if "defense" in r or "downgrade" in r:
        return "buy_defense"
    return "other"


def _intervention_record(
    symbol: str,
    regime_label: str,
    current_price: Optional[float],
    verdict: Optional[Dict[str, Any]],
    atr_defense_on: bool,
) -> Optional[Dict[str, Any]]:
    """从 verdict 的确定性后处理标记提取"干预记录"；无实质干预返回 None。

    反事实记账（2026-06-12）：每次确定性规则改写 CIO 裁决（快崩防御降级 /
    SOLVENCY 拦 TRIM / Sanity5 否 TRIM 等）都落一条结构化记录，攒"如果没拦
    会怎样"的样本——这是防御/兜底规则未来能用钱口径验收的唯一数据来源
    （黄金 VIX 腿验收 scripts/validate_gold_defense.py 判 FAIL 后尤其如此）。
    只动 confidence 不动 verdict/alloc 的不算干预。
    """
    v = verdict or {}
    orig_v = v.get("_original_verdict")
    if not orig_v:
        return None
    final_v = v.get("verdict")
    orig_alloc = float(v.get("_original_alloc", v.get("alloc_cny", 0)) or 0)
    final_alloc = float(v.get("alloc_cny", 0) or 0)
    if orig_v == final_v and orig_alloc == final_alloc:
        return None
    if v.get("_defense_dca"):
        # 黄金防御分批 DCA（2026-06-13 裁决）：_defense_dca ∈ {"tranche","blocked_<reason>"}。
        # tranche=放行一批（final_alloc=意图×1/3，delta=踏空的 2/3）；blocked=本批暂拦
        # （final_alloc=0，delta=全意图）。rule 含 "defense" → rule_family=buy_defense，
        # 与旧全拦的 defense_downgrade 并桶，账本统一回答"分批 vs 一次性省/费多少"。
        rule = f"defense_gold_dca_{v['_defense_dca']}"
    elif v.get("_defense_downgrade"):
        rule = f"defense_{v['_defense_downgrade']}"
    elif v.get("_sanity5_reason"):
        rule = f"sanity5_{v['_sanity5_reason']}"
    elif v.get("_original_trim_reason") == "concentration":
        # 集中度减仓被拦只剩一条路径：用户主动关掉集中度 lens（solvency 自动兜底
        # 2026-06-23 已移除）。历史 jsonl 里的 sanity4_solvency_concentration 由
        # rule_family() 继续并入 trim_blocked 桶，聚合不丢。
        rule = "config_concentration_lens_off"
    else:
        rule = "other"
    from datetime import datetime
    return {
        "schema": 1,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "asset": symbol,
        "regime": regime_label,
        "price": current_price,
        "rule": rule,
        # 粗粒度归并：live 与 reconstructed 同底层规则并桶（聚合钱口径用）
        "rule_family": rule_family(rule),
        # 黄金分批 DCA：第几批（None=非 DCA 干预），账本验"分批 vs 一次性"用
        "defense_dca_tranche_idx": v.get("_defense_dca_tranche_idx"),
        "atr_defense_on": bool(atr_defense_on),
        "original_verdict": orig_v,
        "original_alloc": orig_alloc,
        "final_verdict": final_v,
        "final_alloc": final_alloc,
        # 反事实敞口差（CNY）：counterfactual_pnl_w = delta_exposure × fwd_w
        # （jobs/intervention_review.py 回填）。买侧被拦为正，卖侧被拦为负。
        "delta_exposure_cny": orig_alloc - final_alloc,
    }


def _log_intervention(record: Dict[str, Any], path: Optional[Path] = None) -> None:
    """append 进 memory/.dreams/interventions.jsonl（与 verdict_review 同目录）。

    幂等（ADR-016）：同一 (date, asset, rule) 当天已记则跳过。daily_report 无同日
    cache，每次调用都重跑委员会 → 手动重跑 / GH Actions 重触发 / 重试会对**同一**
    干预重复 append，而 intervention_review.summarize 无去重直接求和 → 反事实 PnL 翻倍。
    """
    import json

    if path is None:
        from openinvest.core.memory_store import MemoryStore
        path = MemoryStore().root / ".dreams" / "interventions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    key = (record.get("date"), record.get("asset"), record.get("rule"))
    # ponytail: O(n) 全扫；interventions 稀疏(每资产每天≤几条)，到万行级再换索引
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 坏行不挡写入
                if (r.get("date"), r.get("asset"), r.get("rule")) == key:
                    return  # 同日同资产同规则已记，幂等跳过
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _gold_defense_dca_gate(
    symbol: str,
    trading_days: Any,
    *,
    n_tranches: int,
    fraction: float,
    min_spacing_days: int,
    window_days: int,
) -> Dict[str, Any]:
    """黄金防御分批 DCA 闸（2026-06-13 裁决，wiki18 §5）：防御触发时该放第几批还是暂拦。

    读 interventions.jsonl 里本资产**已放行**的分批（rule=defense_gold_dca_tranche，
    blocked 不占配额），按**交易日**日历算 spacing/quota：
      - quota：滚动 window_days 个交易日内最多 n_tranches 批
      - spacing：距上一批 ≥ min_spacing_days 个交易日才允许下一批
    交易日日历用 service 层已有的 df.index（2y 行情），无需另起 MarketStore 调用。
    纯读 + 确定性算，返回 parse_cio_memo 要的 dict：
        {"allow": bool, "fraction": float, "reason": str, "tranche_idx": int}
    读失败/无日历 graceful 退化（保守：日历缺则用日历天近似 spacing）。
    """
    import json
    from datetime import datetime

    from openinvest.core.memory_store import MemoryStore

    frac = float(fraction)

    # 1. 已放行分批的日期（只数 tranche，blocked/其它干预不算）
    prior_dates: List[str] = []
    try:
        path = MemoryStore().root / ".dreams" / "interventions.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001  坏行跳过
                    continue
                if rec.get("asset") != symbol:
                    continue
                if rec.get("rule") != "defense_gold_dca_tranche":
                    continue
                d = rec.get("date")
                if d:
                    prior_dates.append(str(d)[:10])
    except Exception as e:  # noqa: BLE001  账本读失败 → 当作无历史，放第一批（不致命）
        log.warning(f"DCA 闸读 interventions.jsonl 失败 graceful：{e}")

    # 无历史分批 → 直接放第一批
    if not prior_dates:
        return {"allow": True, "fraction": frac, "reason": "first", "tranche_idx": 1}

    last = max(prior_dates)

    # 2. 交易日日历（df.index → 升序唯一 date 字符串）
    cal: List[str] = []
    try:
        cal = sorted({
            (ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10])
            for ts in list(trading_days)
        })
    except Exception as e:  # noqa: BLE001  日历坏 → 退化日历天
        log.warning(f"DCA 闸解析交易日日历失败 graceful，退化日历天：{e}")
        cal = []

    # 3. quota：window_days 个交易日窗内已放几批
    if cal:
        cutoff = cal[-window_days] if len(cal) >= window_days else cal[0]
    else:
        cutoff = last  # 无日历：保守用 last 当窗下界（至少把上一批算进窗内）
    tranches_in_window = sum(1 for d in prior_dates if d >= cutoff)

    # 4. spacing：距上一批的交易日数 = 日历里 (last, 最新] 的条目数
    if cal:
        trading_days_since = sum(1 for d in cal if d > last)
    else:
        try:
            trading_days_since = (
                datetime.now().date() - datetime.fromisoformat(last).date()
            ).days
        except Exception:  # noqa: BLE001
            trading_days_since = 0  # 解析失败 → 当作同日，保守暂拦

    tranche_idx = tranches_in_window + 1
    if tranches_in_window >= n_tranches:
        return {"allow": False, "fraction": frac, "reason": "quota", "tranche_idx": tranche_idx}
    if trading_days_since < min_spacing_days:
        return {"allow": False, "fraction": frac, "reason": "spacing", "tranche_idx": tranche_idx}
    return {"allow": True, "fraction": frac, "reason": "ok", "tranche_idx": tranche_idx}


def _save_path_snapshot(
    symbol: str,
    regime_label: str,
    current_price: Optional[float],
    atr_pct: Optional[float],
    path_profile: Dict[str, Any],
) -> None:
    """路径预测快照落盘 memory/.committee/<date>/<sym>_path.json（path_review 闭环）。

    落决策时的结构化 profile（与 CIO 看到的路径参考同源同算），而非事后重算——
    防代码口径漂移污染 ground truth。失败抛异常由调用方 graceful。
    """
    import json
    import re
    from datetime import datetime

    from openinvest.core.memory_store import MemoryStore

    today = datetime.now().strftime("%Y-%m-%d")
    snap_dir = MemoryStore().root / ".committee" / today
    snap_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)
    (snap_dir / f"{safe}_path.json").write_text(json.dumps({
        "schema": 1,
        "date": today,
        "symbol": symbol,
        "regime": regime_label,
        "current_price": current_price,
        "atr_pct": atr_pct,
        "profile": path_profile,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

__all__ = [
    "_extract_regime_label",
    "rule_family",
    "_intervention_record",
    "_log_intervention",
    "_gold_defense_dca_gate",
    "_save_path_snapshot",
]
