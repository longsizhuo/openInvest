"""Real TCP + official MCP client regression tests (no market/LLM requests).

This is same-machine integration coverage, not a second-device/Cloudflare test.
Only the slow business dependencies are replaced; transport and tool dispatch
use the production application.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import socket
import threading
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from openinvest.connectors import mcp_server
from tests.test_mcp_server import EXPECTED_TOOLS


@pytest.fixture
def http_server(monkeypatch):
    monkeypatch.setenv("INVEST_API_TOKEN", "integration-test-token")
    monkeypatch.setattr(mcp_server.mcp, "settings", mcp_server.mcp.settings.model_copy(deep=True))
    monkeypatch.setattr(mcp_server.mcp, "_session_manager", None)
    monkeypatch.setattr(mcp_server, "_COMMITTEE_LOCK", asyncio.Lock())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        mcp_server._configure_http_settings("127.0.0.1", port)
        app = mcp_server.mcp.streamable_http_app()
        app.add_middleware(mcp_server._BearerAuthMiddleware)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", loop="asyncio"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "HTTP server failed to start"
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "HTTP server did not stop"


@asynccontextmanager
async def session(base_url):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer integration-test-token"},
        timeout=10, trust_env=False,
    ) as http:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http) as (read, write, get_id):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                await client.initialize()
                assert get_id() is None, "HTTP must remain stateless"
                yield client


def test_real_client_reconnect_and_tool_calls(http_server, monkeypatch):
    monkeypatch.setattr("openinvest.services.skill_views.build_status_view", lambda: {"cash": {"CNY": 123}})
    monkeypatch.setattr("openinvest.services.skill_views.build_strategy_view", lambda: {"target_assets": []})

    async def go():
        for _ in range(2):
            async with session(http_server) as client:
                assert {t.name for t in (await client.list_tools()).tools} == EXPECTED_TOOLS
                result = await client.call_tool("status", {})
                assert not result.isError
                assert result.structuredContent["result"] == {"cash": {"CNY": 123}}
                assert not (await client.call_tool("strategy", {})).isError

    asyncio.run(go())


def test_slow_status_does_not_block_second_client(http_server, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_status():
        started.set()
        assert release.wait(8), "test did not release status"
        return {"cash": {"CNY": 123}}

    monkeypatch.setattr("openinvest.services.skill_views.build_status_view", slow_status)

    async def go():
        async with session(http_server) as first, session(http_server) as second:
            task = asyncio.create_task(first.call_tool("status", {}))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                # Must complete while the first call is deliberately still blocked.
                listed = await asyncio.wait_for(second.list_tools(), timeout=2)
                assert {t.name for t in listed.tools} == EXPECTED_TOOLS
                async with httpx.AsyncClient(trust_env=False) as http:
                    assert (await http.get(f"{http_server}/health", timeout=2)).json() == {"status": "ok"}
            finally:
                release.set()
                result = await task
                assert not result.isError

    asyncio.run(go())


def test_http_committee_progress_arrives_before_completion(http_server, monkeypatch):
    release = threading.Event()

    def slow_committee(*, symbols, max_debate_rounds, progress_callback):
        progress_callback({"phase": "round_1_start"})
        assert release.wait(8), "test did not release committee"
        progress_callback({"phase": "cio_done"})
        return {"asset_committees": {symbols[0]: {"verdict": {"verdict": "HOLD", "confidence": 0.5}}}}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session", slow_committee)
    monkeypatch.setattr("openinvest.jobs.verdict_review.load_confidence_lookup", lambda: {})

    async def go():
        progress = asyncio.Event()
        phases = []

        async def on_progress(value, total, message):
            phases.append(message)
            progress.set()

        async with session(http_server) as client:
            task = asyncio.create_task(client.call_tool(
                "run_committee", {"symbol": "TEST", "force": True}, progress_callback=on_progress,
            ))
            try:
                await asyncio.wait_for(progress.wait(), timeout=2)
                assert not task.done(), "progress must be streamed before completion"
            finally:
                release.set()
                result = await task
            assert not result.isError
            assert result.structuredContent["result"]["cached"] is False
            assert phases == ["round_1_start", "cio_done"]

    asyncio.run(go())


def test_two_clients_deposit_without_lost_updates(http_server, monkeypatch, tmp_path):
    """Moving sync dispatch to workers must preserve the real ledger locks."""
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.portfolio_manager import PortfolioManager
    from tests.test_onboarding_smoke import _seed_minimal_memory

    _seed_minimal_memory(tmp_path)
    store = MemoryStore(tmp_path / "memory")
    monkeypatch.setattr(mcp_server, "_pm", lambda: PortfolioManager(store))

    async def go():
        async with session(http_server) as first, session(http_server) as second:
            results = await asyncio.gather(*(
                client.call_tool("deposit", {"currency": "CNY", "amount": 1})
                for client in (first, second) * 5
            ))
            assert all(not r.isError and r.structuredContent["result"]["status"] == "ok" for r in results)

    asyncio.run(go())
    assert PortfolioManager(store).cash_amount("CNY") == 20010
