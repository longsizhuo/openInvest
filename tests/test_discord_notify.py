"""discord_notify 与 event_notifier Discord 精简文案的测试。

网络层 mock 掉——只验证 best-effort 语义（未配置跳过 / 失败不抛）与文案组装。
"""
from __future__ import annotations

from openinvest.services.discord_notify import send_discord_alert
from openinvest.services.event_notifier import _build_discord_text

_EVENTS = [
    {
        "one_line_claim": "美联储意外加息 50bp",
        "stance": "risk",
        "severity": "high",
        "affected_symbols": ["510300.SS", "GC=F"],
        "ts": "2026-07-14T10:00:00",
    },
    {
        "one_line_claim": "ASX 科技板块财报超预期",
        "stance": "opportunity",
        "severity": "medium",
        "affected_symbols": ["NDQ.AX"],
        "ts": "2026-07-14T10:05:00",
    },
]


def test_skip_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("CHATBOT_ALERT_URL", raising=False)
    monkeypatch.delenv("CHATBOT_INTERNAL_KEY", raising=False)
    # 未配置：不发请求直接 False（fork 用户路径）
    assert send_discord_alert("hi") is False


def test_post_success_and_failure(monkeypatch) -> None:
    monkeypatch.setenv("CHATBOT_ALERT_URL", "http://127.0.0.1:6200/alert/invest")
    monkeypatch.setenv("CHATBOT_INTERNAL_KEY", "k")

    calls = []

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.text = ""

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        return _Resp(200)

    monkeypatch.setattr("openinvest.services.discord_notify.requests.post", fake_post)
    assert send_discord_alert("hello") is True
    assert calls[0]["json"]["type"] == "invest_event"
    assert calls[0]["headers"]["X-Internal-Key"] == "k"

    # 非 200 → False；异常 → False（永不上抛）
    monkeypatch.setattr(
        "openinvest.services.discord_notify.requests.post",
        lambda url, **kw: _Resp(503),
    )
    assert send_discord_alert("hello") is False

    def boom(url, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr("openinvest.services.discord_notify.requests.post", boom)
    assert send_discord_alert("hello") is False


def test_build_discord_text_compact() -> None:
    text = _build_discord_text(
        _EVENTS, committee_task_id="task-42", api_base_url="http://localhost:8765"
    )
    assert "美联储意外加息 50bp" in text
    assert "510300.SS" in text
    assert "task-42" in text
    # 一行一事件 + 标题 + 委员会链接 + 尾注，绝不超 Discord 消息上限
    assert len(text) < 1900


def test_build_discord_text_truncates_events() -> None:
    many = _EVENTS * 5  # 10 条
    text = _build_discord_text(many, committee_task_id=None, api_base_url="http://x")
    assert "另有 4 条" in text
