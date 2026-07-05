"""tests/test_state_bus.py — connectors/state_bus.py 单元测试

覆盖：
- LRU 淘汰：500 个 key 顺序进 → 第 501 个进来后第 1 个被驱逐
- 第 500 个 key 不会被驱逐（刚好在容量边界）
- 已存在的 key get_status_lock 不会驱逐自己
- write_committee_status / read_committee_status 基础路径
- read 文件不存在返回 None
"""
from __future__ import annotations

import json

import pytest

import openinvest.connectors.state_bus as sb


@pytest.fixture(autouse=True)
def reset_state_bus():
    """每个测试前后清空 _status_locks，避免测试间状态污染"""
    sb._reset_for_test()
    yield
    sb._reset_for_test()


# ---------- LRU 淘汰 ----------

def test_lru_eviction_at_501():
    """第 501 个 task_id 进来后，第 1 个（最老）应被驱逐"""
    first_key = "task-000001"
    # 塞满 500 个（含 first_key）
    sb._get_status_lock(first_key)
    for i in range(2, 501):
        sb._get_status_lock(f"task-{i:06d}")

    # 此时正好 500，first_key 还在
    assert sb._current_size() == 500

    # 第 501 个进来 → first_key 被驱逐
    sb._get_status_lock("task-000501")

    assert sb._current_size() == 500  # 总量仍是 500
    # 直接访问内部 dict 确认 first_key 已消失
    import openinvest.connectors.state_bus as _sb_internal
    with _sb_internal._meta_lock:
        assert first_key not in _sb_internal._status_locks


def test_lru_no_eviction_at_500():
    """刚好 500 个 key 时不触发淘汰"""
    for i in range(1, 501):
        sb._get_status_lock(f"task-{i:06d}")
    assert sb._current_size() == 500


def test_lru_existing_key_no_eviction():
    """已存在的 key 重复 get 只做 move_to_end，不驱逐、不新增"""
    sb._get_status_lock("existing")
    for _ in range(10):
        sb._get_status_lock("existing")  # 同一个 key 重复 get
    assert sb._current_size() == 1


def test_lru_eviction_evicts_oldest_not_newest():
    """连续加 501 个时，被驱逐的应该是第 1 个，而不是第 500 / 501 个"""
    for i in range(1, 502):
        sb._get_status_lock(f"task-{i:06d}")
    import openinvest.connectors.state_bus as _sb_internal
    with _sb_internal._meta_lock:
        assert "task-000001" not in _sb_internal._status_locks
        assert "task-000501" in _sb_internal._status_locks


# ---------- write / read ----------

def test_write_read_roundtrip(tmp_path, monkeypatch):
    """write_committee_status 写入后 read_committee_status 能读回"""
    # 把 COMMITTEE_DIR 重定向到 tmp_path，避免污染真实 memory/
    monkeypatch.setattr(sb, "_COMMITTEE_DIR", tmp_path / ".committee")

    payload = {"status": "running", "phase": "macro", "events": []}
    sb.write_committee_status("test-task-001", payload)
    result = sb.read_committee_status("test-task-001")

    assert result is not None
    assert result["status"] == "running"
    assert result["phase"] == "macro"


def test_read_not_found(tmp_path, monkeypatch):
    """status.json 不存在时 read 返回 None"""
    monkeypatch.setattr(sb, "_COMMITTEE_DIR", tmp_path / ".committee")
    assert sb.read_committee_status("nonexistent-task") is None


def test_read_corrupt_json(tmp_path, monkeypatch):
    """status.json 损坏时 read 返回 None（不抛异常）"""
    monkeypatch.setattr(sb, "_COMMITTEE_DIR", tmp_path / ".committee")
    task_dir = tmp_path / ".committee" / "corrupt-task"
    task_dir.mkdir(parents=True)
    (task_dir / "status.json").write_text("{ invalid json <<<", encoding="utf-8")

    assert sb.read_committee_status("corrupt-task") is None


def test_write_is_atomic(tmp_path, monkeypatch):
    """write 后文件内容完整，无 .tmp 残留"""
    monkeypatch.setattr(sb, "_COMMITTEE_DIR", tmp_path / ".committee")
    payload = {"hello": "world", "nested": {"x": 42}}
    sb.write_committee_status("atomic-test", payload)

    status_path = tmp_path / ".committee" / "atomic-test" / "status.json"
    assert status_path.exists()
    # 确认无 .tmp 残留
    tmp_files = list(status_path.parent.glob("*.tmp*"))
    assert tmp_files == []
    # 确认内容正确
    loaded = json.loads(status_path.read_text(encoding="utf-8"))
    assert loaded["hello"] == "world"
