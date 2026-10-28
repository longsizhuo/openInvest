"""D7：`openinvest maintenance cleanup`——*.bak.<ts> 保留策略 + 遗留 *.lock，默认 dry-run"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from openinvest import cli
from openinvest.remote_dispatch import maybe_dispatch_remote
from openinvest.services import maintenance
from openinvest.utils import oslock

NOW = 1_790_000_000.0
DAY = 86400


def _bak(root: Path, family: str, i: int, age_days: float) -> Path:
    """第 i 份备份（i 越大越新），mtime = NOW - age_days"""
    p = root / f"{family}.bak.202607{i:02d}-120000"
    p.write_text(f"{family}-{i}")
    os.utime(p, (NOW - age_days * DAY, NOW - age_days * DAY))
    return p


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    return root


def test_policy_keeps_newest_n_or_recent_union_per_family(mem):
    for i in range(1, 31):                      # portfolio.md：30 份，全部很老（30 天前）
        _bak(mem, "portfolio.md", i, age_days=30)
    for i in range(1, 4):                       # user.md：3 份很老 → 不到 keep，全留
        _bak(mem, "user.md", i, age_days=100)
    for i in range(1, 4):                       # strategy.md：25 份；序号最小的 3 份 mtime 是 2 天前（新鲜）
        _bak(mem, "strategy.md", i, age_days=2)
    for i in range(4, 26):                      # 其余 22 份 30 天前
        _bak(mem, "strategy.md", i, age_days=30)

    kept, doomed = maintenance.plan_bak(mem, keep=20, days=7, now=NOW)
    names = {p.name for p in doomed}
    # portfolio.md：只留最新 20（11..30），删最老 10 份（01..10）
    assert {n for n in names if n.startswith("portfolio.md")} == {
        f"portfolio.md.bak.202607{i:02d}-120000" for i in range(1, 11)}
    assert not any(n.startswith("user.md") for n in names)          # family 内不足 keep → 全留
    # strategy.md：按名新→旧前 20 份 = 06..25 留；04、05 老且排名 20 之外 → 删；
    # 01-03 虽排名在外，但 mtime 在 7 天内 → 留（并集）。family 之间互不影响
    assert {n for n in names if n.startswith("strategy.md")} == {
        "strategy.md.bak.20260704-120000", "strategy.md.bak.20260705-120000"}
    assert len([p for p in kept if p.name.startswith("portfolio.md")]) == 20


def test_dry_run_deletes_nothing_apply_deletes_exactly_the_planned(mem):
    for i in range(1, 31):
        _bak(mem, "portfolio.md", i, age_days=30)
    (mem / "portfolio.md").write_text("live")
    (mem / "portfolio.md.bak").write_text("无时间戳后缀，不是本工具管的", encoding="utf-8")
    (mem / "foo.bak.notatimestamp").write_text("x")

    dry = maintenance.cleanup(mem, apply=False, now=NOW)
    assert dry["mode"] == "dry-run" and dry["bak"]["to_delete"] == 10 and dry["bak"]["kept"] == 20
    assert "deleted" not in dry["bak"] and len(list(mem.glob("portfolio.md.bak.2026*"))) == 30

    done = maintenance.cleanup(mem, apply=True, now=NOW)
    assert done["mode"] == "apply" and done["bak"]["deleted"] == 10
    assert len(list(mem.glob("portfolio.md.bak.2026*"))) == 20
    assert (mem / "portfolio.md").read_text() == "live"
    assert (mem / "portfolio.md.bak").exists() and (mem / "foo.bak.notatimestamp").exists()
    # 删的是最老的 10 份
    assert not (mem / "portfolio.md.bak.20260701-120000").exists()
    assert (mem / "portfolio.md.bak.20260711-120000").exists()


def test_stale_locks_removed_but_held_lock_and_backtest_dirs_untouched(mem):
    (mem / ".state").mkdir()
    (mem / "daily").mkdir()
    (mem / ".backtest_x").mkdir()
    stale = [mem / "portfolio.md.lock", mem / ".state" / "a.json.lock", mem / "daily" / "2026-04-27.md.lock"]
    for p in stale:
        p.write_text("")
    held = mem / ".state" / "held.json.lock"
    held.write_text("")
    skipped = mem / ".backtest_x" / "y.json.lock"
    skipped.write_text("")
    (mem / ".consolidate-lock").write_text("pid")  # 不是 *.lock，不碰

    fd = os.open(held, os.O_RDWR)
    oslock.flock(fd, oslock.LOCK_EX)  # 模拟另一进程正持有
    try:
        dry = maintenance.cleanup(mem, apply=False, now=NOW)
        assert dry["lock"] == {"found": 4, "in_use": 1, "to_delete": 3}
        assert all(p.exists() for p in stale)
        done = maintenance.cleanup(mem, apply=True, now=NOW)
        assert done["lock"]["deleted"] == 3
        assert not any(p.exists() for p in stale)
        assert held.exists() and skipped.exists() and (mem / ".consolidate-lock").exists()
    finally:
        os.close(fd)


def test_foreign_lock_files_are_never_touched(mem):
    """"无人持有"的 flock 探测只对 flock 型锁成立：别的工具的"文件存在即上锁"型锁（git 的
    index.lock 等）、非 0 字节的 .lock 一概不碰，也不计入 found。"""
    (mem / "index.lock").write_text("")                      # git 风格：O_EXCL 存在即锁，无数据后缀
    (mem / "HEAD.lock").write_text("")
    (mem / "notes.txt.lock").write_text("")                  # 后缀不是 memory_store 的数据文件类型
    (mem / "x.json.lock").write_text("pid 123")              # sidecar 从不写内容；有内容的不是它
    sidecar = mem / "portfolio.md.lock"
    sidecar.write_text("")
    out = maintenance.cleanup(mem, apply=True, now=NOW)
    assert out["lock"] == {"found": 1, "in_use": 0, "deleted": 1}
    assert not sidecar.exists()
    for name in ("index.lock", "HEAD.lock", "notes.txt.lock", "x.json.lock"):
        assert (mem / name).exists(), name


def test_missing_root_reports_zero_and_creates_nothing(tmp_path):
    root = tmp_path / "nope" / "memory"
    out = maintenance.cleanup(root, apply=True)
    assert out["bak"]["found"] == 0 and out["lock"]["found"] == 0 and not root.exists()


# ---------- CLI ----------

def test_cli_defaults_to_dry_run_and_flags_are_exclusive():
    ns = cli.build_parser().parse_args(["maintenance", "cleanup"])
    assert ns.apply is False and ns.dry_run is False and ns.keep == 20 and ns.days == 7
    assert cli.build_parser().parse_args(["maintenance", "cleanup", "--apply"]).apply is True
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["maintenance", "cleanup", "--apply", "--dry-run"])


def test_cli_cmd_runs_against_memory_root_and_prints_json(mem, monkeypatch, capfd):
    from openinvest.core import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_ROOT", mem)
    for i in range(1, 26):
        _bak(mem, "portfolio.md", i, age_days=30)
    # _bak 把 mtime 设成 NOW(2026-09)-30 天，而真实 now 更晚，所以都算老
    cli.cmd_maintenance(cli.build_parser().parse_args(["maintenance", "cleanup"]))
    out = json.loads(capfd.readouterr().out)  # _print_json 直写 sys.__stdout__，capsys 抓不到
    assert out["mode"] == "dry-run" and out["bak"]["to_delete"] == 5
    assert len(list(mem.glob("portfolio.md.bak.*"))) == 25


def test_maintenance_stays_local_in_remote_mode(monkeypatch):
    monkeypatch.setenv("INVEST_API_BASE", "https://hub.example")
    ns = cli.build_parser().parse_args(["maintenance", "cleanup"])
    assert maybe_dispatch_remote(ns) is False  # 本地白名单：不被转发、也不报"未支持远端"
