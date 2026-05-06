"""Verdict 后验复盘 — 算 1d/7d/30d 命中率 + 区分宏观突变 vs 模型差。

输入：memory/.committee/<date>/<symbol>.md（含 macro_context_at_decision）
     memory/.backtest/<date>/<symbol>.md（backtest 产生的，同 schema）
输出：docs/verdict_accuracy.md (gitignored, 含真数字给本地分析用)
     memory/.dreams/verdict_review.jsonl（结构化结果，给 dreaming 用）

命中率定义：
- BUY / ACCUMULATE → 后续涨 >0% = hit
- SELL / TRIM → 后续跌 <0% = hit
- HOLD → 波动 |return| < 3% = hit (说明"无操作"是对的)

上下文归因（A1 增强）：
- 事后 30 天内 VIX 变化 > 30% → 标记 "macro_shock"，verdict 错也免责
- TNX 变化 > 50bp → 同上
- 这样 README 上能展示："60% 命中率，剔除 macro_shock 后 75%"
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

# 命中率窗口：天级 / 周级 / 月级，给短期+中期反馈
HIT_WINDOWS = [1, 7, 30]

# 宏观突变阈值（剔除黑天鹅时用）
MACRO_SHOCK_THRESHOLDS = {
    "vix_pct_change": 0.30,        # VIX 变化 > 30% 算突变
    "tnx_bp_change": 50,            # TNX 变化 > 50 bp
    "usdcny_pct_change": 0.03,      # 人民币 ±3%
}

# verdict → 期望方向
EXPECTED_DIRECTION = {
    "BUY": "up",
    "ACCUMULATE": "up",
    "SELL": "down",
    "TRIM": "down",
    "HOLD": "flat",  # 期望波动小
}


@dataclass
class VerdictReview:
    """单次 verdict 的事后评估"""
    date: str
    asset: str
    verdict: str
    confidence: float
    expected_direction: str
    macro_at_decision: Dict[str, float]
    actual_returns: Dict[str, float] = field(default_factory=dict)  # {"1d": 0.012, "7d": -0.034, ...}
    hits: Dict[str, bool] = field(default_factory=dict)  # 同上 key
    macro_shock: Dict[str, Any] = field(default_factory=dict)  # 事后 macro 突变标记
    source: str = "live"  # "live" 或 "backtest"


# ---------- 解析 ----------

def _parse_committee_file(path: Path) -> Optional[Dict[str, Any]]:
    """从 committee md 文件抽 verdict + macro snapshot"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
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
    return {
        "verdict": verdict_m.group(1).upper(),
        "confidence": float(verdict_m.group(2)),
        "macro_at_decision": macro,
    }


# ---------- 事后涨跌 ----------

def _get_actual_return(symbol: str, decision_date: str, window_days: int) -> Optional[float]:
    """从 yfinance 拉 D 和 D+window 的收盘价，算累计涨跌 %"""
    from utils.exchange_fee import get_history_data
    try:
        d = datetime.strptime(decision_date, "%Y-%m-%d").date()
        end = d + timedelta(days=window_days + 5)  # 给 5 天 buffer 处理周末
        # 需要拉到 end 那天的数据，period 不能精确控制，用够长的窗口
        df = get_history_data(symbol, "1y")
        if df.empty:
            return None
        # 找 D 当天或最近的下一交易日
        df_after_d = df[df.index.date >= d]
        if df_after_d.empty:
            return None
        anchor_close = float(df_after_d["Close"].iloc[0])
        # 找 D+window 当天或之前最近交易日
        target_date = d + timedelta(days=window_days)
        df_at_window = df[df.index.date <= target_date]
        if df_at_window.empty or len(df_at_window) < 1:
            return None
        last_close = float(df_at_window["Close"].iloc[-1])
        return (last_close / anchor_close) - 1.0
    except Exception as e:
        print(f"⚠️ get_actual_return({symbol}, {decision_date}, {window_days}d) 失败: {e}")
        return None


# ---------- 宏观突变检测 ----------

def _detect_macro_shock(
    macro_at_decision: Dict[str, float],
    decision_date: str,
    window_days: int,
) -> Dict[str, Any]:
    """对比决议时和 D+window 后的 macro 快照，标记是否发生突变"""
    from utils.exchange_fee import get_history_data
    shock: Dict[str, Any] = {"detected": False, "drivers": []}

    def _get_close_on(symbol: str, date_str: str) -> Optional[float]:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            df = get_history_data(symbol, "1y")
            if df.empty:
                return None
            df_at = df[df.index.date <= d]
            if df_at.empty:
                return None
            return float(df_at["Close"].iloc[-1])
        except Exception:
            return None

    target_date = (datetime.strptime(decision_date, "%Y-%m-%d").date()
                   + timedelta(days=window_days)).strftime("%Y-%m-%d")

    # VIX
    vix_at = macro_at_decision.get("vix")
    vix_after = _get_close_on("^VIX", target_date)
    if vix_at and vix_after:
        change = abs(vix_after / vix_at - 1)
        if change > MACRO_SHOCK_THRESHOLDS["vix_pct_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"VIX {vix_at:.1f} → {vix_after:.1f} ({change*100:+.0f}%)")

    # TNX (单位是 % 数字，bp = 0.01)
    tnx_at = macro_at_decision.get("tnx")
    tnx_after = _get_close_on("^TNX", target_date)
    if tnx_at and tnx_after:
        bp_change = abs(tnx_after - tnx_at) * 100
        if bp_change > MACRO_SHOCK_THRESHOLDS["tnx_bp_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"TNX {tnx_at:.2f}% → {tnx_after:.2f}% ({bp_change:+.0f}bp)")

    # USDCNY
    usdcny_at = macro_at_decision.get("usdcny")
    usdcny_after = _get_close_on("USDCNY=X", target_date)
    if usdcny_at and usdcny_after:
        change = abs(usdcny_after / usdcny_at - 1)
        if change > MACRO_SHOCK_THRESHOLDS["usdcny_pct_change"]:
            shock["detected"] = True
            shock["drivers"].append(f"USDCNY {usdcny_at:.3f} → {usdcny_after:.3f} ({change*100:+.1f}%)")

    return shock


# ---------- hit 判定 ----------

def _is_hit(verdict: str, return_pct: float) -> bool:
    direction = EXPECTED_DIRECTION.get(verdict, "flat")
    if direction == "up":
        return return_pct > 0
    elif direction == "down":
        return return_pct < 0
    elif direction == "flat":
        return abs(return_pct) < 0.03  # |涨跌| < 3% 算 HOLD 命中
    return False


# ---------- 主流程 ----------

def review_one(committee_dir: Path, asset_symbol: str) -> Optional[VerdictReview]:
    """对单个 verdict 文件做事后 review"""
    decision_date = committee_dir.name  # YYYY-MM-DD
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", asset_symbol)
    path = committee_dir / f"{safe_sym}.md"
    parsed = _parse_committee_file(path)
    if not parsed:
        return None

    rv = VerdictReview(
        date=decision_date,
        asset=asset_symbol,
        verdict=parsed["verdict"],
        confidence=parsed["confidence"],
        expected_direction=EXPECTED_DIRECTION.get(parsed["verdict"], "flat"),
        macro_at_decision=parsed["macro_at_decision"],
        source="backtest" if "backtest" in str(committee_dir) else "live",
    )

    for window in HIT_WINDOWS:
        ret = _get_actual_return(asset_symbol, decision_date, window)
        if ret is not None:
            rv.actual_returns[f"{window}d"] = round(ret, 4)
            rv.hits[f"{window}d"] = _is_hit(parsed["verdict"], ret)

    # 30 天窗口的宏观突变检测（只对最长窗口算，节省时间）
    rv.macro_shock = _detect_macro_shock(
        parsed["macro_at_decision"], decision_date, window_days=30
    )

    return rv


def review_all(*, include_backtest: bool = True, include_live: bool = True) -> List[VerdictReview]:
    """扫所有历史 verdict 做 review"""
    store = MemoryStore()
    reviews: List[VerdictReview] = []

    asset_symbols_to_check = ["NDQ.AX", "GC=F", "GC_F"]  # 兼容旧 sym 命名

    sources: List[Path] = []
    if include_live and (store.root / ".committee").exists():
        sources.extend(sorted((store.root / ".committee").iterdir()))
    if include_backtest and (store.root / ".backtest").exists():
        sources.extend(sorted((store.root / ".backtest").iterdir()))

    for date_dir in sources:
        if not date_dir.is_dir():
            continue
        for asset in asset_symbols_to_check:
            rv = review_one(date_dir, asset)
            if rv:
                reviews.append(rv)
    return reviews


# ---------- 报告生成 ----------

def summarize(reviews: List[VerdictReview]) -> Dict[str, Any]:
    """聚合命中率，按 verdict 类型 + 窗口分类"""
    summary: Dict[str, Any] = {"total": len(reviews), "by_window": {}, "by_verdict": {},
                                "macro_shock_count": 0, "live_count": 0, "backtest_count": 0}
    for window in HIT_WINDOWS:
        key = f"{window}d"
        hits = [r.hits[key] for r in reviews if key in r.hits]
        if hits:
            summary["by_window"][key] = {
                "n": len(hits),
                "hit_rate": round(sum(hits) / len(hits), 3),
            }
            # 剔除 macro shock 后再算
            non_shock = [r.hits[key] for r in reviews if key in r.hits and not r.macro_shock.get("detected")]
            if non_shock:
                summary["by_window"][key]["hit_rate_excl_macro_shock"] = round(
                    sum(non_shock) / len(non_shock), 3
                )
                summary["by_window"][key]["n_excl_shock"] = len(non_shock)

    for verdict in ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"]:
        subset = [r for r in reviews if r.verdict == verdict]
        if subset:
            summary["by_verdict"][verdict] = {
                "n": len(subset),
                "avg_confidence": round(sum(r.confidence for r in subset) / len(subset), 3),
            }
            for window in HIT_WINDOWS:
                key = f"{window}d"
                hits = [r.hits[key] for r in subset if key in r.hits]
                if hits:
                    summary["by_verdict"][verdict][f"hit_rate_{key}"] = round(
                        sum(hits) / len(hits), 3
                    )

    summary["macro_shock_count"] = sum(1 for r in reviews if r.macro_shock.get("detected"))
    summary["live_count"] = sum(1 for r in reviews if r.source == "live")
    summary["backtest_count"] = sum(1 for r in reviews if r.source == "backtest")
    return summary


def write_report(reviews: List[VerdictReview], summary: Dict[str, Any]) -> Path:
    """输出 markdown 报告到 docs/verdict_accuracy.md (gitignore 自动保护)"""
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "verdict_accuracy.md"

    lines = [
        "# Verdict Accuracy Report",
        f"\n*Generated: {datetime.now().isoformat(timespec='seconds')}*",
        f"\n**总 verdict 数**: {summary['total']} ({summary['live_count']} live + {summary['backtest_count']} backtest)",
        f"\n**宏观突变窗口数**: {summary['macro_shock_count']}\n",
        "\n## 按时间窗口命中率\n",
        "| 窗口 | N | 总命中率 | 剔除宏观突变后 |",
        "|---|---|---|---|",
    ]
    for w_key, w in summary["by_window"].items():
        excl = w.get("hit_rate_excl_macro_shock", "—")
        excl_n = w.get("n_excl_shock", "—")
        lines.append(f"| {w_key} | {w['n']} | {w['hit_rate']*100:.1f}% | "
                     f"{excl*100:.1f}% (n={excl_n}) |" if isinstance(excl, float)
                     else f"| {w_key} | {w['n']} | {w['hit_rate']*100:.1f}% | — |")

    lines.append("\n## 按 verdict 类型命中率\n")
    lines.append("| Verdict | N | 平均 confidence | 1d hit | 7d hit | 30d hit |")
    lines.append("|---|---|---|---|---|---|")
    for v_key, v in summary["by_verdict"].items():
        lines.append(
            f"| {v_key} | {v['n']} | {v['avg_confidence']:.2f} | "
            f"{v.get('hit_rate_1d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_7d', 0)*100:.0f}% | "
            f"{v.get('hit_rate_30d', 0)*100:.0f}% |"
        )

    # 动态解读：基于真实数据生成，不再用静态模板
    lines.append("\n## 诚实解读（基于真实数据）\n")

    by_v = summary["by_verdict"]
    hold_n = by_v.get("HOLD", {}).get("n", 0)
    directional_n = sum(by_v.get(v, {}).get("n", 0) for v in
                         ["BUY", "ACCUMULATE", "SELL", "TRIM"])

    if directional_n == 0:
        lines.append("⚠️ **没有任何方向性 verdict**（BUY/ACCUMULATE/SELL/TRIM 全为 0）。"
                     "系统过度保守，不构成可操作 alpha。")
    else:
        # 算方向性 verdict 的命中率（剔除 HOLD 的水分）
        directional_hits_30d = sum(
            by_v.get(v, {}).get("hit_rate_30d", 0) * by_v.get(v, {}).get("n", 0)
            for v in ["BUY", "ACCUMULATE", "SELL", "TRIM"]
        )
        directional_rate = directional_hits_30d / directional_n if directional_n else 0
        lines.append(f"### 方向性 verdict 真实命中率：{directional_rate*100:.1f}% (n={directional_n})\n")
        lines.append("**说明**：剔除 HOLD（命中率被'波动 <3% 算 hit'的定义灌水）后的真实 alpha 信号。\n")

        if directional_rate < 0.5:
            lines.append("🔴 **低于随机**：方向性判断比抛硬币还差。可能原因：")
            lines.append("- LLM 在 ACCUMULATE 时建议加仓，但市场继续跌（contrarian 时机错）")
            lines.append("- BUY/SELL 触发太晚，趋势已经反转")
            lines.append("- prompt 鼓励太多 cross-challenge 调整，磨损了 Round 1 的真实信号\n")
        elif directional_rate < 0.6:
            lines.append("🟡 **接近随机**：方向性判断有微弱信号，但样本量不足以确认。\n")
        else:
            lines.append("🟢 **高于随机**：方向性判断有真实 alpha。继续积累样本验证。\n")

    if hold_n / max(summary["total"], 1) > 0.7:
        lines.append(f"⚠️ **HOLD 占比 {hold_n/summary['total']*100:.0f}%**：系统过度保守，"
                     "几乎不给方向性 verdict。HOLD 的 100% 命中率是统计假象（'市场没动 = HOLD 对'），"
                     "不是预测能力。\n")

    if summary["macro_shock_count"] > 0:
        shock_pct = summary["macro_shock_count"] / summary["total"] * 100
        lines.append(f"📉 **{summary['macro_shock_count']} 个 verdict 后期遭遇宏观突变** "
                     f"({shock_pct:.0f}%)：剔除后真实命中率应参考'剔除宏观突变后'列。")

    if summary["total"] < 30:
        lines.append(f"📊 **样本量 {summary['total']} 条偏小**：30 天 backtest 信噪比仍低，"
                     "建议跑 90+ 天后再做正式评估。")

    lines.append("\n## 已知 backtest 局限\n")
    lines.append("- portfolio_summary 在 backtest 模式是 mock 的'中性持仓'，LLM 看不到当时真实持仓状态")
    lines.append("- prior_insights 在 backtest 时为空（防穿越），失去 Dreaming 长期模式增强")
    lines.append("- 新闻/宏观叙事不在 tool 里（防 DDGS 时间泄露），Macro Strategist 仅靠数值指标")
    lines.append("- 这些限制让 backtest 结果是 LLM 能力的**下限**估计，实盘可能更好（也可能更差）")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_jsonl(reviews: List[VerdictReview]) -> Path:
    """落 jsonl 给 Dreaming 后续 mining 用"""
    store = MemoryStore()
    out = store.root / ".dreams" / "verdict_review.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in reviews:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    return out


def run() -> Dict[str, Any]:
    """job entry"""
    reviews = review_all()
    if not reviews:
        return {"status": "skipped", "reason": "no committee verdicts to review"}
    summary = summarize(reviews)
    md_path = write_report(reviews, summary)
    jsonl_path = write_jsonl(reviews)
    return {
        "status": "ok",
        "summary": summary,
        "report": str(md_path),
        "jsonl": str(jsonl_path),
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
