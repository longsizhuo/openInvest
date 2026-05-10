"""db/insights_db.py 单元测试

覆盖：
- 写读往返（upsert + get）
- list_fresh 按时间过滤
- 50 条插入后按 hit_rate 排序（list_by_hit_rate）
- upsert_from_candidate 快捷写入
- 幂等：重复 slug 覆盖
- 跨线程安全（50 线程并发写不丢）
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db.insights_db import InsightsDB


@pytest.fixture
def db(tmp_path):
    """临时路径的 InsightsDB，测试完自动关闭"""
    db_path = str(tmp_path / "test_insights.db")
    _db = InsightsDB(db_path=db_path)
    yield _db
    _db.close()


# ============ 基础写读 ============

class TestBasicReadWrite:
    def test_upsert_and_get_roundtrip(self, db):
        """写入后能按 slug 读回，所有字段保持一致"""
        db.upsert(
            slug="ndq_bought_vix_low_7d",
            asset="NDQ.AX",
            title="NDQ vix_low 买入 7d 命中率 80%",
            body="# body content",
            hit_rate=0.8,
            sample_count=10,
            source_score=0.85,
            created_at="2026-05-10T03:00:00+08:00",
        )
        row = db.get("ndq_bought_vix_low_7d")
        assert row is not None
        assert row["asset"] == "NDQ.AX"
        assert row["hit_rate"] == pytest.approx(0.8)
        assert row["sample_count"] == 10
        assert row["source_score"] == pytest.approx(0.85)
        assert "body content" in row["body"]

    def test_get_missing_returns_none(self, db):
        """不存在的 slug 返回 None 不抛异常"""
        assert db.get("nonexistent_slug") is None

    def test_upsert_idempotent(self, db):
        """同一 slug 写两次，第二次覆盖（不新增）"""
        db.upsert(slug="dup", asset="A", title="v1", body="body1",
                  created_at="2026-05-10T00:00:00+00:00")
        db.upsert(slug="dup", asset="B", title="v2", body="body2",
                  created_at="2026-05-10T01:00:00+00:00")
        assert db.count() == 1
        row = db.get("dup")
        assert row["asset"] == "B"
        assert row["title"] == "v2"

    def test_count(self, db):
        """count() 返回实际行数"""
        assert db.count() == 0
        db.upsert(slug="a", asset="X", title="t", body="b",
                  created_at="2026-05-10T00:00:00+00:00")
        db.upsert(slug="b", asset="Y", title="t", body="b",
                  created_at="2026-05-10T00:00:00+00:00")
        assert db.count() == 2

    def test_delete(self, db):
        """delete 成功返回 True，不存在返回 False"""
        db.upsert(slug="to_del", asset="X", title="t", body="b",
                  created_at="2026-05-10T00:00:00+00:00")
        assert db.delete("to_del") is True
        assert db.get("to_del") is None
        assert db.delete("to_del") is False  # 已不存在


# ============ list_fresh 时间过滤 ============

class TestListFresh:
    def _make_ts(self, hours_ago: float) -> str:
        """生成 N 小时前的 ISO 时间戳"""
        t = datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
        return t.isoformat(timespec="seconds")

    def test_fresh_within_window(self, db):
        """1 小时内的 insight 应被 list_fresh(since_hours=48) 返回"""
        db.upsert(slug="fresh", asset="A", title="t", body="b",
                  created_at=self._make_ts(1.0))
        rows = db.list_fresh(since_hours=48)
        slugs = [r["slug"] for r in rows]
        assert "fresh" in slugs

    def test_stale_excluded(self, db):
        """72 小时前的 insight 不应被 list_fresh(since_hours=48) 返回"""
        db.upsert(slug="stale", asset="A", title="t", body="b",
                  created_at=self._make_ts(72.0))
        rows = db.list_fresh(since_hours=48)
        slugs = [r["slug"] for r in rows]
        assert "stale" not in slugs

    def test_limit_respected(self, db):
        """list_fresh(limit=3) 最多返回 3 条"""
        for i in range(10):
            db.upsert(slug=f"fresh_{i}", asset="A", title="t", body="b",
                      created_at=self._make_ts(float(i) * 0.1))
        rows = db.list_fresh(since_hours=48, limit=3)
        assert len(rows) <= 3

    def test_ordered_by_created_at_desc(self, db):
        """list_fresh 结果按 created_at 倒序"""
        db.upsert(slug="early", asset="A", title="t", body="b",
                  created_at=self._make_ts(5.0))
        db.upsert(slug="later", asset="A", title="t", body="b",
                  created_at=self._make_ts(1.0))
        rows = db.list_fresh(since_hours=48)
        slugs = [r["slug"] for r in rows]
        assert slugs.index("later") < slugs.index("early")


# ============ 50 条插入按 hit_rate 排序 ============

class TestBulkAndSort:
    def test_50_inserts_sorted_by_hit_rate(self, db):
        """插入 50 条不同 hit_rate，list_by_hit_rate 结果确实按 hit_rate 降序"""
        import random
        slugs_hr = []
        random.seed(42)
        for i in range(50):
            hr = round(random.uniform(0.4, 1.0), 3)
            slug = f"insight_{i:03d}"
            db.upsert(slug=slug, asset="A", title=f"t{i}", body="b",
                      hit_rate=hr, created_at=f"2026-05-10T00:{i:02d}:00+00:00")
            slugs_hr.append((slug, hr))

        assert db.count() == 50

        rows = db.list_by_hit_rate(limit=50)
        assert len(rows) == 50

        # 验证严格降序（允许相邻相等）
        for a, b in zip(rows, rows[1:]):
            assert a["hit_rate"] >= b["hit_rate"], (
                f"排序错误: {a['slug']}({a['hit_rate']}) > {b['slug']}({b['hit_rate']})"
            )

    def test_50_inserts_min_hit_rate_filter(self, db):
        """list_by_hit_rate(min_hit_rate=0.8) 只返回 hit_rate >= 0.8 的条目"""
        for i in range(50):
            hr = i * 0.02  # 0.00, 0.02, ..., 0.98
            db.upsert(slug=f"item_{i:03d}", asset="A", title="t", body="b",
                      hit_rate=hr, created_at=f"2026-05-10T00:{i:02d}:00+00:00")

        rows = db.list_by_hit_rate(min_hit_rate=0.8, limit=50)
        assert all(r["hit_rate"] >= 0.8 for r in rows)
        # hit_rate >= 0.8: i=40..49 → 10 条
        assert len(rows) == 10


# ============ upsert_from_candidate ============

class TestUpsertFromCandidate:
    def test_basic_candidate_write(self, db):
        """upsert_from_candidate 能把 deep_sleep 产出写成完整行"""
        candidate = {
            "asset": "GC=F",
            "action": "bought",
            "regime": ["vix_low", "tnx_mid"],
            "window_days": 30,
            "count": 6,
            "hit_rate": 0.833,
            "avg_return_pct": 2.5,
            "score": 0.85,
        }
        body = "# insight body"
        db.upsert_from_candidate("gc_f_bought_vix_low_tnx_mid_30d", candidate, body)

        row = db.get("gc_f_bought_vix_low_tnx_mid_30d")
        assert row is not None
        assert row["asset"] == "GC=F"
        assert row["hit_rate"] == pytest.approx(0.833)
        assert row["sample_count"] == 6
        assert row["source_score"] == pytest.approx(0.85)
        assert "命中率" in row["title"]  # title 自动生成的

    def test_candidate_title_auto_generated(self, db):
        """title 为空时自动从 asset/action/regime/hit_rate 生成"""
        candidate = {
            "asset": "NDQ.AX",
            "action": "sold",
            "regime": ["vix_high"],
            "window_days": 7,
            "count": 4,
            "hit_rate": 0.75,
            "avg_return_pct": -1.2,
            "score": 0.82,
        }
        db.upsert_from_candidate("ndq_sold_vix_high_7d", candidate, "body")
        row = db.get("ndq_sold_vix_high_7d")
        # title 应含 NDQ.AX 和命中率字样
        assert "NDQ.AX" in row["title"]
        assert "75" in row["title"]  # 75% 命中率


# ============ 并发安全 ============

class TestConcurrentWrites:
    def test_50_threads_concurrent_upsert_no_crash(self, db):
        """50 线程并发写不同 slug，全部成功（不丢不崩）"""
        errors = []

        def worker(i: int):
            try:
                db.upsert(
                    slug=f"concurrent_{i:03d}",
                    asset="A",
                    title=f"thread {i}",
                    body="body",
                    hit_rate=float(i) / 100,
                    created_at=f"2026-05-10T00:{i % 60:02d}:00+00:00",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发写入出错: {errors}"
        assert db.count() == 50
