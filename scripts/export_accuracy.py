"""scripts/export_accuracy.py — verdict_review.jsonl 脱敏聚合脚本

读 memory/.dreams/verdict_review.jsonl，按时间窗口（last 30d / 90d / all）聚合
方向命中率，输出到 docs/accuracy_summary.json。

**脱敏红线**：
- 绝对不输出 symbol / threshold / verdict 原文 / asset / 任何持仓字段
- 只输出命中率聚合数字 + 样本量
- by_direction 按 bullish / bearish / hold 分组（不出现具体标的）

用法：
    python scripts/export_accuracy.py
    python scripts/export_accuracy.py --jsonl path/to/custom.jsonl
    python scripts/export_accuracy.py --out path/to/output.json

条件：
- hits 里至少有一个时间窗口为 True/False 才计入方向命中统计
- hits 全空（{}）的记录跳过（backtest 缺数据的条目）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent

# 默认输入 / 输出路径
DEFAULT_JSONL = ROOT / "memory" / ".dreams" / "verdict_review.jsonl"
DEFAULT_OUT = ROOT / "docs" / "accuracy_summary.json"

# 公开数据红线 #2：命中率 n < 30 不对外展示具体数字（防小样本被截图误传）。
# 与 GUI invest-gui/src/routes/PublicStats.tsx 的 MIN_SAMPLE_FOR_DISPLAY 保持同一阈值——
# 这里在「数据层」就把小样本 rate 置 null，避免任何人直接 curl 公开 JSON 拿到
# GUI 本该屏蔽的小样本数字。total 计数保留（GUI 仍展示 n）。
MIN_SAMPLE_FOR_PUBLIC = 30

# 命中率统计的固定观测窗口（issue #179 P1-A②）：此前 30d→7d→1d 回落取"最长有数据
# 的窗口"，同一份 rate 混着三种 horizon 的命中，数字不可比。固定单一 horizon，
# 未成熟（还没到 D+30）的记录直接不计入。
HIT_HORIZON = "30d"
HIT_HORIZON_DAYS = 30

# 时间窗口按"评估期落点"取，不按决议日取：决议要过 HIT_HORIZON_DAYS 天才成熟，
# 若 "30d" 窗口还按决议日筛（决议 ≥ today−30），它和"已成熟"（决议 ≤ today−30）
# 的交集只剩边界一天——固定 horizon 后该桶结构性恒空。所以窗口筛选统一放宽
# HIT_HORIZON_DAYS："30d" = 评估结果落在最近 30 天内的决议（决议日 ≥ today−60）。

# expected_direction → 方向组映射（verdict 原文不出现在输出里）
_DIRECTION_MAP: Dict[str, str] = {
    "up": "bullish",
    "bullish": "bullish",
    "down": "bearish",
    "bearish": "bearish",
    "flat": "hold",
    "hold": "hold",
    "neutral": "hold",
}


def _parse_date(date_str: str) -> Optional[datetime]:
    """将 YYYY-MM-DD 解析为 UTC aware datetime，无效时返回 None"""
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, AttributeError):
        return None


def _row_is_hit(row: Dict[str, Any]) -> Optional[bool]:
    """取固定观测窗口 HIT_HORIZON 的命中 bool。

    该窗口无 bool（未成熟 / 无数据）时返回 None（跳过该记录）——
    绝不回落到更短窗口混 horizon（issue #179 P1-A②）。
    """
    val = (row.get("hits") or {}).get(HIT_HORIZON)
    return val if isinstance(val, bool) else None


def _load_rows(jsonl_path: Path) -> List[Dict[str, Any]]:
    """逐行解析 jsonl，跳过格式异常行"""
    rows = []
    if not jsonl_path.exists():
        return rows
    for lineno, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  警告：跳过第 {lineno} 行（JSON 解析失败）", file=sys.stderr)
    return rows


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一批 rows 计算方向命中率聚合（脱敏：只看 expected_direction + hits + directions）"""
    # by_direction：bullish / bearish / hold 各自的 hit / total
    by_dir: Dict[str, Dict[str, int]] = {
        "bullish": {"hit": 0, "total": 0},
        "bearish": {"hit": 0, "total": 0},
        "hold": {"hit": 0, "total": 0},
    }
    total_hit = 0
    total_count = 0
    # 同样本市场基率（issue #179 P1-A②）：同一 HIT_HORIZON 窗口里市场实际
    # up/down/flat 的占比，verdict 无关——没有它，命中率没有对照系
    #（60% 命中在 70% 单边上涨的窗口里其实是负 alpha）
    dir_counts = {"up": 0, "down": 0, "flat": 0}
    dir_total = 0

    for row in rows:
        # 固定窗口无命中数据 → 跳过
        is_hit = _row_is_hit(row)
        if is_hit is None:
            continue

        raw_mkt = str((row.get("directions") or {}).get(HIT_HORIZON) or "").lower()
        if raw_mkt in dir_counts:
            dir_counts[raw_mkt] += 1
            dir_total += 1
        else:
            # 上游 verdict_review 在同一分支写 hits+directions，两者应同生同灭；
            # 命中有效但方向缺失 ⇒ base_rate.n 与 sample_size 分母静默漂移，显式喊出来
            print(
                f"  警告：记录 hits[{HIT_HORIZON}] 有效但 directions[{HIT_HORIZON}] "
                f"缺失/非法（{raw_mkt!r}）——base_rate 分母将小于 sample_size",
                file=sys.stderr,
            )

        # 方向分类（不输出 verdict 原文，只用 expected_direction 映射）
        raw_dir = str(row.get("expected_direction") or "").lower().strip()
        bucket = _DIRECTION_MAP.get(raw_dir, "hold")

        by_dir[bucket]["total"] += 1
        if is_hit:
            by_dir[bucket]["hit"] += 1

        total_count += 1
        if is_hit:
            total_hit += 1

    # 计算各方向命中率（四舍五入 2 位）
    by_dir_out: Dict[str, Any] = {}
    for bucket, counts in by_dir.items():
        t = counts["total"]
        h = counts["hit"]
        by_dir_out[bucket] = {
            "hit": h,
            "total": t,
            "rate": round(h / t, 4) if t > 0 else None,
        }

    return {
        "direction_hit_rate": (
            round(total_hit / total_count, 4) if total_count > 0 else None
        ),
        "sample_size": total_count,
        "hit_horizon": HIT_HORIZON,
        "by_direction": by_dir_out,
        "base_rate": {
            "n": dir_total,
            **{
                k: (round(v / dir_total, 4) if dir_total > 0 else None)
                for k, v in dir_counts.items()
            },
        },
    }


def _suppress_small_samples(window: Dict[str, Any]) -> Dict[str, Any]:
    """公开输出前抹掉小样本命中率（红线 #2）。

    - 窗口整体 sample_size < MIN_SAMPLE_FOR_PUBLIC → direction_hit_rate 置 None
    - 每个方向 total < MIN_SAMPLE_FOR_PUBLIC → 该方向 rate **和 hit** 都置 None
      （只藏 rate 的话 hit/total 一次除法就能还原被抑制数字——issue #179 P0-2）
    - 恰好只有一个方向被抑制时，direction_hit_rate 也一并置 None：否则
      被抑制桶的 hit = round(direction_hit_rate × sample_size) − Σ 其余桶 hit，
      减法通道仍然精确可逆（统计披露控制里的 complementary suppression）

    保留 total 计数（GUI 需要展示 n=XX「样本不足」）。
    返回新 dict（不就地改入参，便于测试对照）。
    """
    out = dict(window)
    if int(out.get("sample_size", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
        out["direction_hit_rate"] = None

    by_dir = out.get("by_direction") or {}
    new_by_dir: Dict[str, Any] = {}
    n_suppressed = 0
    for bucket, counts in by_dir.items():
        c = dict(counts)
        if int(c.get("total", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
            c["rate"] = None
            c["hit"] = None
            n_suppressed += 1
        new_by_dir[bucket] = c
    out["by_direction"] = new_by_dir
    if n_suppressed == 1:
        out["direction_hit_rate"] = None

    # base_rate 是市场属性不是模型业绩，但小样本占比同样噪音大且可被截图误读——
    # 同一 n<30 纪律
    br = out.get("base_rate")
    if br and int(br.get("n", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
        out["base_rate"] = {
            k: (None if k != "n" else v) for k, v in br.items()
        }
    return out


def _filter_by_window(
    rows: List[Dict[str, Any]],
    days: Optional[int],
    now: datetime,
) -> List[Dict[str, Any]]:
    """筛选 days 天内的记录；days=None 表示全部"""
    if days is None:
        return rows
    cutoff = now - timedelta(days=days)
    result = []
    for row in rows:
        dt = _parse_date(str(row.get("date") or ""))
        if dt is not None and dt >= cutoff:
            result.append(row)
    return result


def build_summary(jsonl_path: Path) -> Dict[str, Any]:
    """核心逻辑：读 jsonl → 按窗口聚合 → 返回脱敏 dict"""
    rows = _load_rows(jsonl_path)
    # ADR-022 红线：公开命中率只反映【live 实时决策】的真实战绩。backtest 条目一律剔除——
    # 污染段(决议日 ≤ 训练 cutoff)记忆穿越会虚高命中率，干净 holdout 也是回测而非实盘战绩；
    # 两者都不属于"live committee 准确率"。缺 source 的老条目按 live（jsonl 早于 backtest 集成时全是 live）。
    rows = [r for r in rows if str(r.get("source", "live")) == "live"]
    now = datetime.now(timezone.utc)

    windows = {
        "30d": _filter_by_window(rows, 30 + HIT_HORIZON_DAYS, now),
        "90d": _filter_by_window(rows, 90 + HIT_HORIZON_DAYS, now),
        "all": rows,
    }

    return {
        # 生成时间戳（UTC ISO）
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            # 红线 #2：公开输出抹掉小样本命中率（_aggregate 仍算原始统计，
            # 抑制只发生在对外这一层）
            name: _suppress_small_samples(_aggregate(subset))
            for name, subset in windows.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="verdict_review.jsonl 脱敏聚合 → docs/accuracy_summary.json"
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=DEFAULT_JSONL,
        help=f"输入 jsonl 路径（默认 {DEFAULT_JSONL}）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"输出 JSON 路径（默认 {DEFAULT_OUT}）",
    )
    args = parser.parse_args()

    summary = build_summary(args.jsonl)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"accuracy_summary.json 已写入: {args.out}", file=sys.stderr)
    # 同时输出到 stdout 方便脚本管道消费
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
