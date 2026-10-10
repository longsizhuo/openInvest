"""演示站接口：校验、限频、排队、出口。网络和 LLM 全部替身。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import server

FACTS = {"name": "示例基金", "nav": 1.0, "nav_date": "2026-01-05", "max_drawdown_2y": -30.0,
         "from_high_2y": -10.0, "peak": "2025-01-02", "trough": "2025-06-02", "series": []}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_used", server.Counter())
    monkeypatch.setattr(server, "fund_facts", lambda code: None if code == "999999" else FACTS)
    monkeypatch.setattr(server.cooldown, "debate_summary",
                        lambda sym: {"symbol": sym, "debate_summary": "## 支持的理由\n- x"})
    return TestClient(server.app)


def _wait(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/debate/{job_id}").json()
        if j["status"] == "done":
            return j
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_rejects_bad_and_unknown_codes_without_using_quota(client):
    assert client.post("/api/debate", json={"code": "11001"}).status_code == 400
    assert client.post("/api/debate", json={"code": "999999"}).status_code == 404
    assert server._used["*"] == 0


def test_runs_fund_and_returns_only_whitelisted_result(client):
    r = client.post("/api/debate", json={"code": "110011"})
    assert r.status_code == 200 and r.json()["facts"]["name"] == "示例基金"
    j = _wait(client, r.json()["job_id"])
    assert j["result"] == {"symbol": "FUND:110011", "debate_summary": "## 支持的理由\n- x"}


def test_per_ip_and_global_limits(client, monkeypatch):
    for _ in range(server.PER_IP_DAILY):
        assert client.post("/api/debate", json={"code": "110011"}).status_code == 200
    assert client.post("/api/debate", json={"code": "110011"}).status_code == 429
    other = {"cf-connecting-ip": "203.0.113.9"}
    assert client.post("/api/debate", json={"code": "110011"}, headers=other).status_code == 200
    monkeypatch.setattr(server, "GLOBAL_DAILY", server._used["*"])
    third = {"cf-connecting-ip": "203.0.113.10"}
    assert client.post("/api/debate", json={"code": "110011"}, headers=third).status_code == 429


def test_failure_does_not_leak_internals(client, monkeypatch):
    def boom(sym):
        raise RuntimeError("secret path /home/x and key sk-123")
    monkeypatch.setattr(server.cooldown, "debate_summary", boom)
    j = _wait(client, client.post("/api/debate", json={"code": "110011"}).json()["job_id"])
    assert j["result"] == {"status": "error", "error": "这次辩论没跑成，稍后再试"}
