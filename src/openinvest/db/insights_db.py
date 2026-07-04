"""insights SQLite 存储层

替代 memory/insights/*.md 散文件的结构化查询后端（渐进迁移，.md 文件继续保留供人类阅读）。

设计：
- WAL 模式 + busy_timeout，跨进程并发安全（同 db/market_store.py 风格）
- slug 作为主键（幂等 INSERT OR REPLACE）
- created_at 索引支持按时间过滤（/api/insights/fresh 用）

Schema：
  insights(slug, asset, title, body, hit_rate, sample_count, source_score, created_at)
  INDEX idx_insights_created ON insights(created_at DESC)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional
from openinvest.paths import INVEST_ROOT

# 默认路径与 market_store.py 同目录
DEFAULT_DB_PATH = str(INVEST_ROOT / "db" / "insights.db")


class InsightsDB:
    """线程安全 + WAL 模式的 insights SQLite 存储

    跨线程：check_same_thread=False + RLock
    跨进程：WAL + busy_timeout=5000
    幂等写入：INSERT OR REPLACE（slug 是 PRIMARY KEY）
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 允许跨线程访问，配合 _lock 保证安全
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self.conn.row_factory = sqlite3.Row  # 返回可按列名访问的 Row 对象

        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL 是推荐组合
        self.conn.commit()

        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        """初始化 schema（幂等）"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS insights (
                    slug          TEXT PRIMARY KEY,
                    asset         TEXT,
                    title         TEXT,
                    body          TEXT,
                    hit_rate      REAL,
                    sample_count  INTEGER,
                    source_score  REAL,
                    created_at    TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_insights_created
                ON insights(created_at DESC)
            """)
            self.conn.commit()

    # ============ 写 ============

    def upsert(
        self,
        slug: str,
        asset: Optional[str],
        title: Optional[str],
        body: Optional[str],
        hit_rate: Optional[float] = None,
        sample_count: Optional[int] = None,
        source_score: Optional[float] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """写入或更新一条 insight（slug 存在则覆盖）

        created_at 默认用当前 ISO 时间。幂等：重复 slug 会覆盖旧记录。
        """
        if not created_at:
            created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO insights
                    (slug, asset, title, body, hit_rate, sample_count, source_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (slug, asset, title, body, hit_rate, sample_count, source_score, created_at))
            self.conn.commit()

    def upsert_from_candidate(self, slug: str, candidate: Dict[str, Any], body: str) -> None:
        """从 deep_sleep 候选 dict 直接写入（dreaming.py 调用的快捷方法）

        candidate 形如 deep_sleep() 产出的 dict，包含 asset/hit_rate/count/score 等字段。
        """
        # 自动生成 title："{asset} {verdict/action} @ {regime} → {hit_rate*100:.0f}%"
        action = candidate.get("verdict") or candidate.get("action", "")
        regime_tag = "_".join(candidate.get("regime", [])) or "any"
        hit_rate = candidate.get("hit_rate")
        title = (
            f"{candidate.get('asset', slug)} / {action} / {regime_tag} "
            f"→ 命中率 {hit_rate*100:.0f}%" if hit_rate is not None else slug
        )
        self.upsert(
            slug=slug,
            asset=candidate.get("asset"),
            title=title,
            body=body,
            hit_rate=candidate.get("hit_rate"),
            sample_count=candidate.get("count"),
            source_score=candidate.get("score"),
        )

    # ============ 读 ============

    def get(self, slug: str) -> Optional[Dict[str, Any]]:
        """按 slug 查单条；不存在返回 None"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM insights WHERE slug = ?", (slug,))
            row = cursor.fetchone()
        return dict(row) if row else None

    def list_all(self, limit: int = 200) -> List[Dict[str, Any]]:
        """返回全部 insights，按 created_at 倒序，最多 limit 条"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM insights ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def list_fresh(self, since_hours: int = 48, limit: int = 5) -> List[Dict[str, Any]]:
        """返回最近 N 小时内写入的 insights，按 created_at 倒序

        对应 /api/insights/fresh 端点的核心查询，替代原来的 glob + mtime 扫描。
        """
        from datetime import timedelta, timezone
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM insights WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit)
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def list_by_hit_rate(self, min_hit_rate: float = 0.0, limit: int = 50) -> List[Dict[str, Any]]:
        """按命中率过滤，用于测试和调试"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM insights WHERE hit_rate >= ? ORDER BY hit_rate DESC LIMIT ?",
                (min_hit_rate, limit)
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """总记录数"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM insights")
            return cursor.fetchone()[0]

    def delete(self, slug: str) -> bool:
        """删除单条，返回是否真删了"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM insights WHERE slug = ?", (slug,))
            self.conn.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        """关闭连接（测试用）"""
        self.conn.close()
