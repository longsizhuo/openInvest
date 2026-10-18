"""D7 运维加固：_file_lock 用后即删 + 三套 jsonl append 统一到 memory_store 的 sidecar flock

守的行为：
- sidecar `*.lock` 用后删除（生产 memory/ 曾积 158 个）；
- 删除不破坏互斥：已 open 旧 inode 并阻塞在 flock 的等待者拿到锁后必须核对 inode、重来
  （否则等待者与新建同名文件的第三方同时进临界区 → lost update）；线程 + 多进程双验证；
- append_jsonl 行格式与历史写法逐字一致；线程/进程并发追加大于 PIPE_BUF 的行也不撕裂；
- llm_telemetry（原无锁）与 decision_ledger（原直接 flock 数据文件）改走同一把锁。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from openinvest.core import llm_telemetry
from openinvest.core.decision_ledger import record_execution
from openinvest.core.memory_store import MemoryStore, _file_lock, append_jsonl

# 子进程脚本：对计数文件做 N 次 read-modify-write（不加锁的话必丢更新）
_RMW = """
import sys
from pathlib import Path
from openinvest.core.memory_store import _file_lock
p = Path(sys.argv[1]); n = int(sys.argv[2])
for _ in range(n):
    with _file_lock(p):
        v = int(p.read_text() or 0) if p.exists() else 0
        p.write_text(str(v + 1))
"""

_APPEND = """
import sys
from pathlib import Path
from openinvest.core.memory_store import append_jsonl
p = Path(sys.argv[1]); tag = sys.argv[2]; n = int(sys.argv[3])
for i in range(n):
    append_jsonl(p, {"tag": tag, "i": i, "pad": "é" * 6000})
"""


def _leftover_locks(d: Path):
    return sorted(x.name for x in d.rglob("*.lock"))


def test_sidecar_removed_after_use_and_on_exception(tmp_path):
    target = tmp_path / "state" / "x.json"
    with _file_lock(target):
        assert (tmp_path / "state" / "x.json.lock").exists()
    assert _leftover_locks(tmp_path) == []
    try:
        with _file_lock(target):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _leftover_locks(tmp_path) == []


def test_threads_no_lost_update_and_no_leftover(tmp_path):
    p = tmp_path / "counter.txt"
    p.write_text("0")

    def work():
        for _ in range(60):
            with _file_lock(p):
                p.write_text(str(int(p.read_text()) + 1))

    ts = [threading.Thread(target=work) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert p.read_text() == str(16 * 60)
    assert _leftover_locks(tmp_path) == []


def test_processes_and_threads_mixed_no_lost_update(tmp_path):
    """跨进程是 unlink 竞态真正会咬人的地方：多进程 + 本进程多线程一起抢同一把锁。"""
    p = tmp_path / "counter.txt"
    procs = [subprocess.Popen([sys.executable, "-c", _RMW, str(p), "150"]) for _ in range(4)]

    def work():
        for _ in range(150):
            with _file_lock(p):
                v = int(p.read_text() or 0) if p.exists() else 0
                p.write_text(str(v + 1))

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(pr.wait(120) == 0 for pr in procs)
    assert p.read_text() == str(8 * 150)
    assert _leftover_locks(tmp_path) == []


# ---------- append_jsonl ----------

def test_append_jsonl_line_format_is_byte_identical_to_legacy(tmp_path):
    p = tmp_path / ".state" / "a.jsonl"
    rec = {"ts": "2026-10-18T08:00:00+08:00", "name": "黄金", "n": 1, "x": None, "f": 0.5}
    append_jsonl(p, rec)
    append_jsonl(p, {"k": Path("/tmp/x")}, default=str)  # tool_calls 的 default=str 写法
    legacy = (json.dumps(rec, ensure_ascii=False) + "\n"
              + json.dumps({"k": Path("/tmp/x")}, ensure_ascii=False, default=str) + "\n")
    assert p.read_bytes() == legacy.encode("utf-8")
    assert _leftover_locks(tmp_path) == []


def test_memory_store_jsonl_writers_keep_ts_prefix_and_format(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.dream_event({"phase": "light", "msg": "中文"})
    store.append_history({"symbol": "GC=F", "units": 1})
    ev = json.loads((store.root / ".dreams" / "events.jsonl").read_text(encoding="utf-8"))
    hi = json.loads((store.root / "portfolio_history.jsonl").read_text(encoding="utf-8"))
    assert list(ev)[:1] == ["ts"] and ev["msg"] == "中文"
    assert list(hi)[:1] == ["ts"] and hi["symbol"] == "GC=F"
    assert "中文" in (store.root / ".dreams" / "events.jsonl").read_text(encoding="utf-8")  # ensure_ascii=False
    assert _leftover_locks(store.root) == []


def test_append_jsonl_concurrent_threads_and_processes_no_torn_lines(tmp_path):
    """每行 ~12KB（> PIPE_BUF=4096）：无锁追加会交错撕裂；sidecar flock 下每行完整。"""
    p = tmp_path / "usage.jsonl"
    procs = [subprocess.Popen([sys.executable, "-c", _APPEND, str(p), f"p{k}", "25"]) for k in range(3)]

    def work(k):
        for i in range(25):
            append_jsonl(p, {"tag": f"t{k}", "i": i, "pad": "é" * 6000})

    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(pr.wait(120) == 0 for pr in procs)
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]  # 撕裂行会在这里抛
    assert len(rows) == (3 + 4) * 25
    assert sorted((r["tag"], r["i"]) for r in rows) == sorted(
        (f"{pre}{k}", i) for pre, n in (("p", 3), ("t", 4)) for k in range(n) for i in range(25))
    assert _leftover_locks(tmp_path) == []


# ---------- 两个原先纪律不同的调用方 ----------

def test_llm_telemetry_appends_under_sidecar_lock(tmp_path, monkeypatch):
    tel = tmp_path / ".state" / "llm_usage.jsonl"
    calls = tmp_path / ".state" / "tool_calls.jsonl"
    monkeypatch.setattr(llm_telemetry, "TELEMETRY_FILE", tel)
    monkeypatch.setattr(llm_telemetry, "TOOL_CALLS_FILE", calls)
    meta = llm_telemetry.TelemetryMeta(agent_role="macro", asset="GC=F")

    def work():
        for _ in range(20):
            llm_telemetry.record_llm_call(meta, input_tokens=10, output_tokens=5, latency_ms=3)
            llm_telemetry.record_tool_call(meta, tool_name="t", arguments={"p": Path("/x")},
                                           result_preview="ok", latency_ms=1)

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(llm_telemetry.read_telemetry(since=10_000)) == 160
    assert len(llm_telemetry.read_tool_calls(since=10_000)) == 160
    first = json.loads(tel.read_text(encoding="utf-8").splitlines()[0])
    assert first["agent_role"] == "macro" and first["total_tokens"] == 15
    assert _leftover_locks(tmp_path) == []


def test_record_execution_idempotent_under_concurrency_and_leaves_no_lock(tmp_path):
    p = tmp_path / ".dreams" / "executions.jsonl"
    ts = [threading.Thread(target=record_execution, args=("2026-10-18/GC=F", True),
                           kwargs={"reason": "r", "path": p}) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["decision_id"] == "2026-10-18/GC=F"
    record_execution("2026-10-18/GC=F", False, reason="改主意", path=p)  # 内容变了 → 追加
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2
    assert _leftover_locks(tmp_path) == []
