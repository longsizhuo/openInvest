"""回归测试：migrate_profile.py 的 safety guard（issue #172）。

2026-07-08 事故：这个一次性迁移脚本被直接重跑，无任何保护地把
user.md/strategy.md/portfolio.md 覆盖成 user_profile.json 里的旧数据，
daily_report 因 target_assets 变空每天早退、邮件全断。
"""
from __future__ import annotations

import json

import pytest


def _write_profile(path, **overrides):
    profile = {
        "name": "Demo User",
        "risk_tolerance": "Balanced",
        "monthly_income_cny": 20000,
        "monthly_expenses_cny": 8000,
        "exchange_buffer_cny": 5000,
        "current_assets": {"cash_cny": 62000, "aud_cash": 1000.0, "ndq_shares": 50.0},
        "investment_strategy": {
            "target_allocation_stock": 0.7,
            "target_allocation_cash": 0.3,
            "max_single_invest_cny": 10000,
        },
    }
    profile.update(overrides)
    path.write_text(json.dumps(profile), encoding="utf-8")


@pytest.fixture
def _isolated_root(tmp_path, monkeypatch):
    from openinvest.core import memory_store as ms
    from openinvest import migrate_profile as mp

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(mp, "PROFILE_PATH", tmp_path / "user_profile.json")
    return tmp_path


def test_first_time_migration_succeeds(_isolated_root):
    """目标文件都不存在（真正第一次跑）→ 正常迁移，不受 guard 影响。"""
    from openinvest import migrate_profile as mp
    from openinvest.core.memory_store import MemoryStore

    _write_profile(mp.PROFILE_PATH, name="Real User")
    mp.main()

    store = MemoryStore(root=_isolated_root / "memory")
    assert store.read("user").get("display_name") == "Real User"
    assert store.read("strategy") is not None
    assert store.read("portfolio") is not None


def test_rerun_without_force_refuses_and_preserves_data(_isolated_root):
    """已经跑过一次（memory/*.md 已存在）→ 再跑必须拒绝，现有数据原样保留。"""
    from openinvest import migrate_profile as mp
    from openinvest.core.memory_store import MemoryStore

    _write_profile(mp.PROFILE_PATH, name="Real User")
    mp.main()  # 第一次：真实数据落盘

    _write_profile(mp.PROFILE_PATH, name="Demo User")  # 模拟事故：换成 stale demo profile
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        mp.main()

    store = MemoryStore(root=_isolated_root / "memory")
    assert store.read("user").get("display_name") == "Real User"  # 真实数据没被覆盖


def test_rerun_with_force_overwrites_but_backs_up_first(_isolated_root):
    """显式 force=True 才允许覆盖，且覆盖前必须先备份现有文件。"""
    from openinvest import migrate_profile as mp
    from openinvest.core.memory_store import MemoryStore

    _write_profile(mp.PROFILE_PATH, name="Real User")
    mp.main()

    _write_profile(mp.PROFILE_PATH, name="Demo User")
    mp.main(force=True)

    store = MemoryStore(root=_isolated_root / "memory")
    assert store.read("user").get("display_name") == "Demo User"  # 显式 force 才会覆盖
    backups = list((_isolated_root / "memory").glob("user.md.bak.*"))
    assert len(backups) == 1
    assert "Real User" in backups[0].read_text(encoding="utf-8")  # 备份里是覆盖前的真实数据
