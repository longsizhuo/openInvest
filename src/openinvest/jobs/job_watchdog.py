"""job 看门狗——发现"调度器以为一切正常"的静默停摆（2026-10-07）

背景：event_watch 2026-07-30 卡死在新闻抓取线程池 join，APScheduler max_instances=1
把之后 21 天的每次触发都 skip 掉；runner 当时只在 job 跑完才写 job_runs，库里零行、
零报警。现在 runner 开跑即写 status='running' 行，本 job 每小时扫一遍 job_runs，
对每个 enabled job：

  (a) stale      最近一行已结束，且其启动之后 cron 应触发的连续 2 次都没启动（+15 分钟宽限）
  (b) hung       最近一行仍是 running，且已超 max(60 分钟, 3×该 job 历史中位耗时)
  (c) zero_fetch event_watch 最近 6 次成功运行 fetched 全为 0（新闻源全挂也是"成功"）

命中走与其他 job 相同的告警通道（Discord DM best-effort + 邮件）。同一异常 24h 内最多
报一次：state_claim 去重键 = 异常锚点（上次启动时间 / running 行 id / 零抓取连击起点）
+ 自"可报警时刻"起算的 24h 桶序号——异常持续则每 24h 提醒一次，恢复后再犯是新锚点立即报。

盲区：调度器进程整体挂掉时本 job 也不跑——那需要外部 dead-man switch，不在本 job 范围。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from openinvest.core.memory_store import MemoryStore
from openinvest.scheduler.cron import crontab_trigger
from openinvest.services.discord_notify import send_discord_alert
from openinvest.services.notifier import render_markdown_email, send_email_html

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=15)
HUNG_FLOOR = timedelta(minutes=60)
ZERO_FETCH_RUNS = 6
_STATE = "job_watchdog_alerts"
# runner 把 job 返回的 dict 以 str(result) 落进 output_excerpt（Python repr，不是 JSON）
_FETCHED_RE = re.compile(r"""['"]fetched['"]:\s*(\d+)""")


def _ts(s: str) -> datetime:
    # job_runs 里新旧行 offset 混杂（+00:00 / +08:00）；naive 兜底按本地时区
    return datetime.fromisoformat(s).astimezone()


def _trigger(schedule: str, tz: str) -> CronTrigger:
    # 看门狗必须和调度器用同一套 cron 解释（runner 注册 trigger 的同一个函数），
    # 否则 day_of_week 口径一修就互相误报。
    return crontab_trigger(schedule, timezone=tz)


def find_problems(
    conn: sqlite3.Connection, jobs: List[Tuple[str, str, str]], now: datetime,
    scheduled_since: Optional[Dict[str, datetime]] = None,
) -> List[Dict[str, Any]]:
    """纯读 job_runs → 异常列表。jobs = [(name, cron, timezone)]，只传 enabled 的。

    每条 finding = {key, since, text}：key 是去重锚点（不含 24h 桶号），since 是
    该异常变得"可报警"的时刻。
    """
    out: List[Dict[str, Any]] = []
    for name, schedule, tz in jobs:
        last = conn.execute(
            "SELECT id, started_at, status, finished_at FROM job_runs WHERE job_name = ? "
            "ORDER BY id DESC LIMIT 1", (name,),
        ).fetchone()
        if last is None:
            continue  # 从没跑过 = 没有基线，不猜（新 job 首跑前不误报）
        run_id, started_s, status, finished_s = last
        started = _ts(started_s)

        # finished_at 已落 = 跑完了（KeyboardInterrupt/SystemExit 绕过 except Exception 时
        # status 会停在 running 但 finally 照样写 finished_at）——不算卡死
        if status == "running" and finished_s is None:
            # 只看最近一行：更早的孤儿 running（daemon 重启杀掉的）已被后续触发覆盖，不算卡
            durs = [
                (_ts(f) - _ts(s)).total_seconds()
                for s, f in conn.execute(
                    "SELECT started_at, finished_at FROM job_runs WHERE job_name = ? "
                    "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 50", (name,),
                )
            ]
            med = timedelta(seconds=statistics.median(durs)) if durs else timedelta(0)
            limit = max(HUNG_FLOOR, 3 * med)
            if now - started > limit:
                out.append({
                    "key": f"{name}:hung:{run_id}",
                    "since": started + limit,
                    "text": (f"`{name}` 疑似卡死：run #{run_id} 自 {started_s} 起仍是 running，"
                             f"已 {int((now - started).total_seconds() // 60)} 分钟"
                             f"（阈值 {int(limit.total_seconds() // 60)} 分钟 = "
                             f"max(60, 3×中位耗时 {int(med.total_seconds())}s)）。"
                             f"max_instances=1 下它不结束，后续触发会被全部跳过。"),
                })
            continue

        trig = _trigger(schedule, tz)
        # 起点取"上次启动"与"当前 schedule 生效时刻"中较晚者：改 schedule / 重启后，
        # 旧 cron 时代没跑的时刻不算停摆（runner._SCHEDULED_SINCE；--once 下为空 = 不截）
        anchor = max(started, (scheduled_since or {}).get(name, started))
        f1 = trig.get_next_fire_time(anchor, anchor)  # 严格晚于 anchor 的下一次
        f2 = trig.get_next_fire_time(f1, f1) if f1 else None
        if f2 is not None and now > f2 + GRACE:
            out.append({
                "key": f"{name}:stale:{started_s}",
                "since": f2 + GRACE,
                "text": (f"`{name}` 停摆：上次启动 {started_s}，按 cron `{schedule}` "
                         f"应在 {f1.isoformat()} 与 {f2.isoformat()} 启动，均无记录"
                         f"（宽限 {int(GRACE.total_seconds() // 60)} 分钟）。"),
            })

    if any(name == "event_watch" for name, _, _ in jobs):
        streak: List[Tuple[int, str]] = []  # 当前 fetched=0 连击，新→旧
        for rid, s_at, output in conn.execute(
            "SELECT id, started_at, output_excerpt FROM job_runs "
            "WHERE job_name = 'event_watch' AND status = 'success' ORDER BY id DESC"
        ):
            m = _FETCHED_RE.search(output or "")
            if not m or int(m.group(1)) != 0:
                break
            streak.append((rid, s_at))
        if len(streak) >= ZERO_FETCH_RUNS:
            out.append({
                "key": f"event_watch:zero_fetch:{streak[-1][0]}",
                "since": _ts(streak[-ZERO_FETCH_RUNS][1]),
                "text": (f"`event_watch` 连续 {len(streak)} 次成功运行 fetched=0"
                         f"（自 {streak[-1][1]}）——新闻源可能全挂，job 照样报 success。"),
            })
    return out


def _send(findings: List[Dict[str, Any]]) -> None:
    subject = f"⚠️ openInvest 调度看门狗：{len(findings)} 项异常"
    md = (
        "## ⚠️ 调度看门狗告警\n\n"
        + "\n".join(f"- {f['text']}" for f in findings)
        + "\n\n_排查：`db/jobs.sqlite` 的 job_runs 表 + `logs/invest.log`；"
          "同一异常 24h 内只提醒一次。_"
    )
    # 与 send_committee_verdict_email 同顺序：DM 先推（best-effort 永不抛），邮件保底归档。
    # DM 已送达时邮件再失败也不抛：否则 run() 回滚 claim，SMTP 挂多久 DM 就每小时重发多久
    dm_ok = bool(send_discord_alert(f"**{subject}**\n{md}"))
    try:
        send_email_html(
            subject=subject,
            html_body=render_markdown_email(md, footer_label="Invest Job Watchdog"),
            plain_body=md,
        )
    except Exception:
        if not dm_ok:
            raise
        log.warning("[job_watchdog] 邮件投递失败，DM 已送达，不回滚去重")


def run(now: Optional[datetime] = None) -> Dict[str, Any]:
    # 函数内 import：runner 模块级会配 logging + 建 db/ logs/ 目录，不该在 import 本模块时发生
    from openinvest.scheduler import runner

    now = now or datetime.now().astimezone()
    jobs = [
        (c["name"], runner._resolve_schedule(c["name"], c["schedule"]),
         c.get("timezone", "Asia/Shanghai"))
        for c in runner._load_job_configs() if c.get("enabled", False)
    ]
    conn = sqlite3.connect(runner.RUN_LOG_DB)
    try:
        findings = find_problems(conn, jobs, now, runner._SCHEDULED_SINCE)
    finally:
        conn.close()
    for f in findings:
        log.warning(f"[job_watchdog] {f['text']}")

    store = MemoryStore()
    fresh: List[Tuple[str, Dict[str, Any]]] = []
    for f in findings:
        claim = f"{f['key']}#{int((now - f['since']).total_seconds() // 86400)}"
        if store.state_claim(_STATE, claim):
            fresh.append((claim, f))
    if fresh:
        try:
            _send([f for _, f in fresh])
        except Exception:
            # 投递失败 → 回滚 claim，下个小时重试，别让这次告警被 24h 去重吞掉
            for claim, _ in fresh:
                store.state_unclaim(_STATE, claim)
            raise
    return {
        "status": "ok",
        "jobs_checked": len(jobs),
        "findings": [f["text"] for f in findings],
        "alerted": [claim for claim, _ in fresh],
    }


if __name__ == "__main__":
    print(run())
