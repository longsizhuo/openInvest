"""verdict 邮件 — 事件触发的委员会跑完后补发。

修复 event_watch → 委员会 → verdict 邮件断链：web 路径 (_run_committee_task) 之前
跑完不发邮件，event 预警里"verdict 邮件随后送达"成空头支票。
"""
from __future__ import annotations

from openinvest.services import event_notifier


def _patch_email(monkeypatch, sink: dict):
    monkeypatch.setattr(event_notifier, "render_markdown_email", lambda md, **k: md)

    def _send(*, subject, html_body, plain_body):
        sink.update(subject=subject, body=plain_body)
        return "longsizhuo@gmail.com"

    monkeypatch.setattr(event_notifier, "send_email_html", _send)


def test_verdict_email_renders_verdict(monkeypatch):
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {
        "GC=F": {
            "verdict": {"verdict": "TRIM", "confidence": 0.82,
                        "dominant_view": "risk", "alloc_cny": -20000},
            "error": None,
        }
    }
    rcv = event_notifier.send_committee_verdict_email(
        task_id="ae69eb380b01", symbols=["GC=F"],
        by_asset=by_asset, event_ids=["7d0205cc9042eb13"],
    )
    assert rcv == "longsizhuo@gmail.com"
    assert "TRIM" in sink["subject"]
    body = sink["body"]
    assert "GC=F" in body and "TRIM" in body
    assert "0.82" in body                       # confidence
    assert "ae69eb380b01" in body               # task link
    assert "7d0205cc9042eb13" in body           # 触发事件


def test_verdict_email_handles_errored_asset(monkeypatch):
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {"NDQ.AX": {"verdict": {}, "error": "boom"}}
    event_notifier.send_committee_verdict_email(
        task_id="t1", symbols=["NDQ.AX"], by_asset=by_asset,
    )
    assert "运行失败" in sink["body"] and "boom" in sink["body"]
