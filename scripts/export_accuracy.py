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
    """从 hits dict 中取最长窗口的 bool（30d > 7d > 1d）。

    hits 全空或全无 bool 值时返回 None（跳过该记录）。
    这是方向准确性的代理指标——用最长有数据的窗口。
    """
    hits: Dict[str, Any] = row.get("hits") or {}
    # 按优先级取最长窗口
    for window in ("30d", "7d", "1d"):
        val = hits.get(window)
        if isinstance(val, bool):
            return val
    return None  # 无有效命中数据


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
    """对一批 rows 计算方向命中率聚合（脱敏：只看 expected_direction + hits）"""
    # by_direction：bullish / bearish / hold 各自的 hit / total
    by_dir: Dict[str, Dict[str, int]] = {
        "bullish": {"hit": 0, "total": 0},
        "bearish": {"hit": 0, "total": 0},
        "hold": {"hit": 0, "total": 0},
    }
    total_hit = 0
    total_count = 0

    for row in rows:
        # hits 全空 → 跳过
        is_hit = _row_is_hit(row)
        if is_hit is None:
            continue

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
        "by_direction": by_dir_out,
    }


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
    now = datetime.now(timezone.utc)

    windows = {
        "30d": _filter_by_window(rows, 30, now),
        "90d": _filter_by_window(rows, 90, now),
        "all": rows,
    }

    return {
        # 生成时间戳（UTC ISO）
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            name: _aggregate(subset)
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
