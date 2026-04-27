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


class MemoryStore:
    """memory/ 目录的读写门面"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else MEMORY_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 普通 markdown 文档（user / strategy / portfolio / insights/*） ----------

    def path_of(self, name: str) -> Path:
        """name 支持 'portfolio' 或 'insights/risk_calibration' 这种带子目录的形式"""
        return self.root / f"{name}.md"

    def read(self, name: str) -> Optional[MemoryDoc]:
        """读取 memory 文档；不存在返回 None"""
        path = self.path_of(name)
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

    def write(self, name: str, type_: str, data: Dict[str, Any], body: str) -> Path:
        """写入 memory 文档（覆盖式）

        data: frontmatter 字段（不含 name/type/updated，会自动注入）
        body: markdown 正文
        """
        path = self.path_of(name)
        meta = {"name": name, "type": type_, "updated": _now_iso(), **data}
        post = frontmatter.Post(body, **meta)
        with _file_lock(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        return path

    def update_fields(self, name: str, **fields) -> Optional[MemoryDoc]:
        """局部更新 frontmatter 字段；不动 body"""
        doc = self.read(name)
        if doc is None:
            return None
        new_data = {k: v for k, v in doc.metadata.items() if k not in {"name", "type", "updated"}}
        new_data.update(fields)
        self.write(doc.name, doc.type, new_data, doc.body)
        return self.read(name)

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
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
            with open(path, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
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
