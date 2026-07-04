"""把现有 memory/insights/*.md 一次性导入 SQLite（db/insights.db）

用法：
    cd /home/ubuntu/projects-review/invest
    python -m scripts.migrate_insights_to_sqlite

设计：
- 幂等：slug 已存在则 INSERT OR REPLACE（覆盖），可多次执行
- 不删 .md 文件：渐进迁移，.md 继续作为人类可读副本
- 失败单条跳过，记录到 stderr，不中断整体

输出示例：
    扫描 memory/insights/：共 12 个 .md 文件
    导入: ndq_ax_bought_vix_low_7d (score=0.85, hit_rate=0.80, count=5)
    ...
    完成：12 成功 / 0 失败
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore
from openinvest.db.insights_db import InsightsDB


def migrate(
    store: MemoryStore | None = None,
    db: InsightsDB | None = None,
    dry_run: bool = False,
) -> dict:
    """主迁移函数，返回 {success, failed, skipped} 统计

    Args:
        store: MemoryStore 实例（None 则用默认路径）
        db: InsightsDB 实例（None 则用默认路径）
        dry_run: True 时只扫描不写入
    """
    store = store or MemoryStore()
    db = db or InsightsDB()
    insights_dir = store.root / "insights"

    if not insights_dir.exists():
        print(f"目录不存在: {insights_dir}，跳过", file=sys.stderr)
        return {"success": 0, "failed": 0, "skipped": 0}

    md_files = sorted(insights_dir.glob("*.md"))
    print(f"扫描 {insights_dir}：共 {len(md_files)} 个 .md 文件")
    if dry_run:
        print("  [dry_run=True] 仅预览，不写入 SQLite")

    success = 0
    failed = 0
    skipped = 0

    for md_file in md_files:
        slug = md_file.stem
        doc = store.read(f"insights/{slug}")
        if not doc:
            print(f"  跳过（读取失败）: {slug}", file=sys.stderr)
            skipped += 1
            continue

        meta = doc.metadata or {}
        hit_rate = meta.get("hit_rate")
        sample_count = meta.get("count") or meta.get("sample_count")
        source_score = meta.get("score") or meta.get("source_score")
        asset = meta.get("asset")
        # title 从 body 第一行提取
        title = ""
        for line in (doc.body or "").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                title = stripped[:200]
                break
        # created_at 优先用 frontmatter 里的 updated，否则用文件 mtime
        created_at = str(meta.get("updated") or "").strip()
        if not created_at:
            try:
                from datetime import datetime
                mtime = md_file.stat().st_mtime
                created_at = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                from datetime import datetime
                created_at = datetime.now().astimezone().isoformat(timespec="seconds")

        print(
            f"  {'[dry_run] ' if dry_run else ''}导入: {slug}"
            f" (score={source_score}, hit_rate={hit_rate}, count={sample_count})"
        )

        if not dry_run:
            try:
                db.upsert(
                    slug=slug,
                    asset=asset,
                    title=title or slug,
                    body=doc.body,
                    hit_rate=float(hit_rate) if hit_rate is not None else None,
                    sample_count=int(sample_count) if sample_count is not None else None,
                    source_score=float(source_score) if source_score is not None else None,
                    created_at=created_at,
                )
                success += 1
            except Exception as e:
                print(f"  SQLite 写入失败 {slug}: {e}", file=sys.stderr)
                failed += 1
        else:
            success += 1  # dry_run 算预览成功

    print(f"\n完成：{success} 成功 / {failed} 失败 / {skipped} 跳过")
    return {"success": success, "failed": failed, "skipped": skipped}


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    result = migrate(dry_run=dry_run)
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
