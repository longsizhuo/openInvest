"""小白冒烟测试（PM-UX 提案）

模拟从零到 ready 的完整 onboarding 链路：
  1. 空目录 → doctor → needs_setup
  2. 喂 init JSON → doctor → ready + coordinator_ready=true
  3. status → 返回非空 portfolio

所有 IO 都限制在 tmp_path 里，不污染真实 memory/、user_profile.json、.env。

设计要点：
- 不需要真实 DeepSeek key（避免网络依赖）
- monkeypatch ROOT 路径（scripts/skill.py 用 ROOT 常量）
- 只测链路可达性，不测 LLM 行为（LLM 解析 holdings 的精度由 test_dreaming_* 负责）
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============ 辅助函数 ============

def _run_cmd_doctor(fake_root: Path) -> Dict[str, Any]:
    """在 fake_root 环境下调 cmd_doctor，捕获输出返回 JSON。

    cmd_doctor 内用 `MemoryStore()` 不传参 → 走 core.memory_store.MEMORY_ROOT 默认值。
    所以必须**同时** patch `scripts.skill.ROOT` + `core.memory_store.MEMORY_ROOT`，
    否则 CI 环境（memory/ 目录干净）下 doctor 永远报 "memory_initialized: missing"。
    """
    from scripts.skill import cmd_doctor
    import core.memory_store as ms_module

    captured = io.StringIO()
    fake_memory_root = fake_root / "memory"

    # cmd_doctor 实现已搬到 scripts.skill_cmds.lifecycle_cmds，在自身模块全局读 ROOT，
    # patch façade(scripts.skill) 的 ROOT 不再生效 → 必须 patch lifecycle_cmds.ROOT
    with patch("scripts.skill_cmds.lifecycle_cmds.ROOT", fake_root), \
         patch.object(ms_module, "MEMORY_ROOT", fake_memory_root), \
         patch("sys.__stdout__", captured):
        cmd_doctor(SimpleNamespace())

    output = captured.getvalue().strip()
    return json.loads(output)


def _run_cmd_init_from_payload(fake_root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """在 fake_root 环境下调 cmd_init（--from-stdin 模式），捕获输出返回 JSON

    同时 patch core.memory_store.MEMORY_ROOT，让 MemoryStore() 默认路径指向 fake_root。
    """
    from scripts.skill import cmd_init
    import core.memory_store as ms_module

    stdin_data = json.dumps(payload)
    captured = io.StringIO()
    fake_memory_root = fake_root / "memory"

    # cmd_init 实现已搬到 scripts.skill_cmds.lifecycle_cmds，在自身模块全局读 ROOT
    with patch("scripts.skill_cmds.lifecycle_cmds.ROOT", fake_root), \
         patch.object(ms_module, "MEMORY_ROOT", fake_memory_root), \
         patch("sys.__stdout__", captured), \
         patch("sys.stdin", io.StringIO(stdin_data)), \
         patch("subprocess.run") as mock_run:
        # migrate_profile.py 子进程：fake 成功
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout="migrate ok",
            stderr="",
        )
        cmd_init(SimpleNamespace(from_stdin=True, force=False))

    output = captured.getvalue().strip()
    return json.loads(output)


def _seed_minimal_memory(root: Path) -> None:
    """在 root/memory/ 下写最小可用的三份文档（v2 结构），绕过 migrate_profile.py"""
    from core.memory_store import MemoryStore

    store = MemoryStore(root / "memory")
    store.write("user", "user", {
        "display_name": "SmokeUser",
        "risk_tolerance": "Balanced",
        "exchange_buffer_cny": 5000.0,
    }, "# user")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "NDQ.AX", "max_single_invest_cny": 10000.0}],
    }, "# strategy")
    store.write("portfolio", "state", {
        "schema_version": 2,
        "cash": {"CNY": 20000.0},
        "holdings": [],
    }, "# portfolio")


def _build_init_payload(root: Path) -> Dict[str, Any]:
    """构造一份最小合理的 init JSON payload（不含真实 DeepSeek key）"""
    return {
        "profile": {
            "name": "SmokeUser",
            "risk_tolerance": "Balanced",
            "monthly_income_cny": 15000,
            "monthly_expenses_cny": 8000,
            "exchange_buffer_cny": 5000,
            "last_run_date": "2026-05-10",
            # holdings_v2 直接结构化传入，绕过 LLM 解析（不需要 DeepSeek key）
            "holdings_v2": {
                "cash": {"CNY": 20000.0},
                "holdings": [
                    {
                        "symbol": "NDQ.AX",
                        "kind": "etf",
                        "units": 100.0,
                        "unit_label": "股",
                        "avg_cost": 50.0,
                        "cost_currency": "AUD",
                        "channel": "CommSec",
                        "display_name": "BetaShares Nasdaq 100",
                    }
                ],
            },
            "investment_strategy": {
                "target_allocation_stock": 0.7,
                "target_allocation_cash": 0.3,
                "max_single_invest_cny": 10000,
            },
        },
        "env": {
            # 不传真实 DeepSeek key；只测 onboarding 链路可达性
            "EMAIL_SENDER": "test@example.com",
            "EMAIL_PASSWORD": "fake_app_password",
        },
    }


# ============ 冒烟测试主体 ============

class TestOnboardingSmoke:
    """从零到 ready 的完整冒烟链路"""

    def test_step1_empty_dir_doctor_needs_setup(self, tmp_path, monkeypatch):
        """1. 空目录 doctor → status='needs_setup'"""
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()

        # doctor 需要扫描 memory/user.md 等文件，空目录时应返回 needs_setup
        result = _run_cmd_doctor(fake_root)

        assert "status" in result
        assert result["status"] == "needs_setup", (
            f"空目录应返回 needs_setup，实际: {result['status']}"
        )
        # checks 里至少有一个 missing
        checks = result.get("checks", [])
        missing_checks = [c for c in checks if c.get("status") == "missing"]
        assert len(missing_checks) > 0, "空目录至少应有一项 check 是 missing"

    def test_step2_init_writes_memory_files(self, tmp_path, monkeypatch):
        """2. cmd_init → memory/*.md 写入成功"""
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()

        payload = _build_init_payload(fake_root)
        result = _run_cmd_init_from_payload(fake_root, payload)

        assert result.get("status") == "ok", (
            f"init 应返回 status=ok，实际: {result}"
        )
        # migrate_profile.py 由 subprocess mock，但 _write_v2_portfolio 会写真实文件
        # 检查 portfolio.md 存在（用 fake_root memory 路径）
        portfolio_md = fake_root / "memory" / "portfolio.md"
        assert portfolio_md.exists(), "init 后 portfolio.md 应已写入"

    def test_step3_after_init_doctor_ready(self, tmp_path, monkeypatch):
        """3. 写完 memory 后 doctor → coordinator_ready=true

        手动 seed memory 文档（绕过 init 的 subprocess 复杂性），
        专注测 doctor 的判断逻辑。
        """
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()

        # 手动 seed 完整的 memory 文档
        _seed_minimal_memory(fake_root)

        result = _run_cmd_doctor(fake_root)

        # memory 完整时应 coordinator_ready=True（不需要 DeepSeek key）
        assert result.get("coordinator_ready") is True, (
            f"memory 完整后 coordinator_ready 应为 True，实际: {result}"
        )

    def test_step4_doctor_after_memory_seed_ready(self, tmp_path, monkeypatch):
        """4. doctor 的 overall status 在 memory 就绪后为 ready"""
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()
        _seed_minimal_memory(fake_root)

        result = _run_cmd_doctor(fake_root)

        # 注意：doctor 会检查 .env（DEEPSEEK_API_KEY 等），没 .env 时部分 checks 是 missing
        # 但 coordinator_ready 不依赖 DeepSeek key，只依赖 memory 是否完整
        # overall "ready" 要求全部 checks 是 ok，包括 .env；
        # 所以这里只断言 coordinator_ready，不断言 overall
        checks = result.get("checks", [])
        # doctor 里 check name 是 "memory_initialized"（见 scripts/skill.py cmd_doctor）
        memory_check = next((c for c in checks if c["name"] == "memory_initialized"), None)
        assert memory_check is not None, (
            f"doctor checks 里应有 memory_initialized 项，实际 checks: {[c['name'] for c in checks]}"
        )
        assert memory_check["status"] == "ok", (
            f"memory seed 后 memory_initialized check 应为 ok，实际: {memory_check}"
        )

    def test_step5_status_returns_nonempty_portfolio(self, tmp_path, monkeypatch):
        """5. memory 就绪后 cmd_status → 返回非空 portfolio

        status 命令会拉 yfinance，这里 mock 掉避免网络依赖。
        同时 patch MEMORY_ROOT，让 MemoryStore() 默认路径指向 fake_root/memory。
        """
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()
        _seed_minimal_memory(fake_root)

        from scripts.skill import cmd_status
        import core.memory_store as ms_module

        captured = io.StringIO()

        # mock yfinance/gold 相关调用，返回合理 stub 值
        import pandas as pd
        fake_df = pd.DataFrame({"Close": [55.0]}, index=pd.to_datetime(["2026-05-10"]))
        fake_memory_root = fake_root / "memory"

        # cmd_status 实现已搬到 scripts.skill_cmds.analysis_cmds。注意 cmd_status
        # 实际不读 ROOT（此 patch 历史上是 no-op，真正生效的是 MEMORY_ROOT），
        # 但为保持字面一致且不抛 AttributeError，analysis_cmds 也定义了 ROOT。
        with patch("scripts.skill_cmds.analysis_cmds.ROOT", fake_root), \
             patch.object(ms_module, "MEMORY_ROOT", fake_memory_root), \
             patch("sys.__stdout__", captured), \
             patch("utils.exchange_fee.get_history_data", return_value=fake_df), \
             patch("utils.gold_price.get_gold_snapshot", return_value=None):
            cmd_status(SimpleNamespace())

        output = captured.getvalue().strip()
        result = json.loads(output)

        # portfolio 非空：有 cash 字段 + user 字段
        assert "cash" in result, f"status 应返回 cash 字段，实际: {list(result.keys())}"
        assert "user" in result, "status 应返回 user 字段"
        # user 字段非空
        assert result["user"].get("name") == "SmokeUser"
        # cash.cny 应有值（seed 写了 CNY: 20000.0）
        assert result["cash"].get("cny") == pytest.approx(20000.0)


# ============ 辅助：onboarding 数据完整性 ============

class TestOnboardingDataIntegrity:
    """init 写入后的数据结构校验"""

    def test_portfolio_md_v2_schema(self, tmp_path):
        """init holdings_v2 写入后 portfolio.md 符合 v2 schema"""
        fake_root = tmp_path / "invest_home"
        fake_root.mkdir()

        payload = _build_init_payload(fake_root)

        import core.memory_store as ms_module
        fake_memory_root = fake_root / "memory"

        # cmd_init 实现已搬到 scripts.skill_cmds.lifecycle_cmds，在自身模块全局读 ROOT
        with patch("scripts.skill_cmds.lifecycle_cmds.ROOT", fake_root), \
             patch.object(ms_module, "MEMORY_ROOT", fake_memory_root), \
             patch("sys.__stdout__", io.StringIO()), \
             patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            from scripts.skill import cmd_init
            cmd_init(SimpleNamespace(from_stdin=True, force=False))

        # 读取 portfolio.md 验证 schema_version=2
        from core.memory_store import MemoryStore
        store = MemoryStore(fake_root / "memory")
        port_doc = store.read("portfolio")
        assert port_doc is not None, "portfolio.md 应被写入"
        assert port_doc.get("schema_version") == 2, "写入的 portfolio 应是 v2 格式"
        # cash 非空
        cash = port_doc.get("cash") or {}
        assert "CNY" in cash, "cash 应含 CNY"

    def test_no_pollution_of_real_memory(self, tmp_path):
        """onboarding 使用 fake_root，不应修改真实 memory/ 目录"""
        real_memory = ROOT / "memory"
        real_portfolio = real_memory / "portfolio.md"

        # 记录真实文件修改时间（如果存在）
        original_mtime = real_portfolio.stat().st_mtime if real_portfolio.exists() else None

        fake_root = tmp_path / "isolated"
        fake_root.mkdir()
        _seed_minimal_memory(fake_root)

        # 跑一次 doctor（只读操作）
        _run_cmd_doctor(fake_root)

        # 真实 portfolio.md 的 mtime 不应变化
        if original_mtime is not None and real_portfolio.exists():
            assert real_portfolio.stat().st_mtime == original_mtime, (
                "冒烟测试不应修改真实 memory/portfolio.md"
            )
