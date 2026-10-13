"""测试进程里对外发送的出口拿不到凭证（conftest 的 OUTBOUND_KEYS 闸）。

CI 没有 .env，这条恒绿；开发机上 load_dotenv() 已把 .env 灌进 os.environ 时，它才真正把关：
少清一个凭证，出口就会走到下面替身的 requests.post / smtplib.SMTP 而让测试失败。
"""
from openinvest.services import discord_notify, event_notifier, notifier


def _boom(*a, **k):
    raise AssertionError("测试里发生了真实对外发送")


def test_outbound_channels_are_inert(monkeypatch):
    monkeypatch.setattr(discord_notify.requests, "post", _boom)
    monkeypatch.setattr(notifier.smtplib, "SMTP", _boom)
    assert discord_notify.send_discord_alert("x") is False
    assert notifier.send_email_html(subject="x", html_body="x", plain_body="x") == ""
    # 2026-10-13 漏网的那条路径：verdict 邮件先推 DM 再发信，测试只 patch 了发信
    monkeypatch.setattr(event_notifier, "send_discord_alert", discord_notify.send_discord_alert)
    event_notifier.send_committee_verdict_email(
        task_id="t", symbols=["GC=F"], by_asset={"GC=F": {"error": "boom"}})
