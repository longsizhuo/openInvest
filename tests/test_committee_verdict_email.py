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
    from openinvest.jobs import verdict_review
    monkeypatch.setattr(verdict_review, "load_confidence_lookup",
                        lambda: {"by_verdict": {"TRIM": {"n": 33, "rate": 0.6, "from": "default"}}})
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
    # D10 P1：给同类决议查表，自报原数只小字留档
    assert "同类 TRIM 30 天后方向判对的比例 60%（n=33；默认表）（自报 0.82）" in body
    assert "confidence 0.82" not in body
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


def test_verdict_email_link_has_api_prefix(monkeypatch):
    """2026-07-15 修复：漏了 /api 前缀——链接实际打不到委员会 JSON 端点
    （/committee/<id> 落进已退役 GUI 的 SPA fallback，不是 /api/committee/<id>）。"""
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {"GC=F": {"verdict": {"verdict": "ACCUMULATE", "confidence": 0.65,
                                     "dominant_view": "risk", "alloc_cny": 2700},
                         "error": None}}
    event_notifier.send_committee_verdict_email(
        task_id="abc123", symbols=["GC=F"], by_asset=by_asset,
        api_base_url="https://invest.example.com",
    )
    assert "https://invest.example.com/api/committee/abc123" in sink["body"]
    assert "https://invest.example.com/committee/abc123" not in sink["body"]


def test_verdict_email_has_explain_decision_fallback(monkeypatch):
    """localhost 默认链接在邮件/DM 里打不开——explain_decision 是零配置能用的路，
    每个资产都要有，decision_id 格式必须是 <今天日期>/<symbol>（explain_decision 契约）。"""
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {"GC=F": {"verdict": {"verdict": "ACCUMULATE", "confidence": 0.65,
                                     "dominant_view": "risk", "alloc_cny": 2700},
                         "error": None}}
    event_notifier.send_committee_verdict_email(
        task_id="abc123", symbols=["GC=F"], by_asset=by_asset,
    )
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    assert f'explain_decision("{today}/GC=F")' in sink["body"]


def test_verdict_email_silent_when_all_hold(monkeypatch):
    """2026-09-10 老大要求：全 HOLD 静默不打扰（只推非 HOLD 裁决）。
    没有邮件 → 返回空串、sink 保持空。"""
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {
        "GC=F": {"verdict": {"verdict": "HOLD", "confidence": 0.7,
                             "dominant_view": "quant", "alloc_cny": 0}, "error": None},
        "510300.SS": {"verdict": {"verdict": "HOLD", "confidence": 0.6,
                                  "dominant_view": "quant", "alloc_cny": 0}, "error": None},
    }
    rcv = event_notifier.send_committee_verdict_email(
        task_id="hold123", symbols=["GC=F", "510300.SS"], by_asset=by_asset,
    )
    assert rcv == ""
    assert sink == {}


def test_verdict_email_still_sent_on_error(monkeypatch):
    """全 HOLD 但有运行失败 → 仍要通知，不能把失败静默掉。"""
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    by_asset = {
        "GC=F": {"verdict": {"verdict": "HOLD", "confidence": 0.7,
                             "dominant_view": "quant", "alloc_cny": 0}, "error": None},
        "NDQ.AX": {"verdict": {}, "error": "boom"},
    }
    rcv = event_notifier.send_committee_verdict_email(
        task_id="err123", symbols=["GC=F", "NDQ.AX"], by_asset=by_asset,
    )
    assert rcv == "longsizhuo@gmail.com"
    assert "boom" in sink["body"]


def test_event_alert_silent_by_default(monkeypatch):
    """事件预警默认静默（未设 INVEST_EVENT_ALERT=1 时）：不推 Discord 不发邮件。"""
    sink: dict = {}
    _patch_email(monkeypatch, sink)
    monkeypatch.delenv("INVEST_EVENT_ALERT", raising=False)
    rcv = event_notifier.send_event_alert(
        [{"one_line_claim": "test claim", "stance": "opportunity", "severity": "mid",
          "affected_symbols": ["GC=F"], "ts": "2026-09-10T00:00:00Z"}],
    )
    assert rcv == ""
    assert sink == {}
