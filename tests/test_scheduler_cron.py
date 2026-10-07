"""scheduler/cron.py：crontab 星期编号按标准语义（0/7=周日）翻译给 APScheduler

2026-10 事故：APScheduler 3.x 的 from_crontab 把数字星期按 0=周一 解释 →
jobs/*.yml 的 "1-5" 实际跑周二到周六（dca_daily 周六按周五价重复记账、周一从不记）。
"""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from croniter import croniter

import openinvest.jobs as jobs_pkg
from openinvest.core.config._loader import _coerce_and_validate
from openinvest.scheduler.cron import crontab_dow_to_names, crontab_trigger

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize("field,expected", [
    ("*", "*"),
    ("1-5", "mon,tue,wed,thu,fri"),          # ≡ mon-fri（不是 APScheduler 原生的 tue-sat）
    ("0", "sun"),
    ("7", "sun"),
    ("1,3,5", "mon,wed,fri"),
    ("sat-sun", "sun,sat"),      # 以周日结尾的区间 = 7（APScheduler / croniter 都认）
    ("fri-sun", "sun,fri,sat"),
    ("5-0", "sun,fri,sat"),
    ("*/2", "sun,tue,thu,sat"),              # crontab 从 0=周日 起步
    ("0-6", "sun,mon,tue,wed,thu,fri,sat"),  # 原样交给 APScheduler 会变成 "sun-sat" 区间非法
    ("5-7", "sun,fri,sat"),
    ("1-5/2", "mon,wed,fri"),
    ("mon-fri", "mon,tue,wed,thu,fri"),
    ("SUN", "sun"),
])
def test_dow_translation_table(field, expected):
    assert crontab_dow_to_names(field) == expected


@pytest.mark.parametrize("bad", ["8", "x", "1,,2", "?", "1-", "-1", "6/2", "7/2",
                                 "1-5/0", "1-5/", "*/x", "1-5/-1"])
def test_bad_dow_raises(bad):
    with pytest.raises(ValueError):
        crontab_dow_to_names(bad)


def test_weekday_schedule_fires_mon_to_fri_only():
    """dca_daily 的 "0 15 * * 1-5"（北京时间）只落在周一到周五，且包含周一。"""
    trig = crontab_trigger("0 15 * * 1-5", timezone="Asia/Shanghai")
    now = datetime(2026, 10, 5, tzinfo=SH)  # 周一 00:00
    fires = []
    for _ in range(10):
        nxt = trig.get_next_fire_time(None, now)
        fires.append(nxt)
        now = nxt + timedelta(seconds=1)
    assert {f.weekday() for f in fires} == {0, 1, 2, 3, 4}
    assert all(f.hour == 15 for f in fires)


def _yml_schedules():
    return [yaml.safe_load(p.read_text(encoding="utf-8"))["schedule"]
            for p in sorted(Path(jobs_pkg.__file__).parent.glob("*.yml"))]


@pytest.mark.parametrize("expr", [
    "0 15 * * 1-5", "0 11 * * 0", "0 9 * * 7", "*/30 8-23 * * 1,3,5", "0 12 * * */2",
    *_yml_schedules(),
])
def test_matches_standard_crontab(expr):
    """逐日比对：从每天 0 点起的下一次触发时间，与 croniter（标准 crontab 语义）一致。

    起点错开整分 30 秒：APScheduler 含起点、croniter 不含，整点起步会假失败。
    """
    trig = crontab_trigger(expr, timezone="Asia/Shanghai")
    for d in range(8):
        now = datetime(2026, 10, 5, 0, 0, 30, tzinfo=SH) + timedelta(days=d)
        assert trig.get_next_fire_time(None, now) == croniter(expr, now).get_next(datetime), (expr, now)


def test_config_cron_validation_uses_standard_dow():
    """config 白名单 cron 校验走同一 helper：crontab 合法的 7=周日 不被拒（APScheduler 原生会拒）。"""
    assert _coerce_and_validate("event.watch_schedule", "0 15 * * 7") == "0 15 * * 7"
    with pytest.raises(ValueError):
        _coerce_and_validate("event.watch_schedule", "0 15 * * 8")
