"""核心数据完整性测试 — MemoryStore + atomic write + transaction RMW。

覆盖 audit 标的几个关键 path：
- atomic write: tmp + fsync + os.replace 三步走
- transaction(): commit-on-success 语义（caller 抛异常时不写半截）
- 并发 RMW: 50 线程 += 1 不丢 update（TOCTOU 修复回归）
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from core.memory_store import (
    MemoryStore,
    _atomic_write_text,
    _DocTx,
)


@pytest.fixture
def store(tmp_path):
    """临时 memory dir，避免污染真实 portfolio.md"""
    return MemoryStore(tmp_path / "memory")


def _seed_portfolio(store: MemoryStore, **fields):
    store.write("portfolio", "state", fields, "# portfolio body")


# ---------- atomic_write ----------

def test_atomic_write_basic(tmp_path):
    p = tmp_path / "x.md"
    _atomic_write_text(p, "hello\nworld")
    assert p.read_text() == "hello\nworld"


def test_atomic_write_no_tmp_leftover(tmp_path):
    """tmp 文件成功后必须被清理"""
    p = tmp_path / "x.md"
    _atomic_write_text(p, "a")
    leftovers = list(tmp_path.glob("*.tmp.*"))
    assert leftovers == []


def test_atomic_write_does_not_truncate_on_error(tmp_path, monkeypatch):
    """模拟 fsync 抛异常，原文件不应被破坏"""
    p = tmp_path / "x.md"
    _atomic_write_text(p, "original")
    real_fsync = os.fsync

    def boom(_fd):
        raise OSError("simulated disk full")
    monkeypatch.setattr(os, "fsync", boom)

    with pytest.raises(OSError):
        _atomic_write_text(p, "new content")

    # 关键：原文件内容仍是 "original"，没被截断
    assert p.read_text() == "original"
    monkeypatch.setattr(os, "fsync", real_fsync)


# ---------- transaction commit-on-success ----------

def test_transaction_commits_on_success(store):
    _seed_portfolio(store, cash_cny=100.0)
    with store.transaction("portfolio") as p:
        p["cash_cny"] = 200.0
    assert store.read("portfolio").get("cash_cny") == 200.0


def test_transaction_rollback_on_exception(store):
    """audit C2: caller 抛异常时不应 commit 半 state"""
    _seed_portfolio(store, cash_cny=100.0)
    with pytest.raises(RuntimeError):
        with store.transaction("portfolio") as p:
            p["cash_cny"] = 999999.99
            raise RuntimeError("simulated mid-tx failure")
    # 关键：cash_cny 应该回到原值
    assert store.read("portfolio").get("cash_cny") == 100.0


def test_transaction_rollback_keeps_body_intact(store):
    """改 frontmatter + body 都做了一半时，全部回滚"""
    store.write("portfolio", "state", {"cash_cny": 100.0}, "# original body")
    with pytest.raises(ValueError):
        with store.transaction("portfolio") as p:
            p["cash_cny"] = 7777.0
            p.set_body("# corrupted body")
            raise ValueError("oops")
    doc = store.read("portfolio")
    assert doc.get("cash_cny") == 100.0
    assert doc.body.strip() == "# original body"


# ---------- 并发 RMW（TOCTOU 修复回归）----------

def test_concurrent_rmw_no_lost_updates(tmp_path):
    """50 线程并发 cash_cny += 1 → 最终增量 = 50 (audit TOCTOU 修复)"""
    root = tmp_path / "memory"
    s0 = MemoryStore(root)
    s0.write("portfolio", "state", {"cash_cny": 0.0}, "")

    N = 50

    def worker():
        s = MemoryStore(root)
        with s.transaction("portfolio") as p:
            p["cash_cny"] = float(p.get("cash_cny", 0)) + 1.0

    ts = [threading.Thread(target=worker) for _ in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    final = MemoryStore(root).read("portfolio").get("cash_cny")
    assert final == float(N), f"expected {N}, got {final}"


def test_concurrent_mixed_increment_decrement(tmp_path):
    """模拟 audit 描述场景: scheduler 扣 + napcat 存交错"""
    root = tmp_path / "memory"
    MemoryStore(root).write("portfolio", "state", {"cash_cny": 1000.0}, "")

    rounds = 10
    deposit, withdraw = 50.0, 30.0

    def deposit_worker():
        s = MemoryStore(root)
        with s.transaction("portfolio") as p:
            p["cash_cny"] = float(p.get("cash_cny", 0)) + deposit

    def withdraw_worker():
        s = MemoryStore(root)
        with s.transaction("portfolio") as p:
            p["cash_cny"] = float(p.get("cash_cny", 0)) - withdraw

    threads = []
    for _ in range(rounds):
        threads.append(threading.Thread(target=deposit_worker))
        threads.append(threading.Thread(target=withdraw_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = 1000.0 + rounds * (deposit - withdraw)
    final = MemoryStore(root).read("portfolio").get("cash_cny")
    assert final == expected, f"expected {expected}, got {final}"


# ---------- _DocTx 接口 ----------

def test_doctx_dict_like_api():
    tx = _DocTx(name="x", doc=None)
    assert "k" not in tx
    tx["k"] = 1
    assert tx["k"] == 1
    assert tx.get("k") == 1
    assert tx.get("missing", "default") == "default"
    tx.update(a=2, b=3)
    assert tx["a"] == 2 and tx["b"] == 3


def test_doctx_existed_flag():
    tx_new = _DocTx(name="x", doc=None)
    assert tx_new.existed is False

    # 用真 doc 模拟"已存在"
    from core.memory_store import MemoryDoc
    doc = MemoryDoc(name="x", type="state", metadata={"k": 1}, body="body")
    tx_old = _DocTx(name="x", doc=doc)
    assert tx_old.existed is True
    assert tx_old["k"] == 1


# ---------- update_fields 也走单锁 ----------

def test_update_fields_preserves_other_fields(store):
    _seed_portfolio(store, cash_cny=100.0, ndq_shares=50.0)
    store.update_fields("portfolio", cash_cny=200.0)
    doc = store.read("portfolio")
    assert doc.get("cash_cny") == 200.0
    assert doc.get("ndq_shares") == 50.0  # 没动


def test_state_set_get_round_trip(store):
    store.state_set("processed_emails", ["id1", "id2"])
    assert store.state_get("processed_emails") == ["id1", "id2"]


def test_append_history_jsonl_roundtrip(store):
    store.append_history({"action": "bought", "amount": 100})
    store.append_history({"action": "sold", "amount": 50})
    history = store.read_history()
    assert len(history) == 2
    assert history[0]["action"] == "bought"
    assert history[1]["amount"] == 50
