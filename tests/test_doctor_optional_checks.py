"""doctor 的 status 语义（#444）：只有 onboarding 必需项挡路，LLM key / 邮件 / .env 缺失只是 optional。

以前总 status = "所有 check 都 ok 才 ready"，已初始化但没配 LLM key / Gmail 的 Coordinator 用户拿到
needs_setup，SKILL.md 又规定 needs_setup 要切 invest-setup → 被带去重装。现在：
- memory 未初始化（或 portfolio 是 v1）→ needs_setup
- 已初始化缺 key / 邮件 / .env → ready，对应 check 标 optional_*，direct_ready=False 但 coordinator_ready=True

不联网：没 key 时 doctor 根本不探测 LLM；有 key 的用例 patch requests.get。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services.skill_views import build_doctor_view

CRED_ENV = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_BASE_URL", "EMAIL_SENDER", "EMAIL_PASSWORD")


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    for k in CRED_ENV:
        monkeypatch.delenv(k, raising=False)


def _seed(initialized=True, portfolio=None):
    if not initialized:
        return
    s = MemoryStore()
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", portfolio or {"schema_version": 2, "cash": {"CNY": 1.0}, "holdings": []}, "")


def _by_name(view):
    return {c["name"]: c for c in view["checks"]}


def test_initialized_without_any_credentials_is_ready(tmp_path):
    _seed()
    v = build_doctor_view(tmp_path)          # tmp_path 里没有 .env，env 里没有任何凭据
    assert v["status"] == "ready"
    assert v["coordinator_ready"] is True and v["direct_ready"] is False
    c = _by_name(v)
    assert c["memory_initialized"]["status"] == "ok"
    assert c["llm_key"]["status"] == "optional_for_coordinator"
    assert "Direct" in c["llm_key"]["hint"] and "Coordinator" in c["llm_key"]["hint"]
    assert c["gmail_credentials"]["status"] == "optional_for_email"
    assert c[".env_file"]["status"] == "optional_for_coordinator"
    assert "invest-setup" in v["next_step"] and "不要" in v["next_step"]       # 明确告诉 agent 别去重装
    # 检查项名字不再是 DeepSeek 专属
    assert not [n for n in c if "deepseek" in n]


def test_not_initialized_is_needs_setup(tmp_path):
    _seed(initialized=False)
    v = build_doctor_view(tmp_path)
    assert v["status"] == "needs_setup"
    assert v["coordinator_ready"] is False
    assert _by_name(v)["memory_initialized"]["status"] == "missing"


def test_v1_portfolio_still_needs_setup(tmp_path):
    """portfolio.md 是 v1 旧格式属于 onboarding 必需项（status 读不到现金）→ 仍 needs_setup。"""
    _seed(portfolio={"cash_cny": 100.0, "aud_cash": 0.0})
    v = build_doctor_view(tmp_path)
    assert v["status"] == "needs_setup"
    assert _by_name(v)["portfolio_schema"]["status"] == "needs_migration"


def test_key_set_makes_direct_ready_and_probes_llm(tmp_path, monkeypatch):
    _seed()
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return MagicMock(status_code=200)
    monkeypatch.setattr("requests.get", fake_get)
    v = build_doctor_view(tmp_path)
    assert v["status"] == "ready" and v["direct_ready"] is True
    c = _by_name(v)
    assert c["llm_key"]["status"] == "ok" and c["llm_reachable"]["status"] == "ok"
    assert seen["url"] == "https://llm.example/v1/models"                       # 读 LLM_BASE_URL，不是写死 DeepSeek


def test_bad_key_is_optional_not_blocking(tmp_path, monkeypatch):
    """key 配了但 401：Direct 路径坏了，Coordinator 不受影响 → 仍 ready，只在 llm_reachable 提示。"""
    _seed()
    monkeypatch.setenv("LLM_API_KEY", "sk-bad")
    monkeypatch.setattr("requests.get", lambda *a, **k: MagicMock(status_code=401))
    v = build_doctor_view(tmp_path)
    assert v["status"] == "ready"
    r = _by_name(v)["llm_reachable"]
    assert r["status"] == "optional_for_coordinator" and "401" in r["detail"]


def test_python_venv_check_only_in_git_clone_form(tmp_path):
    """python_venv 是 git clone 时代的检查：数据目录没有 pyproject.toml + skills/ 的 uvx 形态跳过。"""
    _seed()
    assert "python_venv" not in _by_name(build_doctor_view(tmp_path))
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "skills").mkdir()
    assert "python_venv" in _by_name(build_doctor_view(tmp_path))


def test_mcp_doctor_tool_matches_cli_view(tmp_path, monkeypatch):
    """MCP doctor 与 CLI/REST 同源（build_doctor_view(INVEST_ROOT)），纯 MCP agent 不必靠 Bash。"""
    from openinvest.connectors import mcp_server as m
    _seed()
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT", tmp_path)
    out = m.doctor()
    assert out["status"] == "ready" and out["coordinator_ready"] is True
    assert {"checks", "next_step", "version", "direct_ready"} <= set(out)
