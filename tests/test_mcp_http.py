"""streamable-HTTP transport（remote MCP）测试——鉴权矩阵 + 端到端 tools/list。

全部 in-process：starlette TestClient（context manager 会跑 lifespan——
streamable_http_app 的 lifespan 挂着 session_manager.run()，裸 ASGITransport
不跑 lifespan 会挂，这是本文件最大的坑）。

坑 2：mcp._session_manager 是懒加载单例，同进程重复 build app 会撞
"already initialized"——fixture 每次重置为 None。
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from openinvest.connectors.mcp_server import _BearerAuthMiddleware, mcp
from tests.test_mcp_server import EXPECTED_TOOLS

# MCP streamable-http 协议要求的 Accept 头（stateless + json_response 下响应是纯 JSON）
_HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "0"}},
}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture
def make_app(monkeypatch):
    """工厂：按 env 状态走**生产同款组装**（_configure_http_settings +
    streamable_http_app + 鉴权中间件）。token/allowed_hosts 必须在 build 前设好——
    transport_security 在 app 构建时快照。"""
    from openinvest.connectors.mcp_server import _configure_http_settings

    built = []

    def _make(token=None, allowed_hosts=None):
        if token is None:
            monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("INVEST_API_TOKEN", token)
        if allowed_hosts is None:
            monkeypatch.delenv("INVEST_MCP_ALLOWED_HOSTS", raising=False)
        else:
            monkeypatch.setenv("INVEST_MCP_ALLOWED_HOSTS", allowed_hosts)
        mcp._session_manager = None
        _configure_http_settings("127.0.0.1", 8766)
        app = mcp.streamable_http_app()
        app.add_middleware(_BearerAuthMiddleware)
        built.append(app)
        return app

    yield _make
    mcp._session_manager = None


def _client(app, base_url="http://127.0.0.1:8766"):
    return TestClient(app, base_url=base_url)


def _post_mcp(client, payload, **kw):
    return client.post("/mcp", json=payload, headers={**_HDRS, **kw.pop("headers", {})}, **kw)


def test_auth_matrix(make_app):
    """token 设定下：无头 401 / 错 token 401 / 对 token 放行 / health 豁免。
    token 值不得出现在任何响应体（红线：token 永不进响应）。"""
    with _client(make_app(token="s3cret-token")) as c:
        r = _post_mcp(c, _INIT)
        assert r.status_code == 401
        assert "s3cret-token" not in r.text

        r = _post_mcp(c, _INIT, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        r = _post_mcp(c, _INIT, headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code != 401, r.text
        assert "s3cret-token" not in r.text

        assert c.get("/health").json() == {"status": "ok"}, "/health 必须豁免鉴权"


def test_no_token_passthrough(make_app):
    """不设 token → 直通（对齐 REST '不设即开' 的 loopback 开发语义）。"""
    with _client(make_app()) as c:
        assert _post_mcp(c, _INIT).status_code != 401


def test_token_env_read_per_request(make_app, monkeypatch):
    """每请求读 env：app 建好后换 token 立即生效（systemd reload 语义）。
    （鉴权中间件每请求读 env；transport_security 是 build 时快照——这里从
    无 token 状态 build，Host 校验保持 loopback 白名单，用 loopback client）"""
    with _client(make_app()) as c:
        assert _post_mcp(c, _INIT).status_code != 401
        monkeypatch.setenv("INVEST_API_TOKEN", "late-token")
        assert _post_mcp(c, _INIT).status_code == 401
        assert _post_mcp(
            c, _INIT, headers={"Authorization": "Bearer late-token"}
        ).status_code != 401


def test_tools_list_matches_stdio_snapshot(make_app):
    """端到端 initialize + tools/list：HTTP transport 暴露的工具集合必须与
    stdio 快照（test_mcp_server.EXPECTED_TOOLS）完全一致——双 transport 共守，
    防单边漂移。"""
    auth = {"Authorization": "Bearer tok"}
    with _client(make_app(token="tok")) as c:
        r = _post_mcp(c, _INIT, headers=auth)
        assert r.status_code == 200, r.text
        r = _post_mcp(c, _LIST, headers=auth)
        assert r.status_code == 200, r.text
        tools = {t["name"] for t in r.json()["result"]["tools"]}
        assert tools == EXPECTED_TOOLS


def test_refuses_nonloopback_without_token(monkeypatch):
    """非 loopback 绑定 + 无 token → 拒绝启动（信任边界不裸奔）。"""
    from openinvest.connectors.mcp_server import _serve_http

    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    monkeypatch.setenv("INVEST_MCP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="INVEST_API_TOKEN"):
        _serve_http()


def test_token_mode_accepts_proxied_public_host(make_app):
    """回归（review 真机踩过 421）：文档推荐形态 = 绑 loopback + Caddy 反代，
    下游请求 Host 是公网域名。token 模式下 Host 校验必须关闭——否则合法流量
    全部 421 Invalid Host Header。"""
    auth = {"Authorization": "Bearer tok"}
    app = make_app(token="tok")
    with _client(app, base_url="http://invest.example.com") as c:
        r = _post_mcp(c, _INIT, headers=auth)
        assert r.status_code == 200, f"公网 Host 被拒（{r.status_code}）: {r.text[:200]}"
        # 鉴权仍然有效：同一 app 无 token 头 → 401
        assert _post_mcp(c, _INIT).status_code == 401


def test_allowed_hosts_whitelist_mode(make_app):
    """INVEST_MCP_ALLOWED_HOSTS 显式白名单：名单内放行、名单外 421 类拒绝。
    （session_manager 每 app 只能 run 一次 lifespan → 两段各 build 一个 app）"""
    auth = {"Authorization": "Bearer tok"}
    app = make_app(token="tok", allowed_hosts="invest.example.com")
    with _client(app, base_url="http://invest.example.com") as c:
        assert _post_mcp(c, _INIT, headers=auth).status_code == 200

    app2 = make_app(token="tok", allowed_hosts="invest.example.com")
    with _client(app2, base_url="http://evil.example.com") as c:
        r = _post_mcp(c, _INIT, headers=auth)
        assert r.status_code >= 400 and r.status_code != 401, \
            f"名单外 Host 应被 Host 校验拒（非 401 鉴权拒），实际 {r.status_code}"
