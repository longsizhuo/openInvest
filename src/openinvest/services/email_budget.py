"""One persistent admission gate for every SMTP entry; no market decisions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid

from openinvest.core.memory_store import MemoryStore
from openinvest.services.notifier import EmailDeliveryError

STATE = 'email_dispatch_budget_v1'


class EmailDeferred(EmailDeliveryError):
    delivery_deferred = True

    def __init__(self, reason, retry_at):
        super().__init__(reason)
        self.retry_at = retry_at


class EmailAcceptanceUnknown(EmailDeliveryError):
    delivery_uncertain = True


def urgent_identity(payload):
    """Repeat identity uses the observed condition/sources, never LLM phrasing."""
    events = payload.get('events') or []
    risk = [e for e in events if e.get('stance') == 'risk']
    domain = (payload.get('cooldown') or {}).get('domain')
    if domain and risk:
        return 'price:' + str(domain)
    identities = []
    for event in events:
        previous = event.get('revision_previous') or {}
        active = event.get('stance') == 'risk' and event.get('severity') == 'high'
        correction = previous.get('stance') == 'risk' and previous.get('severity') == 'high' and (
            event.get('stance') != 'risk' or event.get('severity') != 'high')
        if not (active or correction):
            continue
        sources = [source for source in event.get('sources',[]) if source.get('url')]
        if not sources:
            continue
        material = (event.get('_analysis_cause_id'), event.get('_analysis_version_id'))
        if not all(isinstance(value,(str,int)) and not isinstance(value,bool) for value in material):
            from openinvest.services.news_identity import content_id
            # Legacy URL-only records retain a conservative identity; model
            # paraphrases and fetch timestamps cannot invent a new publication.
            material = tuple(sorted({content_id(source) for source in sources}))
        identities.append([material,event.get('stance','unknown'),event.get('severity','unknown'),
                           'correction' if correction else 'risk'])
    if not identities:
        return None
    # Aliases of one observed publication do not create a second risk identity.
    canonical = sorted({json.dumps(value,sort_keys=True) for value in identities})
    return 'news:' + hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def reserve(*, receiver, subject, plain_body, html_body=None, category='direct', notification_id=None,
            urgency_key=None, report_date=None, store=None, now=None):
    from openinvest.core.config import load_config
    store = store or MemoryStore()
    now = now or datetime.now(timezone.utc)
    clock = now.timestamp()
    cfg = load_config(_force_reload=True).event
    policy_file = store.root/'.state/config_overrides.json'
    if policy_file.exists() or policy_file.is_symlink():
        try:
            saved = json.loads(policy_file.read_text())
            if not isinstance(saved, dict) or not isinstance(saved.get('event', {}), dict):
                raise ValueError('invalid persisted notification policy')
        except (OSError, ValueError):
            raise EmailDeferred('email_policy_unreadable', clock + 3600) from None
    mode = cfg.email_mode
    if not cfg.email_enabled or mode == 'off' or (store.data_root/'integrity/email_delivery_hold.json').exists():
        raise EmailDeferred('email_channel_paused', clock + 3600)
    if mode not in {'all', 'daily_and_risk', 'daily'}:
        raise EmailDeferred('email_policy_invalid', clock + 3600)
    daily = category in {'daily_report', 'daily_digest'}
    day = now.astimezone().date().isoformat()
    if daily and report_date != day:
        raise EmailDeferred('report_day_unverified_or_stale', clock + 3600)
    if mode != 'all' and not daily and not (mode == 'daily_and_risk' and urgency_key):
        raise EmailDeferred('included_in_daily_summary_only', clock + 3600)
    cap = cfg.email_daily_cap
    if type(cap) is not int or cap < 1:
        raise EmailDeferred('email_budget_paused', clock + 3600)
    content = hashlib.sha256(json.dumps([receiver, subject, plain_body, html_body], ensure_ascii=False).encode()).hexdigest()
    logical = notification_id or content
    target = hashlib.sha256(receiver.strip().casefold().encode()).hexdigest()
    key = hashlib.sha256((target + ':' + logical).encode()).hexdigest()
    token = uuid.uuid4().hex

    def admit(old):
        # Idempotency and uncertain acceptance have no 24h expiry. Only the
        # quota window expires; dropping identities could resend an old email.
        entries = dict((old or {}).get('entries', {}))
        previous = entries.get(key)
        if previous and previous['content'] != content:
            raise EmailDeferred('email_identity_content_mismatch', clock + 3600)
        if previous and previous['phase'] == 'accepted':
            return {'entries': entries}, {'already_accepted': True, 'key': key, 'token': previous['token']}
        if previous and previous['phase'] in {'attempting', 'unknown'}:
            raise EmailAcceptanceUnknown('Prior email attempt requires receipt reconciliation')
        active = [v for v in entries.values() if v['target'] == target and v['phase'] != 'failed' and v['at'] > clock-86400]
        if len(active) >= cap:
            raise EmailDeferred('email_24h_budget_deferred', min(v['at'] for v in active) + 86400)
        if mode != 'all' and not daily and sum(not v.get('daily') for v in active) >= max(0,cap-1):
            raise EmailDeferred('daily_summary_slot_reserved', clock + 3600)
        if mode != 'all' and daily and any(v.get('daily') and v.get('day') == day for v in active):
            raise EmailDeferred('one_daily_summary_already_admitted', clock + 3600)
        if mode != 'all' and urgency_key and any(v.get('urgency') == urgency_key for v in active):
            raise EmailDeferred('same_risk_condition_already_admitted', clock + 3600)
        entries[key] = {'target':target, 'content':content, 'at':clock, 'token':token,
                        'phase':'attempting', 'daily':daily, 'day':day, 'urgency':urgency_key}
        return {'entries':entries}, {'already_accepted':False, 'key':key, 'token':token}

    return store, store.state_update(STATE, admit)


def finish(store, admission, phase):
    def update(old):
        old = old or {'entries':{}}
        row = old['entries'].get(admission['key'])
        if row and row['token'] == admission['token']:
            row['phase'] = phase
        return old, None
    store.state_update(STATE, update)


def summary(store, *, now=None):
    """Small user-facing summary; raw diagnostic text stays in local audit."""
    from openinvest.services.notification_outbox import NotificationOutbox
    now = now or datetime.now(timezone.utc)
    box = NotificationOutbox(store)
    with box.connection() as db:
        rows = db.execute("SELECT category,count(*) FROM notifications WHERE created>=? AND category NOT IN ('daily_report','daily_digest') GROUP BY category", (now.timestamp()-86400,)).fetchall()
    labels = {'job_watchdog':'监控状态记录', 'decision_quality':'待核对的分析记录',
              'event':'新闻记录', 'market_risk':'市场变化记录', 'committee_result':'委员会结果'}
    lines = [f"- {labels.get(category, '其他记录')}：{count} 条" for category,count in rows]
    if not lines:
        return ''
    return '\n\n过去24小时记录汇总\n\n' + '\n'.join(lines) + '\n\n待核对不代表新的买卖信号；详细记录已保留，可向助手查询。'


def queue_daily_fallback(box, *, now):
    """If no report has been delivered by 11 local time, queue one cheap summary."""
    from openinvest.core.config import load_config
    from openinvest.services.notifier import render_markdown_email
    cfg = load_config(_force_reload=True).event
    if cfg.email_mode not in {'daily_and_risk', 'daily'} or not cfg.email_enabled:
        return
    local = now.astimezone()
    if local.hour != 11:
        return
    day = local.date().isoformat()
    journal = box.store.state_get(STATE, {}) or {}
    if any(row.get('daily') and row.get('day') == day and row.get('phase') != 'failed'
           for row in journal.get('entries', {}).values()):
        return
    body = summary(box.store, now=now)
    if not body:
        return
    box.enqueue('daily-summary:'+day, {'subject':'openInvest 每日摘要 '+day,
        'report_date':day,
        'plain':'以下为过去24小时的监控记录汇总，不代表投资建议。'+body,
        'html':render_markdown_email('以下为过去24小时的监控记录汇总，不代表投资建议。'+body),
        'discord':'今日监控记录汇总已保存；这不是买卖指令。'}, category='daily_digest',policy='decision',now=now)
