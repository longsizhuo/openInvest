"""Memory 存储层 - 仿 OpenClaw 风格的 markdown + frontmatter 持久化

设计原则：
- frontmatter 是结构化数据的 source of truth（代码读写）
- body 是给 LLM 看的自然语言版本（每次写入时由模板重新生成）
- 文件锁保证并发安全（multi-thread agent 同时跑也不会撕裂）
- daily/*.md 是 append-only 日志，dreaming 用它做长期记忆整合
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import frontmatter

MEMORY_ROOT = Path(__file__).parent.parent / "memory"


@contextmanager
def _file_lock(path: Path):
    """fcntl 排它锁 - 跨线程/进程都安全"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写入：写到同目录的临时文件 → fsync → os.replace 到目标路径。

    防止进程在写到一半时被 kill / OOM / 断电导致 path 被截断为半截文件
    （audit 发现的 P0 数据完整性硬伤）。要求：
    - tmp 必须和目标在同一文件系统/同一目录，rename 才能保证原子（POSIX）
    - rename 前必须 fsync 到磁盘，否则元数据可能比 inode 数据先落盘
    - 调用方仍需要在 _file_lock 内调用以序列化并发写
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 用 pid 区分多进程并发，虽然外面有 fcntl 但保险一点
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # 出错把 tmp 清掉，避免 *.tmp.<pid> 文件堆积
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _now_iso() -> str:
    """本地时区 ISO 8601 时间戳"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class MemoryDoc:
    """一份 memory 文件的内存表示"""
    name: str
    type: str
    metadata: Dict[str, Any]   # frontmatter 全量（含 name/type/updated/业务字段）
    body: str                   # markdown 正文（自然语言）

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)


class _DocTx:
    """transaction() 闭包内 yield 给调用方的可变 doc 视图。

    调用方可以像操作 dict 一样改 metadata，调用 set_body() 改 body；
    退出 with 时由 MemoryStore.transaction() 一次性原子写回。
    """

    __slots__ = ("name", "type", "metadata", "body", "_existed")

    def __init__(self, name: str, doc: Optional[MemoryDoc]):
        self.name = name
        if doc is not None:
            self.type = doc.type
            self.metadata = dict(doc.metadata)
            self.body = doc.body
            self._existed = True
        else:
            # 文件不存在时 transaction 内仍可写：相当于在锁内创建
            self.type = "state"
            self.metadata = {}
            self.body = ""
            self._existed = False

    # dict-like API（read）
    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.metadata[key]

    def __contains__(self, key: str) -> bool:
        return key in self.metadata

    # 写
    def __setitem__(self, key: str, value: Any) -> None:
        self.metadata[key] = value

    def update(self, **kw: Any) -> None:
        self.metadata.update(kw)

    def set_type(self, type_: str) -> None:
        self.type = type_

    def set_body(self, body: str) -> None:
        self.body = body

    @property
    def existed(self) -> bool:
        """transaction 进来时该文件是否已存在"""
        return self._existed


class MemoryStore:
    """memory/ 目录的读写门面"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else MEMORY_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 普通 markdown 文档（user / strategy / portfolio / insights/*） ----------

    def path_of(self, name: str) -> Path:
        """name 支持 'portfolio' 或 'insights/risk_calibration' 这种带子目录的形式"""
        return self.root / f"{name}.md"

    # 内部 helper：不加锁的 read/write，给 transaction() 闭包用，避免锁内套锁
    def _read_unlocked(self, path: Path, name: str) -> Optional[MemoryDoc]:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)
        return MemoryDoc(
            name=post.metadata.get("name", name),
            type=post.metadata.get("type", "unknown"),
            metadata=dict(post.metadata),
            body=post.content,
        )

    def _write_unlocked(
        self, path: Path, name: str, type_: str, data: Dict[str, Any], body: str
    ) -> None:
        meta = {"name": name, "type": type_, "updated": _now_iso(), **data}
        post = frontmatter.Post(body, **meta)
        _atomic_write_text(path, frontmatter.dumps(post))

    def read(self, name: str) -> Optional[MemoryDoc]:
        """读取 memory 文档；不存在返回 None"""
        path = self.path_of(name)
        with _file_lock(path):
            return self._read_unlocked(path, name)

    def write(self, name: str, type_: str, data: Dict[str, Any], body: str) -> Path:
        """写入 memory 文档（覆盖式）

        data: frontmatter 字段（不含 name/type/updated，会自动注入）
        body: markdown 正文
        """
        path = self.path_of(name)
        with _file_lock(path):
            self._write_unlocked(path, name, type_, data, body)
        return path

    def update_fields(self, name: str, **fields) -> Optional[MemoryDoc]:
        """局部更新 frontmatter 字段；不动 body。

        关键修复（audit P1: TOCTOU）：
        旧版是 "self.read() + self.write()" 两把分离的锁，中间任意进程能插
        进来，造成 Lost Update（NapCat 的存款被 scheduler 的扣款覆盖）。
        现在改成单一 _file_lock 闭包内 read-modify-write，并发写不会丢。
        """
        path = self.path_of(name)
        with _file_lock(path):
            doc = self._read_unlocked(path, name)
            if doc is None:
                return None
            new_data = {
                k: v for k, v in doc.metadata.items()
                if k not in {"name", "type", "updated"}
            }
            new_data.update(fields)
            self._write_unlocked(path, doc.name, doc.type, new_data, doc.body)
            # 锁内再读一次，返回 caller 看到最终落盘状态
            return self._read_unlocked(path, name)

    @contextmanager
    def transaction(self, name: str):
        """RMW 安全闭包：read → 调用方修改 → 退出时 atomic write，全程持锁。

        给"读两个字段联动写"或"先改 frontmatter 再重渲染 body"这类多步操作用：

            with store.transaction("portfolio") as p:
                p["cash_cny"] = float(p.get("cash_cny", 0)) - 6894
                p["ndq_shares"] = float(p.get("ndq_shares", 0)) + 128
                p.set_body(render_body(p))
            # 退出 with 时自动一次性 atomic write

        相比 update_fields() 的优势：调用方可以在锁内做任意复杂的派生计算，
        不需要把多个 update_fields() 串起来（每串一次都开/关一次锁，期间还
        是可能被插入）。

        如果 doc 不存在，返回的 _DocTx 仍然可写，等于"在锁内创建新文件"。

        **commit-on-success 语义**（audit C2 修复）：caller 在 with 块内抛异常
        时，已经修改的 tx.metadata / tx.body **不会**被写入。这避免了"frontmatter
        改了一半 + body 没重渲染"的撕裂状态被持久化。锁仍在 finally 中释放。
        """
        path = self.path_of(name)
        with _file_lock(path):
            doc = self._read_unlocked(path, name)
            tx = _DocTx(name=name, doc=doc)
            try:
                yield tx
            except BaseException:
                # caller 抛异常 → 放弃所有修改，让异常向上传播
                raise
            # 只有 yield 块正常结束才 commit
            meta_clean = {
                k: v for k, v in tx.metadata.items()
                if k not in {"name", "type", "updated"}
            }
            self._write_unlocked(path, name, tx.type, meta_clean, tx.body)

    # ---------- daily/*.md - append-only 日志 ----------

    def append_daily(self, section: str, content: str, date: Optional[str] = None) -> Path:
        """往今天的 daily/<date>.md 追加一段内容

        section: 段落标题（## section）
        content: markdown 内容
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        path = self.root / "daily" / f"{date}.md"
        with _file_lock(path):
            is_new = not path.exists()
            with open(path, "a", encoding="utf-8") as f:
                if is_new:
                    f.write(f"---\nname: daily-{date}\ntype: log\ndate: {date}\n---\n\n")
                    f.write(f"# Daily Log {date}\n\n")
                ts = datetime.now().strftime("%H:%M:%S")
                f.write(f"## {section} ({ts})\n\n{content}\n\n")
        return path

    def list_daily(self, since_days: int = 30) -> List[Path]:
        """返回最近 N 天的 daily 文件路径（按日期升序）"""
        daily_dir = self.root / "daily"
        if not daily_dir.exists():
            return []
        files = sorted(daily_dir.glob("*.md"))
        return files[-since_days:] if len(files) > since_days else files

    # ---------- .dreams/ - dreaming 子系统私有空间 ----------

    def dream_event(self, event: Dict[str, Any]) -> None:
        """append-only 审计日志：light/REM/deep 各阶段都往这里写"""
        path = self.root / ".dreams" / "events.jsonl"
        with _file_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": _now_iso(), **event}, ensure_ascii=False) + "\n")

    def write_dream_state(self, name: str, data: Dict[str, Any]) -> Path:
        """写 .dreams/<name>.json（短期记忆 / 候选池）"""
        path = self.root / ".dreams" / f"{name}.json"
        with _file_lock(path):
            _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def read_dream_state(self, name: str) -> Optional[Dict[str, Any]]:
        path = self.root / ".dreams" / f"{name}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- .state/ - 简单的持久化 KV（已处理邮件 ID 等） ----------

    def state_get(self, name: str, default: Any = None) -> Any:
        path = self.root / ".state" / f"{name}.json"
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def state_set(self, name: str, value: Any) -> Path:
        path = self.root / ".state" / f"{name}.json"
        with _file_lock(path):
            _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))
        return path

    # ---------- portfolio_history.jsonl - append-only 交易流水 ----------

    def append_history(self, trade: Dict[str, Any]) -> None:
        path = self.root / "portfolio_history.jsonl"
        with _file_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": _now_iso(), **trade}, ensure_ascii=False) + "\n")

    def read_history(self) -> List[Dict[str, Any]]:
        path = self.root / "portfolio_history.jsonl"
        if not path.exists():
            return []
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
