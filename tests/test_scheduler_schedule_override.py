"""scheduler/runner._resolve_schedule 的单测

守的行为（2026-07-03 event_watch 扫描窗口修正）：
- event_watch 的 schedule 优先读 config override（event.watch_schedule）
- config 层异常 / 空值 / 非法 cron → 一律退回 yml 兜底值，绝不拦住调度器启动
- 非 event_watch 的 job 不受 config 影响
"""
import pytest

from scheduler.runner import _resolve_schedule


YML_DEFAULT = "*/30 0-2,8-23 * * *"


class _FakeEvent:
    def __init__(self, watch_schedule):
        self.watch_schedule = watch_schedule


class _FakeCfg:
    def __init__(self, watch_schedule):
        self.event = _FakeEvent(watch_schedule)


def test_other_jobs_use_yml(monkeypatch):
    """非 event_watch 不碰 config——load_config 被调就直接炸,证明没走那条路。"""
    monkeypatch.setattr(
        "core.config.load_config",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该被调用")),
    )
    assert _resolve_schedule("daily_report", "0 9 * * *") == "0 9 * * *"


def test_event_watch_prefers_config(monkeypatch):
    monkeypatch.setattr(
        "core.config.load_config", lambda *a, **k: _FakeCfg("*/15 8-23 * * 1-5")
    )
    assert _resolve_schedule("event_watch", YML_DEFAULT) == "*/15 8-23 * * 1-5"


def test_event_watch_empty_config_falls_back(monkeypatch):
    monkeypatch.setattr("core.config.load_config", lambda *a, **k: _FakeCfg("  "))
    assert _resolve_schedule("event_watch", YML_DEFAULT) == YML_DEFAULT


def test_event_watch_bad_cron_falls_back(monkeypatch):
    """手改 overrides json 塞了非法 cron——退回 yml,不让 daemon 起不来。"""
    monkeypatch.setattr("core.config.load_config", lambda *a, **k: _FakeCfg("not a cron"))
    assert _resolve_schedule("event_watch", YML_DEFAULT) == YML_DEFAULT


def test_event_watch_config_error_falls_back(monkeypatch):
    monkeypatch.setattr(
        "core.config.load_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("config 层炸了")),
    )
    assert _resolve_schedule("event_watch", YML_DEFAULT) == YML_DEFAULT
