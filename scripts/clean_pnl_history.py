"""清理 memory/.state/pnl_history.jsonl 的噪声数据点。

何时跑：
- 调试 jobs.pnl_snapshot 留下了非交易时段的 entry
- 同一天多次跑导致密集采样（折线图上变成一段水平线噪声）

清理规则（按顺序应用）：
1. 同一日期保留最后一条（按 ts 排序）—— 让每天只有一个数据点
2. 删除非交易时段的 entry（北京时间周末 / 凌晨 0-9 点）

跑法：
  python -m scripts.clean_pnl_history --dry-run    # 看会删什么
  python -m scripts.clean_pnl_history              # 实际执行
  python -m scripts.clean_pnl_history --keep-all-days   # 只删非交易时段，不去重
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"

# 北京时间偏移
TZ_BEIJING = timezone(timedelta(hours=8))


def _parse_ts(ts: str) -> datetime:
    """容忍 +00:00 / +08:00 / 无时区"""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # 末位是 Z 就替换
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _is_trading_window_beijing(dt: datetime) -> bool:
    """是否为'合法采样时间'（只删凌晨噪声，周末 16:00 是 backfill 用前一
    交易日填充的合法点，要保留以让折线连续）。

    判断标准：北京时间小时 ∈ [9, 23] 就算合法。
    凌晨 0-8 点跑出来的（手动调试 / 跨时区误差）一律视为噪声。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    bj = dt.astimezone(TZ_BEIJING)
    return 9 <= bj.hour <= 23


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只 print，不实际写入")
    parser.add_argument("--keep-all-days", action="store_true",
                        help="不做同日去重，只删非交易时段")
    args = parser.parse_args()

    if not HISTORY_PATH.exists():
        print(f"❌ {HISTORY_PATH} 不存在")
        sys.exit(1)

    entries: List[Dict] = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    original_count = len(entries)
    print(f"📥 读取 {original_count} 条 entry")

    # 1. 先删非交易时段（凌晨噪声）—— 必须在去重之前，否则同日去重可能
    #    保留凌晨那条而把白天合法采样删掉
    after_filter: List[Dict] = []
    removed_nontrading: List[Dict] = []
    for e in entries:
        if _is_trading_window_beijing(_parse_ts(e["ts"])):
            after_filter.append(e)
        else:
            removed_nontrading.append(e)
    if removed_nontrading:
        print(f"  非交易时段: -{len(removed_nontrading)} 条")
        for r in removed_nontrading[:5]:
            print(f"    drop: {r['ts']}")
        if len(removed_nontrading) > 5:
            print(f"    ... +{len(removed_nontrading) - 5} more")

    # 2. 同日去重（保留最后一条合法采样）
    if not args.keep_all_days:
        by_date: Dict[str, Dict] = {}
        for e in sorted(after_filter, key=lambda x: x["ts"]):
            date_key = _parse_ts(e["ts"]).astimezone(TZ_BEIJING).strftime("%Y-%m-%d")
            by_date[date_key] = e  # 后写覆盖，最终保留最新
        keep = sorted(by_date.values(), key=lambda x: x["ts"])
        removed_dup = len(after_filter) - len(keep)
        if removed_dup > 0:
            print(f"  同日去重: -{removed_dup} 条")
    else:
        keep = after_filter

    final_count = len(keep)
    print(f"📤 最终保留 {final_count} 条 (删 {original_count - final_count})")

    if args.dry_run:
        print("\n[--dry-run] 未实际写入。去掉 --dry-run 才会落盘。")
        return

    # 备份原文件
    backup = HISTORY_PATH.with_suffix(".jsonl.bak")
    HISTORY_PATH.rename(backup)
    print(f"💾 原文件备份到 {backup.name}")

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for e in keep:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"✅ 写入 {final_count} 条到 {HISTORY_PATH}")
    print(f"\n下一步：python -m jobs.pnl_snapshot  重新渲染 SVG")


if __name__ == "__main__":
    main()
