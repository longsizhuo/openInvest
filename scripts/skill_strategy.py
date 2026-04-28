"""Skill helper: 策略 + 长期洞察一站式输出"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402


def main():
    store = MemoryStore()
    strategy_doc = store.read("strategy")
    insights_dir = store.root / "insights"

    insights = []
    if insights_dir.exists():
        for f in sorted(insights_dir.glob("*.md")):
            doc = store.read(f"insights/{f.stem}")
            if doc:
                insights.append({
                    "slug": f.stem,
                    "asset": doc.get("asset"),
                    "action": doc.get("action"),
                    "regime": doc.get("regime"),
                    "window_days": doc.get("window_days"),
                    "count": doc.get("count"),
                    "hit_rate": doc.get("hit_rate"),
                    "avg_return_pct": doc.get("avg_return_pct"),
                    "score": doc.get("score"),
                })

    out = {
        "strategy": dict(strategy_doc.metadata) if strategy_doc else None,
        "long_term_insights": insights,
        "insights_count": len(insights),
        "note": "长期洞察由每天凌晨 3 点的 Dreaming Deep Sleep 写入。"
                "数据量太少时为空。",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
