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
    monkeypatch.setattr(server, "_lookups", server.Counter())
    monkeypatch.setattr(server, "_facts_cache", {})
    monkeypatch.setattr(server, "fund_facts", lambda code: None if code.startswith("9") else FACTS)
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


def test_rejects_bad_and_unknown_codes_without_using_debate_quota(client):
    assert client.post("/api/debate", json={"code": "11001"}).status_code == 400
    assert client.post("/api/debate", json={"code": "999999"}).status_code == 404
    assert server._used["*"] == 0 and server._lookups["*"] == 1


def test_lookups_are_limited_before_downloading_and_cached(client, monkeypatch):
    """查基金要下载东方财富净值：额度在下载之前扣，查不到的代码也算；同一只当天只下载一次。"""
    calls = []
    monkeypatch.setattr(server, "fund_facts", lambda code: calls.append(code))
    monkeypatch.setattr(server, "LOOKUP_PER_IP_DAILY", 2)
    assert client.post("/api/debate", json={"code": "900001"}).status_code == 404
    assert client.post("/api/debate", json={"code": "900001"}).status_code == 404   # 缓存命中
    assert client.post("/api/debate", json={"code": "900002"}).status_code == 404
    assert client.post("/api/debate", json={"code": "900003"}).status_code == 429
    assert calls == ["900001", "900002"]


def test_runs_fund_and_returns_only_whitelisted_result(client):
    r = client.post("/api/debate", json={"code": "110011"})
    assert r.status_code == 200 and r.json()["facts"]["name"] == "示例基金"
    j = _wait(client, r.json()["job_id"])
    assert j["result"] == {"symbol": "FUND:110011", "debate_summary": "## 支持的理由\n- x"}


def test_per_ip_and_global_limits(client, monkeypatch):
    for _ in range(server.PER_IP_DAILY):
        assert client.post("/api/debate", json={"code": "110011"}).status_code == 200
    assert client.post("/api/debate", json={"code": "110011"}).status_code == 429
    other = {"x-client-ip": "203.0.113.9"}
    assert client.post("/api/debate", json={"code": "110011"}, headers=other).status_code == 200
    monkeypatch.setattr(server, "GLOBAL_DAILY", server._used["*"])
    third = {"x-client-ip": "203.0.113.10"}
    assert client.post("/api/debate", json={"code": "110011"}, headers=third).status_code == 429


@pytest.mark.parametrize("failure", [
    RuntimeError("secret path /home/x and key sk-123"),                        # 抛异常
    {"status": "error", "error": "行情拉取失败: https://internal/x /home/x"},   # 正常返回但带后端报错
])
def test_failure_does_not_leak_internals(client, monkeypatch, failure):
    def fake(sym):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(server.cooldown, "debate_summary", fake)
    j = _wait(client, client.post("/api/debate", json={"code": "110011"}).json()["job_id"])
    assert j["result"] == {"status": "error", "error": server.GENERIC_ERROR}
