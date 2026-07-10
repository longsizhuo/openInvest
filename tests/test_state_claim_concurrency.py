"""state_claim / state_unclaim 多进程竞争测试（issue #179 P1-D③）。

ADR-016 账本幂等闸的地基原语：任何"累加语义 + 可重放触发"的账本写入
（HTTP 重试 / 邮件轮询 / cron 重跑 / agent 重发 / 双击）都靠 state_claim
保证恰好入账一次。此前只有单进程语义测试，fcntl 锁的跨进程互斥零覆盖——
这里用真实 multiprocessing 验证核心不变量：

1. N 进程同时 claim 同一 key ⇒ 恰好 1 个拿到 True
2. N 进程各 claim 不同 key ⇒ 全部 True 且文件不丢 key（无覆盖写）
3. claim → unclaim → claim 可重试（回滚语义）
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

from openinvest.core.memory_store import MemoryStore


def _claim_same(root_str: str, q):
    store = MemoryStore(root=Path(root_str))
    q.put(store.state_claim("processed_emails", "email-42"))


def test_concurrent_claim_same_key_exactly_one_winner(tmp_path):
    """ADR-016 核心：同一去重键被 N 进程同时 claim，恰好 1 次成功。"""
    n = 10
    q = mp.Queue()
    procs = [mp.Process(target=_claim_same, args=(str(tmp_path), q)) for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    results = [q.get(timeout=5) for _ in range(n)]
    assert sum(results) == 1, f"应恰好 1 个进程 claim 成功，实际 {sum(results)}/{n}"

    data = json.loads((tmp_path / ".state" / "processed_emails.json").read_text())
    assert data == ["email-42"], f"落盘应恰好一条，实际 {data}"


def _claim_own(root_str: str, idx: int, q):
    store = MemoryStore(root=Path(root_str))
    q.put((idx, store.state_claim("processed_emails", f"email-{idx}")))


def test_concurrent_claim_distinct_keys_no_lost_update(tmp_path):
    """N 进程各 claim 不同 key：全部成功且一个不丢（read-modify-write 不互相覆盖）。"""
    n = 10
    q = mp.Queue()
    procs = [mp.Process(target=_claim_own, args=(str(tmp_path), i, q)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    results = dict(q.get(timeout=5) for _ in range(n))
    assert all(results.values()), f"不同 key 应全部 claim 成功: {results}"

    data = json.loads((tmp_path / ".state" / "processed_emails.json").read_text())
    assert sorted(data) == sorted(f"email-{i}" for i in range(n)), \
        f"并发写互相覆盖丢了 key: {sorted(data)}"


def test_unclaim_allows_retry(tmp_path):
    """claim 后失败回滚（unclaim）→ 同 key 可再次 claim（ADR-016 重试语义）。"""
    store = MemoryStore(root=tmp_path)
    assert store.state_claim("processed_emails", "email-7") is True
    assert store.state_claim("processed_emails", "email-7") is False  # 幂等拒绝
    store.state_unclaim("processed_emails", "email-7")
    assert store.state_claim("processed_emails", "email-7") is True  # 回滚后可重试
    # 坏文件容错：手工写坏成非 list → claim 视为空表重建，不抛
    (tmp_path / ".state" / "corrupt.json").write_text('{"not": "a list"}')
    assert store.state_claim("corrupt", "x") is True
