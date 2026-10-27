"""Hermes 插件入口（plugin/__init__.py）：路由规则 + 账本摘要注入。

2026-10-27 hermes-clone 实测：Hermes 不注入 MCP instructions，Agent 问持仓股只去网页搜索。
守：subagent / cron 不注入；同一会话摘要不变不重复注入（每轮往历史里堆）；摘要变了再注入；
run.sh 失败（老后端没有 brief）不注入、不抛；老 Hermes 没有新 API 时照常加载。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location("_oi_plugin_test", PLUGIN / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self, config=None):
        self.hooks, self.sections, self.skills, self.config = {}, {}, [], config or {}

    def register_skill(self, name, path):
        self.skills.append(name)

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_system_prompt_section(self, sid, content):
        self.sections[sid] = content

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def _fake_run(outputs, calls):
    def run(cmd, **kw):
        calls.append(kw.get("env", {}).get("INVEST_HOME"))
        out = outputs.pop(0)
        return subprocess.CompletedProcess(cmd, 0 if out.startswith("[openInvest]") else 2, out, "")
    return run


def test_register_wires_skills_rule_and_hook(plugin):
    ctx = _Ctx()
    plugin.register(ctx)
    assert {"invest", "invest-setup"} <= set(ctx.skills)
    assert "openinvest tools first" in ctx.sections["openinvest.routing"]
    assert "pre_llm_call" in ctx.hooks


def test_old_hermes_without_new_apis_still_loads(plugin):
    class Old:
        def __init__(self):
            self.skills = []

        def register_skill(self, name, path):
            self.skills.append(name)

    ctx = Old()
    plugin.register(ctx)
    assert "invest" in ctx.skills


def test_injects_on_first_turn_and_only_when_digest_changes(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_spawn", lambda fn: fn())                # 后台刷新改成同步，好断言
    monkeypatch.setattr(plugin.subprocess, "run", _fake_run(
        ["[openInvest] Holdings: NVDA.", "[openInvest] Holdings: NVDA.", "[openInvest] Holdings: NVDA, GC=F."], calls))
    ctx = _Ctx({"invest_home": "/data/invest"})
    plugin.register(ctx)
    hook = ctx.hooks["pre_llm_call"]
    clock = [1000.0]
    monkeypatch.setattr(plugin.time, "monotonic", lambda: clock[0])

    assert hook(session_id="s1", platform="discord") == {"context": "[openInvest] Holdings: NVDA."}
    assert hook(session_id="s1", platform="discord") is None             # 缓存期内，同一摘要
    assert hook(session_id="s2", platform="qq") == {"context": "[openInvest] Holdings: NVDA."}  # 新会话首轮
    clock[0] += plugin.BRIEF_TTL_SECONDS + 1
    assert hook(session_id="s1", platform="discord") is None             # 重新取到相同摘要，不重复注入
    clock[0] += plugin.BRIEF_TTL_SECONDS + 1
    assert hook(session_id="s1", platform="discord")["context"].endswith("GC=F.")  # 账本变了
    assert calls == ["/data/invest"] * 3                                 # 只在缓存过期时调 run.sh


def test_stale_digest_is_served_while_refreshing_in_background(plugin, monkeypatch):
    """只有什么都没缓存的第一次要等 run.sh；之后过期了先给旧摘要，后台刷新，不卡这一轮。"""
    calls, spawned = [], []
    monkeypatch.setattr(plugin.subprocess, "run", _fake_run(["[openInvest] Holdings: NVDA."], calls))
    monkeypatch.setattr(plugin, "_spawn", spawned.append)
    clock = [1000.0]
    monkeypatch.setattr(plugin.time, "monotonic", lambda: clock[0])
    ctx = _Ctx()
    plugin.register(ctx)
    hook = ctx.hooks["pre_llm_call"]
    assert hook(session_id="s1", platform="discord")["context"].endswith("NVDA.")
    clock[0] += plugin.BRIEF_TTL_SECONDS + 1
    assert hook(session_id="s2", platform="discord")["context"].endswith("NVDA.")
    assert len(calls) == 1 and len(spawned) == 1                          # 刷新交给了后台线程


def test_failed_brief_backs_off_instead_of_retrying_every_ttl(plugin, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired("bash", 15)                     # 首装 uvx 还在下依赖

    monkeypatch.setattr(plugin.subprocess, "run", run)
    clock = [1000.0]
    monkeypatch.setattr(plugin.time, "monotonic", lambda: clock[0])
    ctx = _Ctx()
    plugin.register(ctx)
    hook = ctx.hooks["pre_llm_call"]
    assert hook(session_id="s", platform="cli") is None
    clock[0] += plugin.BRIEF_TTL_SECONDS + 1
    assert hook(session_id="s", platform="cli") is None and len(calls) == 1
    clock[0] += plugin.BRIEF_RETRY_SECONDS
    hook(session_id="s", platform="cli")
    assert len(calls) == 2


def test_without_session_id_injects_every_turn(plugin, monkeypatch):
    monkeypatch.setattr(plugin.subprocess, "run", _fake_run(["[openInvest] Holdings: NVDA."], []))
    ctx = _Ctx()
    plugin.register(ctx)
    hook = ctx.hooks["pre_llm_call"]
    assert hook(platform="discord") and hook(platform="discord")          # 没法去重：不能只给第一个会话


@pytest.mark.parametrize("value", [False, "false"])
def test_inject_digest_off_keeps_only_the_routing_rule(plugin, value):
    ctx = _Ctx({"inject_digest": value})
    plugin.register(ctx)
    assert "pre_llm_call" not in ctx.hooks and "openinvest.routing" in ctx.sections


@pytest.mark.parametrize("platform", ["subagent", "cron"])
def test_skips_subagents_and_cron(plugin, monkeypatch, platform):
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: pytest.fail("should not run brief"))
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.hooks["pre_llm_call"](session_id="s", platform=platform) is None


@pytest.mark.parametrize("result", [
    json.dumps({"status": "error", "error": "skill: invalid choice: 'brief'"}),   # 老后端
    subprocess.TimeoutExpired("bash", 15),
])
def test_brief_failure_injects_nothing(plugin, monkeypatch, result):
    def run(cmd, **kw):
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(cmd, 2, result, "")

    monkeypatch.setattr(plugin.subprocess, "run", run)
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.hooks["pre_llm_call"](session_id="s", platform="cli") is None


def test_claude_code_session_start_hook_runs_brief():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    assert hooks["type"] == "command" and hooks["command"].endswith("run.sh\" brief --text")
