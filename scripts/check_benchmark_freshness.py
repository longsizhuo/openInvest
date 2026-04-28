"""检查基准数据陈旧度，提醒手动更新。

重点是 AI 投顾类基准（Wealthfront / Betterment / 蚂蚁帮你投）—— 这几个的
"年化收益率"是通过一次性网络搜索写死在 core/benchmarks.py 里的，会过时。
core/benchmarks.py 给每条带 `_meta.retrieved` 字段记录搜索日期。

跑法：
  python -m scripts.check_benchmark_freshness            # 默认 90 天阈值
  python -m scripts.check_benchmark_freshness --days 60  # 自定义阈值

退出码：
  0 = 全部新鲜（可作为 cron 静默通过）
  1 = 至少一条超过阈值（可让 cron 把 stderr 转邮件 / Slack 提醒）

建议加到 monthly cron：
  0 9 1 * *  python -m scripts.check_benchmark_freshness || echo "...请手动更新"
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.benchmarks import BENCHMARKS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="超过 N 天未更新就告警（默认 90）")
    args = parser.parse_args()

    today = datetime.now().date()
    cutoff = today - timedelta(days=args.days)

    stale: list = []
    fresh: list = []
    no_meta: list = []  # 没 _meta 字段的（指数/基金/常数储蓄）—— 跳过

    for key, config in BENCHMARKS.items():
        meta = config.get("_meta")
        if not meta:
            no_meta.append(key)
            continue
        retrieved_str = meta.get("retrieved")
        if not retrieved_str:
            no_meta.append(key)
            continue
        try:
            retrieved = datetime.strptime(retrieved_str, "%Y-%m-%d").date()
        except ValueError:
            print(f"⚠️ {key}: _meta.retrieved 格式异常 ({retrieved_str})，跳过")
            continue
        age = (today - retrieved).days
        if retrieved < cutoff:
            stale.append((key, retrieved_str, age, meta.get("source_url", "")))
        else:
            fresh.append((key, retrieved_str, age))

    # 报告
    print(f"📅 今天 {today}，陈旧阈值 {args.days} 天")
    print(f"   {len(BENCHMARKS)} 条基准：{len(fresh)} 新鲜 / {len(stale)} 陈旧 / {len(no_meta)} 无需检查（自动 API 拉取）")

    if fresh:
        print("\n✅ 新鲜：")
        for key, dt, age in fresh:
            print(f"   {key}  (retrieved {dt}, age {age} 天)")

    if stale:
        print(f"\n⚠️ 陈旧（请手动重新搜索更新数据）：")
        for key, dt, age, url in stale:
            print(f"\n   • {key}")
            print(f"     上次 retrieved: {dt} ({age} 天前)")
            if url:
                print(f"     来源 URL: {url}")
            print(f"     更新方式：")
            print(f"       1. 用 WebSearch 重搜该产品最新公开年化数据")
            print(f"       2. 改 core/benchmarks.py 的 apr_pct + _meta.retrieved")
            print(f"       3. python -m scripts.refresh_benchmarks --key '{key}'")
        sys.exit(1)
    else:
        print("\n🎉 所有 AI 投顾 / 手动维护的基准都在新鲜窗口内。")
        sys.exit(0)


if __name__ == "__main__":
    main()
