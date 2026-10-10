"""演示站接口：校验、限频、排队、出口、依据回填。网络和 LLM 全部替身。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import server

FACTS = {"name": "示例基金", "code": "110011", "nav": 1.0, "nav_date": "2026-01-05",
         "chips": ["近 1 年 -29.8%"], "chart": {}, "dist": [],
         "metrics": {"price_pct": {"text": "两年价格分位 4%", "hot": False, "chip": ""},
                     "tnx": {"text": "10Y 美债 5.24%，两年分位 99%", "hot": True, "chip": ""}}}
SCRIBE = {"symbol": "FUND:110011", "headline": "价格在低位，趋势仍向下",
          "pros": [{"role": "量化", "claim": "两年低位", "metric": "price_pct"}],
          "cons": [{"role": "宏观", "claim": "利率两年最高", "metric": "tnx"},
                   {"role": "风险", "claim": "回撤深", "metric": "none"}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_used", server.Counter())
    monkeypatch.setattr(server, "_lookups", server.Counter())
    monkeypatch.setattr(server, "_facts_cache", {})
    monkeypatch.setattr(server, "fund_facts", lambda code: None if code.startswith("9") else FACTS)
    monkeypatch.setattr(server.cooldown, "debate_summary", lambda sym, metrics: SCRIBE)
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


def test_facts_go_out_without_internal_metrics_table(client):
    r = client.post("/api/debate", json={"code": "110011"}).json()
    assert r["facts"]["name"] == "示例基金" and "metrics" not in r["facts"]


def test_evidence_is_filled_from_computed_metrics(client):
    """依据数值按指标键从程序算的 metrics 回填；标红跟着 metrics 的 hot 走。"""
    j = _wait(client, client.post("/api/debate", json={"code": "110011"}).json()["job_id"])
    assert j["result"] == {
        "headline": "价格在低位，趋势仍向下",
        "pros": [{"role": "量化", "claim": "两年低位", "evidence": "两年价格分位 4%", "hot": False}],
        "cons": [{"role": "宏观", "claim": "利率两年最高", "evidence": "10Y 美债 5.24%，两年分位 99%", "hot": True},
                 {"role": "风险", "claim": "回撤深", "evidence": "", "hot": False}],
    }


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
    def fake(sym, metrics):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(server.cooldown, "debate_summary", fake)
    j = _wait(client, client.post("/api/debate", json={"code": "110011"}).json()["job_id"])
    assert j["result"] == {"status": "error", "error": server.GENERIC_ERROR}


def test_metrics_hot_rules():
    """标红按固定阈值，不由模型决定。"""
    m = server._metrics({"ret_1y": -29.8, "ret_6m": -10, "scale_now": 67.77, "scale_prev": 127.81,
                         "stock_now": 81.3, "stock_prev": 93.6, "mdd": -33.0, "from_high": -31.5,
                         "price_pct": 4, "ma_spread": -5.8, "rsi": 40.6,
                         "vix": 14.84, "vix_pct": 10, "tnx": 5.24, "tnx_pct": 99})
    hot = {k for k, v in m.items() if v["hot"]}
    assert hot == {"ret_1y", "scale", "stock", "drawdown", "from_high", "tnx"}
    assert m["scale"]["text"] == "规模一年 -47%"
