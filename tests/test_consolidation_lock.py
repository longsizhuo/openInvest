"""consolidation_lock 回归测试（issue #179 P1-D②）。

Dreaming 防撕裂锁 130 行并发逻辑此前零测试，历史上真出过 race（audit algo C3：
write + read-back-verify 的窗口让两个进程都认为自己拿到锁）。覆盖：
- 基本 acquire / 二次 acquire 被活 PID 拒绝
- 僵尸判定两条腿：PID 已死 / mtime 超过 HOLDER_STALE_MS
- rollback 两分支：prior=0 删文件、prior>0 倒回 mtime
- 多进程竞争：N 进程同时 acquire，恰好 1 个成功（C3 的核心不变量）
"""
from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

from openinvest.core.consolidation_lock import (
    HOLDER_STALE_MS,
    rollback_consolidation_lock,
    try_acquire_consolidation_lock,
)


def _lock_file(root: Path) -> Path:
    return root / ".dreams" / ".consolidate-lock"


def test_acquire_then_reject_live_holder(tmp_path):
    """第一次成功；同一把锁被活 PID（本进程之外的真实活进程）持有时拒绝。"""
    prior = try_acquire_consolidation_lock(tmp_path)
    assert prior is not None, "空目录首次 acquire 必须成功"
    assert _lock_file(tmp_path).read_text().strip() == str(os.getpid())

    # 模拟"另一个活进程"持锁：写入 PID 1（init，永远活着且 != 本进程）
    _lock_file(tmp_path).write_text("1")
    assert try_acquire_consolidation_lock(tmp_path) is None, "活 PID 持锁必须被拒"


def test_zombie_dead_pid_reclaimed(tmp_path):
    """持有者 PID 已死 → 视为僵尸，可重新认领。"""
    lock = _lock_file(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 起一个立即退出的子进程拿它的已死 PID（比猜一个未用 PID 更可靠）
    p = mp.Process(target=lambda: None)
    p.start()
    p.join()
    lock.write_text(str(p.pid))
    assert try_acquire_consolidation_lock(tmp_path) is not None, "死 PID 应可重新认领"
    assert lock.read_text().strip() == str(os.getpid())


def test_zombie_stale_mtime_reclaimed(tmp_path):
    """PID 活着但 mtime 超过 HOLDER_STALE_MS → 卡死僵尸，可重新认领。"""
    lock = _lock_file(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1")  # PID 1 永远活着
    stale_secs = time.time() - (HOLDER_STALE_MS / 1000 + 60)
    os.utime(lock, (stale_secs, stale_secs))
    assert try_acquire_consolidation_lock(tmp_path) is not None, \
        "超时僵尸（PID 活但 60min 无刷新）应可重新认领"


def test_rollback_prior_zero_removes_file(tmp_path):
    """首次 acquire（prior=0）失败回滚 → 锁文件删除，回到初始态。"""
    prior = try_acquire_consolidation_lock(tmp_path)
    assert prior == 0.0
    rollback_consolidation_lock(tmp_path, prior)
    assert not _lock_file(tmp_path).exists()
    # 回滚后能立刻重新 acquire
    assert try_acquire_consolidation_lock(tmp_path) is not None


def test_rollback_restores_prior_mtime(tmp_path):
    """有历史完成时间（prior>0）时回滚 → mtime 倒回，lastConsolidatedAt 语义不丢。"""
    lock = _lock_file(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 制造"上次完成于 2 小时前"的锁（超过 stale 阈值 → 可认领）
    lock.write_text("")
    old_secs = time.time() - 2 * 3600
    os.utime(lock, (old_secs, old_secs))

    prior = try_acquire_consolidation_lock(tmp_path)
    assert prior is not None and prior > 0
    rollback_consolidation_lock(tmp_path, prior)
    assert abs(lock.stat().st_mtime - old_secs) < 2, "mtime 未倒回上次完成时间"
    assert lock.read_text() == "", "PID 字段未清空"


def _worker(root_str: str, q):
    """子进程：尝试 acquire，把结果（成功与否 + 自己 PID）报回主进程。"""
    got = try_acquire_consolidation_lock(Path(root_str))
    q.put((os.getpid(), got is not None))
    if got is not None:
        time.sleep(0.5)  # 持锁装死，让其它进程在"活 PID 持有"窗口内被拒


def test_concurrent_acquire_exactly_one_winner(tmp_path):
    """C3 核心不变量：N 进程同时抢，恰好 1 个成功。"""
    n = 8
    q = mp.Queue()
    procs = [mp.Process(target=_worker, args=(str(tmp_path), q)) for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    results = [q.get(timeout=5) for _ in range(n)]
    winners = [pid for pid, ok in results if ok]
    assert len(winners) == 1, f"应恰好 1 个进程拿到锁，实际 {len(winners)}: {results}"
    assert _lock_file(tmp_path).read_text().strip() == str(winners[0])


def test_star_import_matches_all():
    """__all__ 曾列出两个从未实现的函数，import * 直接 AttributeError。"""
    import openinvest.core.consolidation_lock as m
    for name in m.__all__:
        assert hasattr(m, name), f"__all__ 含不存在的符号 {name}"
