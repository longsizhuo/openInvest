"""crontab 字符串 → APScheduler CronTrigger（按标准 crontab 语义解释 day_of_week）

APScheduler 3.x 的 `CronTrigger.from_crontab` 把数字 day_of_week 按 **0=周一** 解释，
标准 crontab 是 **0/7=周日、1=周一**。2026-10 实测（db/jobs.sqlite job_runs）：
jobs/*.yml 写 "1-5"（本意周一到周五）实际跑周二到周六——dca_daily 周六按周五价重复
记账、周一从不记账；"0"（本意周日）实际跑周一。

所有 crontab 字符串（jobs/*.yml + config override 校验）必经 `crontab_trigger()`：
把数字 day_of_week 展开成星期名再交给 APScheduler，名字两边语义一致。
本模块无副作用（runner.py import 时会配 logging/建目录），core.config 校验也能安全 import。

ponytail: 只翻译 day_of_week 编号。crontab "日 和 星期 同时受限 = OR" 的语义 APScheduler
是 AND，未翻译——当前 yml/config 无此写法；真要用时在这里加拆分。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from apscheduler.triggers.cron import CronTrigger

# 每个 job 按当前 schedule 生效的起点：runner 注册 / schedule 变更时写，job_watchdog 读
# （只把这之后的应触发时刻算"该跑没跑"）。放这里而不是 runner：daemon 以
# `python -m openinvest.scheduler.runner` 启动，runner 在进程里是 __main__，job 里
# `from openinvest.scheduler import runner` 会再加载一份模块副本、状态是空的——
# 2026-10-08 #272 就因此在生产没生效（15:07 照样判停摆）。本模块两边都按正常名导入。
SCHEDULED_SINCE: Dict[str, datetime] = {}

# crontab 编号顺序：0=sun … 6=sat（7 也是 sun，取模处理）
_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _dow_index(tok: str) -> int:
    if tok.isdigit() and int(tok) <= 7:
        return int(tok)
    if tok in _DOW:
        return _DOW.index(tok)
    raise ValueError(f"非法 day_of_week: {tok!r}（crontab 只接受 0-7 或 sun-sat）")


def crontab_dow_to_names(field: str) -> str:
    """crontab day_of_week 字段 → APScheduler 星期名列表。

    "1-5" → "mon,tue,wed,thu,fri"；"0"/"7" → "sun"；"*/2" → "sun,tue,thu,sat"。
    支持数字/名字、区间、列表、步长（只接在 "*" 或区间后，同标准 crontab）；"*" 原样返回。
    """
    if field == "*":
        return field
    days = set()
    for term in field.lower().split(","):
        rng, slash, step = term.partition("/")
        if rng == "*":
            lo, hi, dash = 0, 6, ""
        else:
            a, dash, b = rng.partition("-")
            if slash and not dash:  # "6/2"：非标准写法（各家 cron 解释不一），直接拒
                raise ValueError(f"非法 day_of_week: {term!r}（步长只能接 * 或区间）")
            lo = _dow_index(a)
            hi = _dow_index(b) if dash else lo  # "1-" / "-1" 的空端点在 _dow_index 里拒
        n = int(step) if slash else 1  # "1-5/" / "1-5/x" → int 抛 ValueError
        if dash and hi == 0 and lo > 0:  # "sat-sun" / "5-0"：区间以周日结尾 = 7
            hi = 7
        if lo > hi or n < 1:
            raise ValueError(f"非法 day_of_week: {term!r}")
        days.update(d % 7 for d in range(lo, hi + 1, n))
    return ",".join(_DOW[d] for d in sorted(days))


def crontab_trigger(expr: str, timezone: Any = None) -> CronTrigger:
    """`CronTrigger.from_crontab` 的标准 crontab 语义版（day_of_week 0/7=周日）。非法表达式抛 ValueError。"""
    values = expr.split()
    if len(values) == 5:
        values[4] = crontab_dow_to_names(values[4])
    return CronTrigger.from_crontab(" ".join(values), timezone=timezone)
