"""Skill helper: 最近交易 + 最近辩论裁决"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=10, help="返回数量")
    args = parser.parse_args()

    store = MemoryStore()
    trades = store.read_history()[-args.n:]

    debate_dir = store.root / ".debate"
    debates = []
    if debate_dir.exists():
        for date_dir in sorted(debate_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for md in date_dir.glob("*.md"):
                content = md.read_text(encoding="utf-8")
                # 抽取 verdict 行（前 6 行通常）
                first_lines = "\n".join(content.splitlines()[:8])
                debates.append({"date": date_dir.name, "asset": md.stem,
                                "summary": first_lines})
            if len(debates) >= args.n:
                break

    out = {
        "recent_trades": trades,
        "recent_debates": debates[:args.n],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
