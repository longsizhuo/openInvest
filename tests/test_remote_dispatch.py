"""scripts/remote_dispatch.py — 远端模式分发契约

守的契约：
- 路由表：命令 → 正确的 method/path/body（hub 端点）
- 输出与本地 CLI 同形状（2xx 原样打印；400 → status=error JSON + exit 1）
- init 远端禁用、live_prices/correlate 本地白名单、未归类命令默认拒绝
- 鉴权 header（INVEST_API_TOKEN / CF service token）正确携带
- run_committee 的 触发→轮询→渲染 全链路（含同日 cache 用 hub 日期口径）

全程 monkeypatch requests.request，不打网络。
"""
from __future__ import annotations

import io
import json
import sys
from argparse import Namespace
from typing import Any, Dict, List, Optional

import pytest

import openinvest.remote_dispatch as rd

BASE = "http://hub.test:8765"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status {self.status_code}")


@pytest.fixture
def remote_env(monkeypatch):
    monkeypatch.setenv("INVEST_API_BASE", BASE)
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)


@pytest.fixture
def recorder(monkeypatch, remote_env):
    """记录所有 requests.request 调用；按 (method, url) 查表返回响应"""
    calls: List[Dict[str, Any]] = []
    routes: Dict[str, Any] = {}   # "METHOD url" → FakeResponse | [FakeResponse, ...]

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        calls.append({
            "method": method, "url": url, "json": json,
            "params": params, "headers": headers or {}, "timeout": timeout,
        })
        key = f"{method} {url}"
        resp = routes.get(key)
        if resp is None:
            raise AssertionError(f"未预设的请求: {key}")
        if isinstance(resp, list):
            return resp.pop(0) if len(resp) > 1 else resp[0]
        return resp

    monkeypatch.setattr(rd.requests, "request", fake_request)
    return {"calls": calls, "routes": routes}


def _out_json(capfd) -> Dict[str, Any]:
    raw = capfd.readouterr().out
    return json.loads(raw[raw.index("{"):])


# ---------- 读命令路由 ----------

def test_status_routes_to_skill_status(recorder, capfd):
    payload = {"user": {"name": "X"}, "cash": {"cny": 1.0}}
    recorder["routes"][f"GET {BASE}/api/skill/status"] = FakeResponse(200, payload)

    assert rd.maybe_dispatch_remote(Namespace(cmd="status")) is True
    assert recorder["calls"][0]["method"] == "GET"
    assert _out_json(capfd) == payload


def test_history_passes_n_param(recorder, capfd):
    recorder["routes"][f"GET {BASE}/api/skill/history"] = FakeResponse(
        200, {"recent_trades": [], "recent_debates": []})
    rd.maybe_dispatch_remote(Namespace(cmd="history", n=5))
    assert recorder["calls"][0]["params"] == {"n": 5}


def test_what_if_builds_body_from_args(recorder, capfd):
    recorder["routes"][f"POST {BASE}/api/skill/what_if"] = FakeResponse(200, {"status": "ok"})
    rd.maybe_dispatch_remote(Namespace(
        cmd="what_if", symbol="NDQ.AX", pct=-5.0, price=None,
        gold_price=None, gold_pct=None, ndq_price=None, ndq_pct=None, audcny=None,
    ))
    body = recorder["calls"][0]["json"]
    assert body["symbol"] == "NDQ.AX" and body["pct"] == -5.0


# ---------- 写命令 + 错误映射 ----------

def test_buy_maps_400_to_cli_error(recorder, capfd):
    recorder["routes"][f"POST {BASE}/api/skill/buy"] = FakeResponse(
        400, {"detail": "units / price 必须 > 0"})
    with pytest.raises(SystemExit) as exc:
        rd.maybe_dispatch_remote(Namespace(
            cmd="buy", symbol="X", units=-1, price=1,
            currency="CNY", kind="etf", unit_label="股",
        ))
    assert exc.value.code == 1
    out = _out_json(capfd)
    assert out["status"] == "error" and "units / price" in out["error"]


def test_deposit_routes_and_prints_cli_shape(recorder, capfd):
    payload = {"status": "ok", "currency": "CNY", "amount_deposited": 100.0,
               "new_balance": 1100.0, "cny_total": 1100.0}
    recorder["routes"][f"POST {BASE}/api/skill/deposit"] = FakeResponse(200, payload)
    rd.maybe_dispatch_remote(Namespace(cmd="deposit", currency="CNY", amount=100))
    assert recorder["calls"][0]["json"] == {"currency": "CNY", "amount": 100.0}
    assert _out_json(capfd) == payload


# ---------- 禁用 / 白名单 / 默认拒绝 ----------

def test_init_disabled_without_http(recorder, capfd):
    with pytest.raises(SystemExit) as exc:
        rd.maybe_dispatch_remote(Namespace(cmd="init", from_stdin=True, force=False))
    assert exc.value.code == 1
    assert not recorder["calls"], "禁用命令不应发任何 HTTP"
    out = _out_json(capfd)
    assert "hub" in out["hint"]


def test_local_whitelist_falls_through(recorder):
    assert rd.maybe_dispatch_remote(Namespace(cmd="live_prices")) is False
    assert rd.maybe_dispatch_remote(Namespace(cmd="correlate")) is False
    assert not recorder["calls"]


def test_unknown_command_rejected_by_default(recorder, capfd):
    """新子命令必须显式归类——防远端模式静默写客户端本地 memory/"""
    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="some_future_cmd"))
    assert not recorder["calls"]
    assert "尚未支持远端模式" in _out_json(capfd)["error"]


def test_event_check_recall_disabled(recorder, capfd):
    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="event_check", recall="NDQ.AX", live=False))
    assert not recorder["calls"]


# ---------- 鉴权 header ----------

def test_bearer_token_header(recorder, monkeypatch, capfd):
    monkeypatch.setenv("INVEST_API_TOKEN", "sek-ret")
    recorder["routes"][f"GET {BASE}/api/skill/strategy"] = FakeResponse(200, {})
    rd.maybe_dispatch_remote(Namespace(cmd="strategy"))
    assert recorder["calls"][0]["headers"]["Authorization"] == "Bearer sek-ret"


def test_cf_access_service_token_headers(recorder, monkeypatch, capfd):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "id.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "shhh")
    recorder["routes"][f"GET {BASE}/api/skill/strategy"] = FakeResponse(200, {})
    rd.maybe_dispatch_remote(Namespace(cmd="strategy"))
    h = recorder["calls"][0]["headers"]
    assert h["CF-Access-Client-Id"] == "id.access"
    assert h["CF-Access-Client-Secret"] == "shhh"


def test_401_dies_with_token_hint(recorder, capfd):
    recorder["routes"][f"GET {BASE}/api/skill/status"] = FakeResponse(401, {"detail": "unauthorized"})
    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="status"))
    assert "INVEST_API_TOKEN" in _out_json(capfd)["hint"]


def test_connection_error_dies_with_hint(monkeypatch, remote_env, capfd):
    def boom(*a, **k):
        raise rd.requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(rd.requests, "request", boom)
    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="status"))
    out = _out_json(capfd)
    assert "无法连接 hub" in out["error"] and "INVEST_API_BASE" in out["hint"]


# ---------- committee 三命令 ----------

def test_prepare_committee_posts_symbol(recorder, capfd):
    recorder["routes"][f"POST {BASE}/api/committee/prepare"] = FakeResponse(
        200, {"asset": {"symbol": "NDQ.AX"}, "prompts": {}})
    rd.maybe_dispatch_remote(Namespace(cmd="prepare_committee", symbol="NDQ.AX"))
    assert recorder["calls"][0]["json"] == {"symbol": "NDQ.AX"}


def test_save_committee_pipes_stdin(recorder, monkeypatch, capfd):
    transcript = "=== CIO ===\nverdict: HOLD\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(transcript))
    recorder["routes"][f"POST {BASE}/api/committee/save"] = FakeResponse(
        200, {"saved": "/hub/path.md", "verdict": {"verdict": "HOLD"}})
    rd.maybe_dispatch_remote(Namespace(cmd="save_committee", symbol="NDQ.AX"))
    body = recorder["calls"][0]["json"]
    assert body == {"symbol": "NDQ.AX", "transcript": transcript}
    assert _out_json(capfd)["verdict"]["verdict"] == "HOLD"


def test_save_committee_empty_stdin_no_http(recorder, monkeypatch, capfd):
    monkeypatch.setattr(sys, "stdin", io.StringIO("  \n"))
    assert rd.maybe_dispatch_remote(Namespace(cmd="save_committee", symbol="NDQ.AX")) is True
    assert not recorder["calls"]
    assert _out_json(capfd) == {"error": "empty stdin"}


def test_run_committee_full_flow(recorder, monkeypatch, capfd):
    """触发 → 轮询(running→done) → 输出 verdict/cio_memo；日期取 hub 时间戳"""
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    r = recorder["routes"]
    r[f"GET {BASE}/api/health"] = FakeResponse(
        200, {"ok": True, "timestamp": "2026-06-12T10:00:00+08:00"})
    r[f"GET {BASE}/api/committee_sessions/2026-06-12/NDQ_AX"] = FakeResponse(404, {})
    r[f"POST {BASE}/api/committee/run"] = FakeResponse(
        200, {"task_id": "tsk123", "status": "queued", "started_at": "x",
              "poll_url": "/api/committee/tsk123"})
    r[f"GET {BASE}/api/committee/tsk123"] = [
        FakeResponse(200, {"task_id": "tsk123", "status": "running", "phase": "macro_start"}),
        FakeResponse(200, {
            "task_id": "tsk123", "status": "done", "phase": "done",
            "result": {"by_asset": {"NDQ.AX": {
                "verdict": {"verdict": "HOLD", "confidence": 0.6},
                "cio_memo": "## verdict\nHOLD",
            }}},
        }),
    ]

    rd.maybe_dispatch_remote(Namespace(
        cmd="run_committee", symbol="NDQ.AX", force=False, max_rounds=1))
    out = _out_json(capfd)
    assert out["status"] == "ok"
    assert out["verdict"]["verdict"] == "HOLD"
    assert out["cio_memo"].startswith("## verdict")
    run_call = next(c for c in recorder["calls"] if c["url"].endswith("/api/committee/run"))
    assert run_call["json"]["symbols"] == ["NDQ.AX"]
    assert run_call["json"]["max_debate_rounds"] == 1


def test_run_committee_same_day_cache(recorder, capfd):
    r = recorder["routes"]
    r[f"GET {BASE}/api/health"] = FakeResponse(
        200, {"ok": True, "timestamp": "2026-06-12T10:00:00+08:00"})
    r[f"GET {BASE}/api/committee_sessions/2026-06-12/NDQ_AX"] = FakeResponse(
        200, {"content": "# Committee: NDQ.AX\n..."})

    rd.maybe_dispatch_remote(Namespace(
        cmd="run_committee", symbol="NDQ.AX", force=False, max_rounds=1))
    out = _out_json(capfd)
    assert out["status"] == "cached"
    assert "--force" in out["reason"]
    assert out["transcript_md"].startswith("# Committee")
    # cache 命中后不应触发 run
    assert not any(c["url"].endswith("/api/committee/run") for c in recorder["calls"])


def test_doctor_appends_remote_section(recorder, monkeypatch, capfd):
    monkeypatch.setenv("INVEST_API_TOKEN", "sekret-do-not-echo")
    recorder["routes"][f"GET {BASE}/api/doctor"] = FakeResponse(
        200, {"status": "ready", "checks": []})
    rd.maybe_dispatch_remote(Namespace(cmd="doctor"))
    out = _out_json(capfd)
    assert out["status"] == "ready"
    assert out["remote"]["api_base"] == BASE
    assert out["remote"]["auth"] == "bearer_token"
    # 输出绝不回显 token
    assert "sekret-do-not-echo" not in json.dumps(out)
