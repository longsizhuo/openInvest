#!/usr/bin/env python3
"""scripts/snapshot.py — hub 权威状态一键快照 / 恢复（确定性，无 LLM）。

为什么存在：openInvest 的"钱"散在两个 gitignore 的 store 里——
  memory/portfolio.md（当前持仓 + 现金） + db/trades.db（交易账本）——
再加历史决议（memory/.committee 等）。迁移 hub 到新机器、或做机外冷备时，
这些必须一致地一起搬，否则丢的是真金白银和九个月交易史。本脚本把它们打成
单个 tar.gz，新机一条命令拉起。

    uv run python -m scripts.snapshot snapshot [--out FILE]
    uv run python -m scripts.snapshot restore --in FILE [--force] [--target DIR]

可重拉的东西（market_data.db / events.db / chroma / jobs / cache_data / static）
故意不进 snapshot——新机首跑自动重建。分层定义见下方常量，叙述见
docs/wiki/08-deployment.md §7 / §9。

环境凭据（.env）不打进 snapshot（避免把密钥塞进备份）；manifest 只列出
新机需要填哪些 key（名字，不含值）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 路径走单一可信源（跟着代码走，别写死）
try:
    from openinvest.core.memory_store import MEMORY_ROOT
except Exception:  # pragma: no cover - 仓库未装依赖时退回约定路径
    MEMORY_ROOT = REPO_ROOT / "memory"
try:
    from openinvest.db.trades_db import DB_PATH as _TRADES_DB_PATH
    TRADES_DB_PATH = Path(_TRADES_DB_PATH)
except Exception:  # pragma: no cover
    TRADES_DB_PATH = REPO_ROOT / "db" / "trades.db"

MEMORY_ROOT = Path(MEMORY_ROOT)
DB_ROOT = TRADES_DB_PATH.parent

SCHEMA_VERSION = 1

# ── 分层：什么进 snapshot，什么不进 ────────────────────────────────────────
# Tier 1 — 钱与配置：无法重建，丢了等于丢钱 / 丢风险偏好。
TIER1_FILES = ["portfolio.md", "user.md", "strategy.md", "MEMORY.md", "DREAMS.md"]
TIER1_DIRS = [".state"]
TIER1_SQLITE = ["trades.db"]  # WAL 库，走 sqlite online backup
# Tier 2 — 历史决议 / 洞察：理论可重生，但要烧大量 LLM token，值得搬。
TIER2_DIRS = [".committee", "insights", "daily", ".dreams"]
TIER2_SQLITE = ["insights.db"]
# Tier 3 — 可重拉，不进 snapshot：market_data.db / events.db / chroma.sqlite3 /
#          jobs.sqlite / cache_data/ / static/ / memory/.backtest*。


def _die(msg: str, hint: str = "") -> None:
    out = {"status": "error", "error": msg}
    if hint:
        out["hint"] = hint
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(1)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_online_backup(src: Path, dst: Path) -> None:
    """WAL 安全的整库拷贝——输出单个干净 .db（无需 -wal/-shm）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _env_keys_needed() -> list[str]:
    """从 .env.example 抓 key 名（不含值），告诉新机要配哪些凭据。"""
    example = REPO_ROOT / ".env.example"
    keys: list[str] = []
    if example.exists():
        for line in example.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key and key.isupper() and key not in keys:
                keys.append(key)
    return keys


def _dir_stats(root: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)。"""
    n = total = 0
    for p in root.rglob("*"):
        if p.is_file():
            n += 1
            total += p.stat().st_size
    return n, total


def _guard_is_hub() -> None:
    import os

    if os.getenv("INVEST_API_BASE", "").strip():
        _die(
            "当前是远端客户端（INVEST_API_BASE 已设），本机没有权威账本",
            "去 hub 那台机器上跑 snapshot；客户端的 memory/ 本就不存在。",
        )


# ── snapshot ──────────────────────────────────────────────────────────────
def cmd_snapshot(args: argparse.Namespace) -> None:
    _guard_is_hub()

    portfolio = MEMORY_ROOT / "portfolio.md"
    if not portfolio.exists():
        _die(
            f"找不到 {portfolio}（hub 还没 init？）",
            "先在本机 run.sh init 建账本，再 snapshot。",
        )

    created_at = datetime.now(timezone.utc).isoformat()
    out = Path(args.out) if args.out else (
        Path.cwd() / f"invest-snapshot-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.tar.gz"
    )

    manifest: dict = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "host": socket.gethostname(),
        "repo_root": str(REPO_ROOT),
        "tier1_files": {},   # name -> {bytes, sha256}
        "tier1_sqlite": {},  # name -> {bytes, sha256}
        "tier2_dirs": {},    # name -> {files, bytes}
        "tier2_sqlite": {},
        "env_keys_needed": _env_keys_needed(),
    }

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        stage_mem = stage / "memory"
        stage_db = stage / "db"
        stage_mem.mkdir(parents=True)
        stage_db.mkdir(parents=True)

        # Tier 1 文件（含 sha256 完整性校验）
        for name in TIER1_FILES:
            src = MEMORY_ROOT / name
            if src.is_file():
                shutil.copy2(src, stage_mem / name)
                manifest["tier1_files"][name] = {
                    "bytes": src.stat().st_size,
                    "sha256": _sha256(src),
                }

        # Tier 1 目录
        for name in TIER1_DIRS:
            src = MEMORY_ROOT / name
            if src.is_dir():
                shutil.copytree(src, stage_mem / name)

        # Tier 1 SQLite（online backup）
        for name in TIER1_SQLITE:
            src = DB_ROOT / name
            if src.is_file():
                dst = stage_db / name
                _sqlite_online_backup(src, dst)
                manifest["tier1_sqlite"][name] = {
                    "bytes": dst.stat().st_size,
                    "sha256": _sha256(dst),
                }

        # Tier 2 目录（历史，记数不逐文件 sha）
        for name in TIER2_DIRS:
            src = MEMORY_ROOT / name
            if src.is_dir():
                shutil.copytree(src, stage_mem / name)
                n, total = _dir_stats(src)
                manifest["tier2_dirs"][name] = {"files": n, "bytes": total}

        # Tier 2 SQLite
        for name in TIER2_SQLITE:
            src = DB_ROOT / name
            if src.is_file():
                dst = stage_db / name
                _sqlite_online_backup(src, dst)
                manifest["tier2_sqlite"][name] = {"bytes": dst.stat().st_size}

        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            tar.add(stage, arcname=".")

    size = out.stat().st_size
    print(json.dumps({
        "status": "ok",
        "action": "snapshot",
        "out": str(out),
        "bytes": size,
        "human": f"{size / 1e6:.1f} MB",
        "tier1_files": list(manifest["tier1_files"]),
        "tier1_sqlite": list(manifest["tier1_sqlite"]),
        "tier2_dirs": manifest["tier2_dirs"],
        "env_keys_needed": manifest["env_keys_needed"],
        "restore_hint": "新机：clone + uv sync，然后 "
                        "uv run python -m scripts.snapshot restore --in <此文件>",
    }, ensure_ascii=False, indent=2))


# ── restore ─────────────────────────────────────────────────────────────────
def cmd_restore(args: argparse.Namespace) -> None:
    src_tar = Path(args.in_file)
    if not src_tar.is_file():
        _die(f"找不到快照文件 {src_tar}")

    target = Path(args.target).resolve() if args.target else REPO_ROOT
    target_portfolio = target / "memory" / "portfolio.md"

    if target_portfolio.exists() and not args.force:
        _die(
            f"目标已有账本 {target_portfolio}，拒绝覆盖",
            "确认要覆盖再加 --force（强烈建议先停服 "
            "`sudo systemctl stop invest-web invest-scheduler` 并给现状留个 snapshot）。",
        )

    with tempfile.TemporaryDirectory() as tmp:
        ex = Path(tmp)
        with tarfile.open(src_tar, "r:gz") as tar:
            tar.extractall(ex, filter="data")  # filter=data 防路径穿越

        manifest_path = ex / "manifest.json"
        if not manifest_path.exists():
            _die("快照缺 manifest.json，可能不是本工具产出的文件")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # 落地：memory/ 与 db/ 原样铺回 target
        restored: list[str] = []
        warnings: list[str] = []
        for sub in ("memory", "db"):
            src_dir = ex / sub
            if not src_dir.is_dir():
                continue
            dst_dir = target / sub
            dst_dir.mkdir(parents=True, exist_ok=True)
            for item in src_dir.iterdir():
                dst = dst_dir / item.name
                # SQLite：先清掉目标的陈旧 -wal/-shm，避免和新主库不一致
                if item.suffix == ".db":
                    for stale in (dst.with_name(dst.name + "-wal"),
                                  dst.with_name(dst.name + "-shm")):
                        if stale.exists():
                            stale.unlink()
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)
                restored.append(f"{sub}/{item.name}")

        # 完整性校验：Tier1 文件 + sqlite 比对 manifest 的 sha256
        for name, meta in {**manifest.get("tier1_files", {})}.items():
            f = target / "memory" / name
            if f.exists() and "sha256" in meta and _sha256(f) != meta["sha256"]:
                warnings.append(f"memory/{name} sha256 不符（拷贝可能损坏）")
        for name, meta in {**manifest.get("tier1_sqlite", {})}.items():
            f = target / "db" / name
            if f.exists() and "sha256" in meta and _sha256(f) != meta["sha256"]:
                warnings.append(f"db/{name} sha256 不符（拷贝可能损坏）")

    print(json.dumps({
        "status": "ok" if not warnings else "ok_with_warnings",
        "action": "restore",
        "from_snapshot": {
            "created_at": manifest.get("created_at"),
            "host": manifest.get("host"),
        },
        "target": str(target),
        "restored": restored,
        "warnings": warnings,
        "env_keys_needed": manifest.get("env_keys_needed", []),
        "next": [
            "在 target 填好 .env（上面 env_keys_needed 列的 key）",
            "uv sync --frozen --python 3.13",
            "sudo systemctl start invest-web invest-scheduler（或 run.sh doctor 验证）",
        ],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(prog="snapshot", description="hub 权威状态快照 / 恢复")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="打包权威状态成 tar.gz")
    sp.add_argument("--out", help="输出文件路径（默认 ./invest-snapshot-<时间>.tar.gz）")
    sp.set_defaults(func=cmd_snapshot)

    rp = sub.add_parser("restore", help="从 tar.gz 还原")
    rp.add_argument("--in", dest="in_file", required=True, help="快照 tar.gz 路径")
    rp.add_argument("--force", action="store_true", help="覆盖目标已有账本")
    rp.add_argument("--target", help="还原到哪个仓库目录（默认本仓库；调试时可指向临时目录）")
    rp.set_defaults(func=cmd_restore)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
