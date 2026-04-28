"""一次性 / 周更：从外部源拉取所有基准数据到 memory/.state/benchmarks/ 缓存。

跑法：
  python -m scripts.refresh_benchmarks                 # 全量 refresh
  python -m scripts.refresh_benchmarks --key 沪深300   # 单独刷一个

设计：
- 拉取窗口：(BACKFILL_DAYS 天前) → 今天，与 backfill_pnl_history 对齐
- 失败的基准跳过 + print 错误，不 abort
- 缓存到 memory/.state/benchmarks/ 受 .gitignore 保护，原始净值不入 git

建议作为 weekly cron job 跑（基金净值每周更新即可）：
  0 3 * * 1  python -m scripts.refresh_benchmarks
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.benchmarks import BENCHMARKS, refresh_benchmark  # noqa: E402

BACKFILL_DAYS = 60  # 与 scripts/backfill_pnl_history.py 对齐


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default=None,
                        help="只刷某个 benchmark（默认全部刷）")
    args = parser.parse_args()

    end = datetime.now().date()
    start = end - timedelta(days=BACKFILL_DAYS)
    start_str, end_str = start.isoformat(), end.isoformat()

    keys = [args.key] if args.key else list(BENCHMARKS.keys())
    print(f"📡 Refresh {len(keys)} 个 benchmark ({start_str} → {end_str})...")

    ok = 0
    for key in keys:
        if key not in BENCHMARKS:
            print(f"  ❌ 未知 key: {key}")
            continue
        print(f"  → {key} ({BENCHMARKS[key]['source']})...", end=" ", flush=True)
        result = refresh_benchmark(key, start_str, end_str)
        if result is None:
            print("FAILED")
        else:
            n = len(result["prices"])
            print(f"OK ({n} points)")
            ok += 1

    print(f"\n✅ {ok}/{len(keys)} 个基准刷新成功")


if __name__ == "__main__":
    main()
