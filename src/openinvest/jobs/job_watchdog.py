"""job 看门狗——发现"调度器以为一切正常"的静默停摆（2026-10-07）

背景：event_watch 2026-07-30 卡死在新闻抓取线程池 join，APScheduler max_instances=1
把之后 21 天的每次触发都 skip 掉；runner 当时只在 job 跑完才写 job_runs，库里零行、
零报警。现在 runner 开跑即写 status='running' 行，本 job 每小时扫一遍 job_runs，
对每个 enabled job：

  (a) stale      最近一行已结束，且其启动之后 cron 应触发的连续 2 次都没启动（+15 分钟宽限）
  (b) hung       最近一行仍是 running，且已超 max(60 分钟, 3×该 job 历史中位耗时)
  (c) zero_fetch event_watch 最近 6 次成功运行 fetched 全为 0（新闻源全挂也是"成功"）
  (d) silent_src 单个新闻源曾经抓到过、最近一轮仍在抓，但已连续 24h 抓到 0 条
                （2026-10 yfinance 新闻接口 404 被吞成 []，其他源照常 → (c) 看不见，断了 13 天）
  (f) norm_fail  event_watch 连续 2 次运行有归一化失败批（缺 key / 额度用完 / 解析失败）——
                抓到了新闻却没入库，job 照样 success（2026-10-19 Claude 回复带尾巴整批丢，无人知道）
  (g) verdict    今天/昨天的委员会记录里有 LLM 没跑成的（UNCLEAR / 有角色没产出被强制 HOLD）
  (h) backlog    agent 模式待办没人取：工作日 10 点后有待归一化新闻 / 待跑委员会，且宿主 Agent
                超过 26h 没来取过（哨兵只在工作日跑，周末积压不报）
  (e) timeout    runner 的 per-job 墙钟超时（jobs/*.yml timeout_minutes）把 job_runs 标成
                'timeout'：24h 内每条各报一次。超时后 runner 放弃该线程让下次触发进得来，
                所以最新一行可能已是 success——不能只看最后一行，否则"超时了又恢复"没人知道
  (f) failed     jobs/*.yml 标了 `alert_on_failure: true` 的 job（backup / verdict_review / price_sentinel /
                fund_confirm）最近一次已结束的运行是 'failed'（job 抛了异常，runner 记 error）。此前 failed 只落
                job_runs 不报警——"失败抛异常，看门狗据此告警"的说法并不成立（2026-10 ADR-027 PR3 查出）。
                默认不标 = 行为不变（别的 job 失败仍只落库）。去重键 (job, 失败那天的 Asia/Shanghai 日期)：
                price_sentinel 每 5 分钟一次、fund_confirm 每晚 6 次，同一天连续失败只报一次；只看**最近一次**，
                下一次成功就算恢复（失败一直是最近一次——周末 / 隔夜没再跑——则与其余判据一样每 24h 提醒一次）。timeout 不在这里重复报——(e) 已对所有 job 报

命中走与其他 job 相同的告警通道（Discord DM best-effort + 邮件）。同一异常 24h 内最多
报一次：state_claim 去重键 = 异常锚点（上次启动时间 / running 行 id / 零抓取连击起点）
+ 自"可报警时刻"起算的 24h 桶序号——异常持续则每 24h 提醒一次，恢复后再犯是新锚点立即报。

盲区：调度器进程整体挂掉时本 job 也不跑——靠外部心跳（runner `_heartbeat`，
`scheduler.heartbeat_url`，docs/wiki/08-deployment.md）补。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any, Collection, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from openinvest.core.decision_ledger import llm_failure_reason, parse_committee_file
from openinvest.core.memory_store import MemoryStore
from openinvest.scheduler.cron import SCHEDULED_SINCE, crontab_trigger
from openinvest.services.news_sources import SOURCE_HEALTH_STATE
from openinvest.services.notifier import render_markdown_email

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=15)
HUNG_FLOOR = timedelta(minutes=60)
ZERO_FETCH_RUNS = 6
NORMALIZE_FAIL_RUNS = 2
AGENT_DRAIN_STALE = timedelta(hours=26)
SOURCE_SILENT = timedelta(hours=24)
TIMEOUT_LOOKBACK = timedelta(hours=24)
_STATE = "job_watchdog_alerts"
# runner 把 job 返回的 dict 以 str(result) 落进 output_excerpt（Python repr，不是 JSON）
_FETCHED_RE = re.compile(r"""['"]fetched['"]:\s*(\d+)""")
_NORM_FAILED_RE = re.compile(r"""['"]normalize_failed['"]:\s*(\d+)""")


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
    alert_on_failure: Collection[str] = (),
) -> List[Dict[str, Any]]:
    """纯读 job_runs → 异常列表。jobs = [(name, cron, timezone)]，只传 enabled 的。
    alert_on_failure = jobs/*.yml 标了 `alert_on_failure: true` 的 job 名（判据 f，默认空 = 不查）。

    每条 finding = {key, since, text}：key 是去重锚点（不含 24h 桶号），since 是
    该异常变得"可报警"的时刻。
    """
    out: List[Dict[str, Any]] = []
    for name, schedule, tz in jobs:
        for rid, s_at, f_at, err in conn.execute(
            "SELECT id, started_at, finished_at, error FROM job_runs "
            "WHERE job_name = ? AND status = 'timeout' ORDER BY id DESC LIMIT 20", (name,),
        ).fetchall():
            if f_at is None or now - _ts(f_at) > TIMEOUT_LOOKBACK:
                continue
            out.append({
                "key": f"{name}:timeout:{rid}",
                "since": _ts(f_at),
                "text": (f"`{name}` 执行超时：run #{rid}（{s_at} 启动）{err or 'timeout'}。"
                         f"该线程已被放弃；同一 job 的超时线程累计达上限（runner.MAX_ORPHANS，"
                         f"默认 3）仍未退出时调度器会拒绝再启动它（届时转为停摆告警，重启调度器清空）。"
                         f"超时时刻的线程调用栈见 job_runs.output_excerpt。"),
            })
        if name in alert_on_failure:
            done = conn.execute(
                "SELECT id, started_at, finished_at, status, error FROM job_runs "
                "WHERE job_name = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (name,),
            ).fetchone()
            if done is not None and done[3] == "failed":
                rid, s_at, f_at, _, err = done
                day = _ts(s_at).astimezone(ZoneInfo(tz)).date().isoformat()
                out.append({
                    "key": f"{name}:failed:{day}",
                    "since": _ts(f_at),
                    "text": (f"`{name}` 运行失败：run #{rid}（{s_at} 启动）{(err or 'failed')[:300]}。"
                             f"该 job 标了 alert_on_failure；同一天连续失败只提醒一次，下一次运行成功即视为恢复。"
                             f"详情见 job_runs.output_excerpt（回溯栈）与 logs/invest.log。"),
                })
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
        # 旧 cron 时代没跑的时刻不算停摆（scheduler.cron.SCHEDULED_SINCE；--once 下为空 = 不截）
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
        # 归一化失败连击：没走到归一化的轮次（无新新闻）不带该字段，跳过不算断。
        # 不设窗口上限：连击起点固定 → 去重 key 稳定（窗口滑动会让 key 每轮都变、每 30 分钟重报）
        fails: List[Tuple[int, str, int]] = []
        for rid, s_at, output in conn.execute(
            "SELECT id, started_at, output_excerpt FROM job_runs "
            "WHERE job_name = 'event_watch' AND status = 'success' ORDER BY id DESC"
        ):
            m = _NORM_FAILED_RE.search(output or "")
            if not m:
                continue
            if int(m.group(1)) == 0:
                break
            fails.append((rid, s_at, int(m.group(1))))
        if len(fails) >= NORMALIZE_FAIL_RUNS:
            out.append({
                "key": f"event_watch:normalize_failed:{fails[-1][0]}",
                "since": _ts(fails[-NORMALIZE_FAIL_RUNS][1]),
                "text": (f"`event_watch` 连续 {len(fails)} 次运行新闻归一化有失败批"
                         f"（最近一次失败 {fails[0][2]} 批，自 {fails[-1][1]}）——抓到的新闻没入库，"
                         f"job 照样报 success。多半是 LLM 不可用（key / 额度 / 参数），看 logs/invest.log。"),
            })
    return out


def find_agent_backlog(now: datetime) -> List[Dict[str, Any]]:
    """Track actual unfinished requests, including expired ones; polling is not completion."""
    from openinvest.services import agent_queue as q
    news=q.pending_news_items(now,include_expired=True)
    committees=q.pending_committee_items(now,include_expired=True)
    findings=[]
    legacy=q.legacy_news_items()
    if legacy:
        findings.append({"key":"agent_legacy_source_verification","since":now,"one_shot":True,
                         "text":f"{len(legacy)} 条升级前的来源队列记录仅限审计，需要重新取得原始来源；不算已处理或已验证新闻。"})
    for item in committees:
        queued=datetime.fromisoformat(item["queued_at"])
        deadline=queued+timedelta(hours=1)
        if now<deadline:continue
        findings.append({"key":f"agent_committee_unfinished:{item['symbol']}:{item['queued_at']}","since":deadline,
                         "text":f"{item['symbol']} 有 {len(item.get('request_ids',[]))} 个委员会触发请求尚无绑定输入的完成回执（状态 {item.get('status','pending')}，最早排队 {queued.isoformat()}；轮询待办不等于完成。"})
    if news:
        oldest=min(datetime.fromisoformat(item["queued_at"]) for item in news)
        if now-oldest>AGENT_DRAIN_STALE:
            findings.append({"key":f"agent_news_unfinished:{oldest.isoformat()}","since":oldest+AGENT_DRAIN_STALE,
                             "text":f"{len(news)} 条新闻尚未完成归一化，最早排队 {oldest.isoformat()}。"})
    return findings


def find_failed_verdicts(committee_dir: Any, now: datetime) -> List[Dict[str, Any]]:
    """今天/昨天落盘的委员会记录里 LLM 没跑成的（纯读盘）。重跑成功会覆盖当天文件 → 自然消警。"""
    out: List[Dict[str, Any]] = []
    for day in sorted({now.date(), (now - timedelta(days=1)).date()}):
        d = committee_dir / day.isoformat()
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            reason = llm_failure_reason(parse_committee_file(p))
            if not reason:
                continue
            out.append({
                "key": f"verdict_failed:{day}/{p.stem}",
                "since": datetime.fromtimestamp(p.stat().st_mtime).astimezone(),
                "text": (f"委员会 `{day}/{p.stem}` 没跑成：{reason}。多半是 LLM 调用失败"
                         f"（key / 额度 / 参数），这份裁决不能当真；修好后直接重跑"
                         f"（UNCLEAR 记录不当缓存，不需要 force）。"),
            })
    return out


def find_silent_sources(health: Dict[str, Dict[str, Optional[str]]]) -> List[Dict[str, Any]]:
    """event_watch 记的每源抓取健康（label → {last_run, last_ok}）→ 断流列表。纯函数。

    只看最近一轮还在抓的源（移除的 RSS feed、不再触发的 akshare 不报），且必须曾经抓到过
    （只持港股/A 股的用户 yfinance 从来 0 条，没有基线，不猜）。空窗按"最近一轮"算而不是 now：
    event_watch 自己停了归 stale 报，不该再把每个源都算成断流。
    """
    if not health:
        return []
    latest = max(v["last_run"] for v in health.values())
    out: List[Dict[str, Any]] = []
    for label, v in sorted(health.items()):
        ok = v.get("last_ok")
        if v["last_run"] != latest or not ok:
            continue
        gap = _ts(latest) - _ts(ok)
        if gap > SOURCE_SILENT:
            out.append({
                "key": f"news_source:{label}:{ok}",
                "since": _ts(ok) + SOURCE_SILENT,
                "text": (f"新闻源 `{label}` 已连续 {gap.total_seconds() / 86400:.1f} 天抓到 0 条"
                         f"（上次有数据 {ok}）——接口可能失效或改版；其他源正常时 event_watch 照样报 success。"),
            })
    return out


def _send(findings: List[Dict[str, Any]]) -> dict:
    subject = f"⚠️ openInvest 调度看门狗：{len(findings)} 项异常"
    md = (
        "## ⚠️ 调度看门狗告警\n\n"
        + "\n".join(f"- {f['text']}" for f in findings)
        + "\n\n_排查：`db/jobs.sqlite` 的 job_runs 表 + `logs/invest.log`；"
          "同一异常 24h 内只提醒一次。_"
    )
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.core.evidence_eligibility import digest
    outbox=NotificationOutbox(MemoryStore())
    key="watchdog:"+digest([f["notification_claim"] for f in findings])
    ident=outbox.enqueue(key,{"subject":subject,"plain":md,"discord":f"**{subject}**\n{md}",
                             "html":render_markdown_email(md,footer_label="Invest Job Watchdog")},
                         category="job_watchdog",policy="decision")
    # The watchdog also attempts its own delivery when the delivery job is down.
    outbox.flush(only_id=ident)
    return outbox.receipt(ident)


def run(now: Optional[datetime] = None) -> Dict[str, Any]:
    # 函数内 import：runner 模块级会配 logging + 建 db/ logs/ 目录，不该在 import 本模块时发生
    from openinvest.scheduler import runner

    now = now or datetime.now().astimezone()
    configs = [c for c in runner._load_job_configs() if c.get("enabled", False)]
    jobs = [(c["name"], runner._resolve_schedule(c["name"], c["schedule"]), c.get("timezone", "Asia/Shanghai"))
            for c in configs]
    flagged = {c["name"] for c in configs if c.get("alert_on_failure", False) is True}
    conn = sqlite3.connect(runner.RUN_LOG_DB)
    try:
        findings = find_problems(conn, jobs, now, SCHEDULED_SINCE, flagged)
    finally:
        conn.close()
    store = MemoryStore()
    if any(name == "event_watch" for name, _, _ in jobs):
        findings += find_silent_sources(store.state_get(SOURCE_HEALTH_STATE) or {})
    findings += find_failed_verdicts(store.root / ".committee", now)
    findings += find_agent_backlog(now)
    from openinvest.services.decision_quality import findings as decision_findings
    from openinvest.core.config import load_config
    findings += decision_findings(store.root, hold_window=load_config().scheduler.decision_hold_review_days)
    for f in findings:
        log.warning(f"[job_watchdog] {f['text']}")

    fresh: List[Tuple[str, Dict[str, Any]]] = []
    known=set(store.state_get(_STATE,{}) or {})
    for f in findings:
        suffix = "once" if f.get("one_shot") else str(int((now - f["since"]).total_seconds() // 86400))
        claim = f"{f['key']}#{suffix}"
        if claim not in known:
            fresh.append((claim, f))
        if len(fresh) >= 20:  # bounded delivery; remaining alerts stay unclaimed for the next run
            break
    accepted=[];queued=[]
    for claim,f in fresh:
        receipt=_send([{**f,"notification_claim":claim}])
        if not isinstance(receipt,dict) or not receipt.get("notification_id"):
            raise RuntimeError("watchdog alert was not durably queued")
        store.state_claim(_STATE,claim)  # Queue receipt, not a claim of delivery.
        queued.append(claim)
        if receipt.get("accepted"):accepted.append(claim)
    return {
        "status": "ok",
        "jobs_checked": len(jobs),
        "findings": [f["text"] for f in findings],
        "alerted": accepted,
        "notifications_queued":queued,
    }


if __name__ == "__main__":
    print(run())
