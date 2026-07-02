"""scheduler/runner._resolve_schedule 的单测

守的行为（2026-07-03 event_watch 扫描窗口修正）：
- event_watch 的 schedule 优先读 config override（event.watch_schedule）
- config 层异常 / 空值 / 非法 cron → 一律退回 yml 兜底值，绝不拦住调度器启动
- 非 event_watch 的 job 不受 config 影响
"""
import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from scheduler.runner import _resolve_schedule, register_jobs


YML_DEFAULT = "*/30 0-2,8-23 * * *"


class _FakeEvent:
    def __init__(self, watch_schedule, sentinel_schedule="*/5 0-2,8-23 * * *"):
        self.watch_schedule = watch_schedule
        self.sentinel_schedule = sentinel_schedule


class _FakeCfg:
    def __init__(self, watch_schedule, sentinel_schedule="*/5 0-2,8-23 * * *"):
        self.event = _FakeEvent(watch_schedule, sentinel_schedule)


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


def test_yml_and_tunable_defaults_do_not_drift():
    """jobs/event_watch.yml 的兜底 schedule 与 EventConfig.watch_schedule 的默认值
    现在是两处手写字面量（PR #128 自己的注释承认"两处默认值保持一致"）——这正是
    本 PR 修的那类 bug（注释/字面量与实际运行值悄悄分叉）。没有这条测试，改动
    一处忘了改另一处不会有任何信号，直到再次错过报警窗口才会被发现。"""
    from core.config.tunable import EventConfig
    from scheduler.runner import _load_job_configs

    configs = _load_job_configs()
    event_watch_cfg = next(c for c in configs if c["name"] == "event_watch")
    assert event_watch_cfg["schedule"] == EventConfig().watch_schedule, (
        "jobs/event_watch.yml 的 schedule 兜底值和 core/config/tunable.py 的 "
        "EventConfig.watch_schedule 默认值不一致了——两处都要改，别只改一处"
    )


class TestRegisterJobsRemovesDisabled:
    """register_jobs 对刚被 disable 的 job 必须真的从调度器摘除，而不是只是
    跳过注册——否则"改 yml 不用重启"这个卖点对禁用操作是假的：旧 trigger 会
    一直跑到进程重启为止（code review 发现，见 commit message）。"""

    def _fake_configs(self, enabled: bool):
        return [{
            "name": "fake_job", "schedule": "*/5 * * * *", "timezone": "UTC",
            "entry": "jobs.dca_daily:run", "enabled": enabled,
        }]

    def test_disabling_a_job_removes_it_from_scheduler(self, monkeypatch):
        sched = BackgroundScheduler()
        monkeypatch.setattr(
            "scheduler.runner._load_job_configs", lambda: self._fake_configs(True)
        )
        register_jobs(sched)
        assert sched.get_job("fake_job") is not None

        monkeypatch.setattr(
            "scheduler.runner._load_job_configs", lambda: self._fake_configs(False)
        )
        register_jobs(sched, quiet=True)
        assert sched.get_job("fake_job") is None

    def test_never_enabled_job_disable_pass_is_a_noop(self, monkeypatch):
        """从没注册过就被跳过——不该因为 get_job 返回 None 就报错或崩溃"""
        sched = BackgroundScheduler()
        monkeypatch.setattr(
            "scheduler.runner._load_job_configs", lambda: self._fake_configs(False)
        )
        register_jobs(sched, quiet=True)  # 不抛异常即通过
        assert sched.get_job("fake_job") is None


def test_price_sentinel_prefers_config(monkeypatch):
    """price_sentinel 同样走 config 映射（_CONFIG_SCHEDULES 泛化）。"""
    monkeypatch.setattr(
        "core.config.load_config",
        lambda *a, **k: _FakeCfg(YML_DEFAULT, sentinel_schedule="*/10 8-23 * * *"),
    )
    assert _resolve_schedule("price_sentinel", "*/5 0-2,8-23 * * *") == "*/10 8-23 * * *"
