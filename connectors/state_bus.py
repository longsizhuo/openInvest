"""connectors/state_bus.py — 单例状态总线

职责：
- 持有 _status_locks（按 task_id 隔离的 threading.Lock dict）
- 提供 _write_committee_status / _read_committee_status 原子 I/O helper
- LRU 淘汰：超过 maxsize=500 时删最老的 key，防内存无限增长

为什么要单独一个模块：
  web_api.py 之前把这三块全放在模块全局区，未来把路由拆出去时
  每个子模块 import web_api 会得到不同的 _status_locks 实例，
  导致 SSE 订阅者和 committee task 看到的状态不一致。
  改成这里的单例 import 后，所有子模块共享同一个 dict。

使用：
    from connectors.state_bus import write_committee_status, read_committee_status
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("state_bus")

# committee task 状态文件存放目录（与 web_api.COMMITTEE_DIR 对齐）
_COMMITTEE_DIR = Path(__file__).parent.parent / "memory" / ".committee"

# ──────────────────────────────────────────────────────────────
# 内部 LRU lock dict
# ──────────────────────────────────────────────────────────────

# 最多保留 500 个 task_id 的锁；超过时按 LRU 淘汰最老的
_LOCK_MAXSIZE: int = 500

# 外层锁：保护 _status_locks 这个 OrderedDict 本身（插入 / 淘汰）
_meta_lock = threading.Lock()

# OrderedDict 同时充当 LRU：最近使用的在末尾，淘汰时删 first key
_status_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()


def _get_status_lock(task_id: str) -> threading.Lock:
    """按 task_id 取（或新建）独立 Lock，并刷新其 LRU 位置。

    超过 _LOCK_MAXSIZE 时驱逐最旧的 key（LRU 淘汰），
    被驱逐 key 若还有线程持有那把锁也不影响正确性——
    对应的 task 早就结束了，新的请求会为 task_id 创建新锁。
    """
    with _meta_lock:
        lk = _status_locks.get(task_id)
        if lk is None:
            # 容量检查：超限则驱逐最早加入的 key
            if len(_status_locks) >= _LOCK_MAXSIZE:
                evicted_key, _ = _status_locks.popitem(last=False)
                log.debug("state_bus: 驱逐过期 task_id lock: %s", evicted_key)
            lk = threading.Lock()
            _status_locks[task_id] = lk
        else:
            # 已存在：移到末尾（标记为最近使用）
            _status_locks.move_to_end(task_id)
        return lk


# ──────────────────────────────────────────────────────────────
# 公开 helper：原子读 / 写 status.json
# ──────────────────────────────────────────────────────────────

def _status_path(task_id: str) -> Path:
    return _COMMITTEE_DIR / task_id / "status.json"


def write_committee_status(task_id: str, payload: Dict[str, Any]) -> None:
    """原子写 status.json（按 task_id 加 threading.Lock 防多线程混写）

    用 pid + thread id 组成唯一 tmp 文件名，atomic rename 保证磁盘原子性。
    """
    path = _status_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(
        f".json.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    with _get_status_lock(task_id):
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def read_committee_status(task_id: str) -> Optional[Dict[str, Any]]:
    """读 status.json；文件不存在返回 None，JSON 损坏记 error 日志后返回 None"""
    path = _status_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("committee status %s 损坏: %s", task_id, e)
        return None


# ──────────────────────────────────────────────────────────────
# 供测试用的 reset 入口（不对外 API）
# ──────────────────────────────────────────────────────────────

def _reset_for_test() -> None:
    """清空 _status_locks（仅测试用）"""
    with _meta_lock:
        _status_locks.clear()


def _current_size() -> int:
    """返回当前 _status_locks dict 大小（仅测试用）"""
    with _meta_lock:
        return len(_status_locks)
