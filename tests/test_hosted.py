"""Hosted control-plane security and data lifecycle with synthetic accounts only."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
pytest.importorskip("authlib")
import httpx
import jwt
from starlette.testclient import TestClient

from openinvest.hosted.app import create_app
from openinvest.hosted.auth import Authenticator, AuthenticationError
from openinvest.hosted.config import Settings
from openinvest.hosted.pool import Busy
from openinvest.hosted.research import Research
from openinvest.hosted.store import Store, Conflict


def test_preview_worker_discovery_cannot_adopt_production_containers(settings):
    import docker
    from unittest.mock import Mock
    from openinvest.hosted.pool import DockerPool
    client = SimpleNamespace(containers=SimpleNamespace(list=Mock(return_value=[]), get=Mock(side_effect=docker.errors.NotFound('synthetic missing worker'))))
    pool = DockerPool(replace(settings, worker_namespace='test-owned-preview'), client=client)
    asyncio.run(pool.recover())
    client.containers.list.assert_called_once_with(all=True, filters={'label':'openinvest.hosted=test-owned-preview'})

    client.containers.list.reset_mock()
    client.containers.list.return_value = [object()] * settings.workers
    with pytest.raises(Busy):
        asyncio.run(pool._attach('0123456789abcdef0123456789abcdef'))
    client.containers.list.assert_called_once_with(all=True, filters={'label':'openinvest.hosted=test-owned-preview'})


def test_browser_login_lands_on_personal_home_and_mcp_handoff_stays_on_connect(environment, monkeypatch):
    env = environment
    async def token(_request):
        return {'userinfo': {'sub': env.accounts[0][0]['subject']}}
    monkeypatch.setattr(env.app.state.oauth.auth0, 'authorize_access_token', token)
    response = env.client.get('/auth/callback', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/account/'
    account = env.accounts[0][0]
    ticket = jwt.encode({'sub': account['subject'], 'client_id': 'synthetic-home-client', 'aud': env.settings.origin,
                         'exp': int(time.time())+300, 'jti': 'synthetic-home-handoff'}, env.settings.action_secret, algorithm='HS256')
    handoff = env.client.get('/auth/action', params={'state':'synthetic-state','session_token':ticket}, follow_redirects=False)
    assert handoff.status_code == 303 and handoff.headers['location'] == '/connect'


def test_config_check_does_not_expose_secrets_or_create_state(tmp_path, monkeypatch, capsys):
    from openinvest.hosted import cli
    from openinvest.hosted.config import REQUIRED_ENV
    import sys
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOSTED_DATA_DIR", str(tmp_path / "not-created"))
    monkeypatch.setenv("HOSTED_AUTH0_CLIENT_SECRET", "synthetic-secret-marker")
    monkeypatch.setattr(sys, "argv", ["openinvest-hosted", "check-config"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    output = capsys.readouterr().out
    assert error.value.code == 2
    assert "synthetic-secret-marker" not in output
    assert "HOSTED_AUTH0_ISSUER" in json.loads(output)["missing"]
    assert not (tmp_path / "not-created").exists()


def test_config_check_valid_and_invalid_values(tmp_path, monkeypatch, capsys):
    from openinvest.hosted import cli
    import base64
    import sys
    values = dict(HOSTED_DATA_DIR=str(tmp_path / "not-created"), HOSTED_ORIGIN="https://hosted.test",
                  HOSTED_AUTH0_ISSUER="https://identity.test/", HOSTED_AUTH0_CLIENT_ID="web-client",
                  HOSTED_AUTH0_CLIENT_SECRET="synthetic-secret", HOSTED_ENCRYPTION_KEY=base64.b64encode(b"k"*32).decode(),
                  HOSTED_SESSION_SECRET="s"*40, HOSTED_ACTION_SECRET="a"*40, HOSTED_ADMIN_TOKEN="o"*40)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOSTED_REGISTRATION_OPEN", "0")
    monkeypatch.setattr(sys, "argv", ["openinvest-hosted", "check-config"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["valid"]
    assert not (tmp_path / "not-created").exists()
    monkeypatch.setenv("HOSTED_ORIGIN", "http://invalid.test")
    with pytest.raises(SystemExit):
        cli.main()
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_values"


def test_hosted_policy_allows_component_styles_without_inline_scripts(environment):
    policy = environment.client.get("/health").headers["content-security-policy"]
    directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
    assert directives["style-src-attr"] == "'unsafe-inline'"
    assert directives["style-src"] == directives["script-src"] == "'self'"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path, "https://hosted.test", "https://identity.test/", "https://hosted.test/mcp",
                    "web-client", "test-client-secret", "s"*40, b"k"*32, action_secret="a"*40,
                    admin_token="o"*40, registration_open=True)


class FakePool:
    def __init__(self):
        self.entries, self.balances, self.calls = {}, {}, []

    @asynccontextmanager
    async def lease(self, owner):
        self.calls.append(owner)
        yield SimpleNamespace(origin=f"http://{owner}.worker", token="private-worker-secret")

    async def reap(self):
        pass

    async def internal(self, owner, path, payload=None, raw=None):
        if path == "initialize":
            from openinvest.hosted.worker import Initialization
            Initialization.model_validate(payload)
        return b"synthetic-backup"

    async def destroy(self, owner):
        self.balances.pop(owner, None)

    def request(self, request):
        owner = request.url.host.split(".")[0]
        assert request.headers["authorization"] == "Bearer private-worker-secret"
        message = json.loads(request.content)
        if message["method"] == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"].get("arguments", {})
            if name == "deposit":
                self.balances[owner] = self.balances.get(owner, 0)+arguments["amount"]
            data = {"amount": self.balances.get(owner, 0), "currency": "USD", "status": "ok"}
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        else:
            result = {"tools": []}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"), "result": result})


@pytest.fixture
def environment(settings):
    store = Store(settings.root, settings.encryption_key)
    pool = FakePool()
    app = create_app(settings, store=store, pool=pool, transport=httpx.MockTransport(pool.request))
    accounts = []
    for subject in ("google-oauth2|synthetic-a", "github|synthetic-b"):
        account = store.identity(subject)
        store.preferences(account["id"], True, settings.policy_version)
        store.initialized(account["id"])
        token, _ = store.credential(account["id"])
        accounts.append((store.account(account["id"]), token))
    with TestClient(app, base_url=settings.origin) as client:
        yield SimpleNamespace(client=client, store=store, pool=pool, accounts=accounts,
                              app=app, research=app.state.research, settings=settings)


def rpc(env, token, name="deposit", arguments=None, request_id=1, **headers):
    return env.client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments or {"amount": 120.75, "currency": "USD", "idempotency_key": "same-key"}}},
                           headers={"Authorization": "Bearer "+token, **headers})


def browser(env, account):
    token, credential = env.store.credential(account["id"], "session", ttl=3600)
    env.client.cookies.set("oi_session", token)
    return {"Origin": env.settings.origin, "X-CSRF-Token": credential["csrf"]}


def test_owner_scoped_idempotency_and_replay_id(environment):
    env = environment
    (a, first), (b, second) = env.accounts
    assert rpc(env, first).status_code == 200
    replay = rpc(env, first, request_id=89)
    assert replay.json()["id"] == 89
    assert env.pool.balances[a["id"]] == 120.75
    assert rpc(env, second).status_code == 200
    assert env.pool.balances[b["id"]] == 120.75
    changed = rpc(env, first, arguments={"amount": 5, "currency": "USD", "idempotency_key": "same-key"})
    assert changed.status_code == 409
    assert env.pool.balances[a["id"]] == 120.75
    assert b"120.75" not in env.store.path.read_bytes()  # receipts are encrypted


def research_samples(env):
    with env.research.db() as db:
        return [env.research.unseal(row[0]) for row in db.execute("SELECT payload FROM samples WHERE payload IS NOT NULL ORDER BY created,id")]


def test_research_orders_attempts_scopes_retry_links_and_survives_restart(environment):
    env = environment
    (a, first), (b, second) = env.accounts
    arguments = {"amount": 12345.67, "currency": "AUD", "idempotency_key": "PRIVATE_RETRY_KEY"}
    assert rpc(env, first, arguments=arguments, request_id="PRIVATE_RPC_ID").status_code == 200
    assert rpc(env, first, arguments=arguments).status_code == 200
    assert rpc(env, second, arguments=arguments).status_code == 200
    samples = research_samples(env)
    first_samples = [s for s in samples if s["participant"] == a["participant"]]
    assert [s["trace"]["sequence"] for s in first_samples] == [1, 2]
    assert [s["retry"] for s in first_samples] == [False, True]
    assert all(s["trace"]["sequence_source"] == "received" and s["trace"]["received_at"] for s in samples)
    assert len({s["trace"]["call_id"] for s in samples}) == 3
    assert len({s["trace"]["operation_id"] for s in first_samples}) == 1
    assert first_samples[0]["trace"]["operation_id"] != samples[2]["trace"]["operation_id"]
    assert samples[2]["trace"]["sequence"] == 1
    assert all(s["arguments"]["amount"] == 12345.67 and s["arguments"]["currency"] == "AUD" for s in samples)
    serialized = json.dumps(samples)
    for marker in ("PRIVATE_RETRY_KEY", "PRIVATE_RPC_ID", first, a["id"], a["subject"], second, b["id"]):
        assert marker not in serialized
    restarted = Research(env.settings, env.store)
    assert restarted.begin(a, "status", {})["sequence"] == 3
    assert all(s["schema_version"] == 2 for s in samples)


@pytest.mark.parametrize("executed,expected", [(True, "accepted"), (False, "declined")])
def test_execution_feedback_export_keeps_reported_fact_without_correctness_label(environment, tmp_path, executed, expected):
    env = environment
    _, token = env.accounts[0]
    arguments = {"decision_id": "2026-11-01/NDQ.AX", "executed": executed,
                 "reason": "PRIVATE_REASON test@example.com", "trade_ids": [12, 13]}
    assert rpc(env, token, name="record_execution", arguments=arguments).status_code == 200
    sample = research_samples(env)[0]
    assert sample["arguments"]["executed"] is executed
    assert sample["arguments"]["decision_id"] == "2026-11-01/NDQ.AX"
    assert sample["arguments"]["trade_ids"] == [12, 13]
    assert sample["reported_execution"] == expected
    assert sample["execution_source"] == "client_reported"
    assert sample["adoption"] == "unknown" and sample["correctness"] == "unverified"
    env.research.review(sample["sample_id"], "accepted", source_validity="valid", allowed_uses=["agent_training"])
    output = tmp_path/"feedback.jsonl"
    env.research.export(output)
    assert "PRIVATE_REASON" not in output.read_text()
    assert "test@example.com" not in output.read_text()
    assert json.loads(output.read_text())["reported_execution"] == expected


@pytest.mark.parametrize("mode,expected", [("business", "tool_error"), ("rpc", "rpc_error"),
                                          ("http", "upstream_http_error"), ("incomplete", "incomplete_response")])
def test_mcp_failure_samples_are_not_successful_decisions(environment, mode, expected):
    env = environment
    _, token = env.accounts[0]
    def response(request):
        if mode == "http":
            return httpx.Response(503, content=b"SECRET_UPSTREAM_ERROR")
        if mode == "incomplete":
            return httpx.Response(200, content=b": keepalive\n\n", headers={"Content-Type": "text/event-stream"})
        message = json.loads(request.content)
        content = {"error": {"code": -32603, "message": "SECRET_RPC_ERROR"}} if mode == "rpc" else {
            "result": {"structuredContent": {"status": "error", "error": "SECRET_BUSINESS_ERROR"}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], **content})
    # Use a separate app to bind the synthetic error transport explicitly.
    app = create_app(env.settings, store=env.store, pool=env.pool, transport=httpx.MockTransport(response))
    with TestClient(app, base_url=env.settings.origin) as client:
        result = client.post("/mcp", json={"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"submit_committee", "arguments":{"analysis_id":"a"*32}}}, headers={"Authorization":"Bearer "+token})
    assert result.status_code == (503 if mode == "http" else 200)
    sample = research_samples(env)[0]
    assert sample["error_kind"] == expected
    assert sample["outcome"] == "error" and sample["sample_type"] == "tool_call"
    assert sample["reported_execution"] == "unknown"
    assert "SECRET_" not in json.dumps(sample)


def test_interrupted_write_is_an_error_sample_and_is_never_replayed(environment):
    env = environment
    _, token = env.accounts[0]
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": keepalive\n\n"
            raise httpx.ReadError("PRIVATE_TRANSPORT_EXCEPTION")
    def response(_request):
        return httpx.Response(200, stream=Interrupted(), headers={"Content-Type":"text/event-stream"})
    app = create_app(env.settings, store=env.store, pool=env.pool, transport=httpx.MockTransport(response))
    body = {"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"deposit", "arguments":{"amount":77.25, "currency":"USD", "idempotency_key":"PRIVATE_PENDING_KEY"}}}
    with TestClient(app, base_url=env.settings.origin) as client:
        with pytest.raises(httpx.ReadError):
            client.post("/mcp", json=body, headers={"Authorization":"Bearer "+token})
        assert client.post("/mcp", json=body, headers={"Authorization":"Bearer "+token}).status_code == 409
    samples = research_samples(env)
    assert [s["error_kind"] for s in samples] == ["stream_interrupted", "idempotency_conflict"]
    assert all(s["outcome"] == "error" and s["reported_execution"] == "unknown" for s in samples)
    assert samples[0]["trace"]["operation_id"] == samples[1]["trace"]["operation_id"]
    assert "PRIVATE_" not in json.dumps(samples)


def test_research_inflight_trace_cannot_cross_optout_or_policy_change(environment):
    env = environment
    account, _ = env.accounts[0]
    trace = env.research.begin(account, "status", {})
    assert trace is not None
    env.store.preferences(account["id"], False)
    env.research.capture(account, "status", {}, {"amount": 200}, 10, trace=trace)
    assert research_samples(env) == []
    stale = env.store.preferences(account["id"], True, "obsolete-policy")
    assert env.research.begin(stale, "status", {}) is None
    env.research.capture(stale, "status", {}, {"amount": 200}, 10)
    assert research_samples(env) == []


def test_pending_after_restart_is_not_replayed(settings):
    store = Store(settings.root, settings.encryption_key)
    args = {"amount": 100, "currency": "USD"}
    assert store.reserve("owner", "k", "deposit", args) is None
    restarted = Store(settings.root, settings.encryption_key)
    with pytest.raises(Conflict, match="unknown"):
        restarted.reserve("owner", "k", "deposit", args)
    with pytest.raises(Conflict, match="different"):
        restarted.reserve("owner", "k", "deposit", {"amount": 101})


def test_auth_scope_tool_and_input_boundaries(environment):
    env = environment
    a, token = env.accounts[0]
    assert rpc(env, "bad").status_code == 401
    assert rpc(env, token, "run_committee").status_code == 403
    assert rpc(env, token, "add_news_source").status_code == 403
    assert rpc(env, token, arguments={"amount": 10}).status_code == 422
    assert env.client.post("/mcp", json=[] , headers={"Authorization": "Bearer "+token}).status_code == 400
    result = rpc(env, token, **{"X-User-Id": env.accounts[1][0]["id"]})
    assert result.status_code == 200
    assert env.pool.calls[-1] == a["id"]
    assert env.client.post("/mcp", content=b"x"*(2*1024*1024+1)).status_code == 413


def test_collection_toggle_csrf_and_inflight_epoch(environment):
    env = environment
    account, token = env.accounts[0]
    headers = browser(env, account)
    assert env.client.patch("/api/me/research", json={"enabled": False}).status_code == 403
    assert rpc(env, token).status_code == 200
    assert env.client.patch("/api/me/research", json={"enabled": False}, headers=headers).status_code == 200
    with env.research.db() as db:
        assert db.execute("SELECT COUNT(*) FROM samples WHERE status='pending'").fetchone()[0] == 0
    env.research.capture(account, "status", {}, {"amount": 22}, 3)  # stale in-flight settings
    assert rpc(env, token, "status", arguments={}).status_code == 200
    with env.research.db() as db:
        assert db.execute("SELECT COUNT(*) FROM samples WHERE status='pending'").fetchone()[0] == 0


def test_training_exact_amounts_quarantined_text_and_versioned_export(environment, tmp_path):
    env = environment
    account, _ = env.accounts[0]
    env.research.capture(account, "submit_committee", {"amount": 12345.67, "currency": "USD",
                         "transcript": "IDENTITY_CANARY test@example.com Bearer secret-canary-0123456789",
                         "name": "IDENTITY_CANARY"}, {"verdict": {"verdict": "HOLD", "confidence": .7}}, 20)
    with env.research.db() as db:
        row = db.execute("SELECT * FROM samples").fetchone()
    assert b"IDENTITY_CANARY" not in env.research.path.read_bytes()
    data = env.research.unseal(row["payload"])
    assert data["arguments"]["amount"] == 12345.67
    assert data["adoption"] == "unknown"
    env.research.review(row["id"], "accepted", {"rationale": "Evidence supports waiting."}, source_validity="valid", allowed_uses=["agent_training"])
    output = tmp_path/"dataset.jsonl"
    manifest = env.research.export(output)
    exported = json.loads(output.read_text())
    assert exported["arguments"]["amount"] == 12345.67
    assert "IDENTITY_CANARY" not in output.read_text()
    assert "example.com" not in output.read_text()
    assert account["id"] not in output.read_text()
    assert account["subject"] not in output.read_text()
    assert exported["review_label"] == "accepted"
    assert manifest["samples"] == [row["id"]]
    assert env.research.delete_unused(account["participant"]) == 1
    assert not output.exists()
    with pytest.raises(ValueError):
        env.research.mark_used(manifest["dataset_version"])


def test_sensitive_text_screening_handles_long_non_email_runs():
    import subprocess
    import sys
    from openinvest.hosted.research import PII
    for email in ("test@example.com", "first.last+tag@sub.example.co.uk", "a-b_c@domain.test"):
        text = "Evidence from " + email + "."
        assert PII.search(text).group() == email
        assert email not in PII.sub("[REDACTED]", text)
    # Run potentially hostile input outside pytest so a regression has a hard
    # deadline rather than wedging the whole suite or gateway research thread.
    subprocess.run([sys.executable, "-c", "from openinvest.hosted.research import PII; "
                    "assert not PII.search('+'*32000); "
                    "assert not PII.search('a'*32000); "
                    "assert not PII.search('x@' + 'a.'*16000)"],
                   check=True, capture_output=True, timeout=5)


def test_unreviewed_samples_are_not_training_data(environment, tmp_path):
    env = environment
    env.research.capture(env.accounts[0][0], "status", {}, {"amount": 300}, 1)
    result = env.research.export(tmp_path/"empty.jsonl")
    assert result["samples"] == []


def test_queue_failure_never_reverses_business_success(environment):
    env = environment
    env.research.settings = replace(env.settings, queue_bytes=0)
    assert rpc(env, env.accounts[0][1]).status_code == 200
    assert env.pool.balances[env.accounts[0][0]["id"]] == 120.75
    with env.store.db() as db:
        assert db.execute("SELECT value FROM metrics WHERE name='research_queue_full'").fetchone()[0] == 1


def test_token_revocation_and_browser_defaults(environment):
    env = environment
    account, token = env.accounts[0]
    headers = browser(env, account)
    created = env.client.post("/api/me/credentials", json={}, headers=headers).json()
    assert rpc(env, created["token"], "status", arguments={}).status_code == 200
    assert env.client.delete("/api/me/credentials/"+created["id"], headers=headers).status_code == 200
    assert rpc(env, created["token"], "status").status_code == 401
    assert created["token"] not in env.client.get("/api/me").text
    env.store.preferences(account["id"], False)
    assert env.client.get("/api/me").json()["research_enabled"] is False
    assert env.client.get("/operator/status").status_code == 401


def test_metadata_and_business_context_do_not_contain_notice(environment):
    env = environment
    response = env.client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.json()["resource"] == env.settings.audience
    token = env.accounts[0][1]
    for method in ("initialize", "tools/list"):
        result = env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, headers={"Authorization": "Bearer "+token})
        assert "research_enabled" not in result.text
        assert env.settings.policy_version not in result.text


def test_jwt_signature_audience_issuer_expiration_and_connection(environment):
    from cryptography.hazmat.primitives.asymmetric import rsa
    env = environment
    account = env.accounts[0][0]
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))
    auth = Authenticator(env.settings, env.store, jwks)
    claims = {"iss": env.settings.issuer, "aud": env.settings.audience, "sub": account["subject"],
              "iat": int(time.time()), "exp": int(time.time())+3600, "azp": "chatgpt", "scope": "invest:read"}
    sign = lambda c: "Bearer "+jwt.encode(c, private, algorithm="RS256")
    with pytest.raises(AuthenticationError):
        auth.bearer(sign(claims))
    env.store.connect_oauth(account["id"], "chatgpt", authorize=True)
    assert auth.bearer(sign(claims))[1] == {"invest:read"}
    for invalid in ({"aud": "google-userinfo"}, {"iss": "https://evil.test/"}, {"exp": 1}, {"sub": "other-user"}):
        with pytest.raises(AuthenticationError):
            auth.bearer(sign({**claims, **invalid}))
    with pytest.raises(AuthenticationError):
        auth.bearer("Bearer "+jwt.encode(claims, "attacker", algorithm="HS256"))
    env.store.revoke_all(account["id"])
    with pytest.raises(AuthenticationError):
        auth.bearer(sign(claims))


def test_action_handoff_is_once_and_browser_authorization_required(environment):
    env = environment
    account = env.accounts[0][0]
    token = jwt.encode({"sub": account["subject"], "client_id": "chatgpt", "jti": "once",
                        "aud": env.settings.origin, "exp": int(time.time())+300}, env.settings.action_secret, algorithm="HS256")
    response = env.client.get("/auth/action", params={"session_token": token, "state": "auth0-state"}, follow_redirects=False)
    assert response.status_code == 303
    assert not env.store.connect_oauth(account["id"], "chatgpt")
    me = env.client.get("/api/me").json()
    response = env.client.post("/api/me/authorize", headers={"Origin": env.settings.origin, "X-CSRF-Token": me["csrf"]})
    assert response.status_code == 200
    assert env.store.connect_oauth(account["id"], "chatgpt")
    assert env.client.get("/auth/action", params={"session_token": token, "state": "auth0-state"}).status_code == 401


class MemoryArchive:
    def __init__(self):
        self.objects = {}
    def put(self, key, body):
        self.objects[key] = body
    def get(self, key):
        return self.objects[key]
    def delete(self, key):
        self.objects.pop(key, None)


def test_archive_outage_and_optout_upload_race(environment):
    env = environment
    account = env.accounts[0][0]
    env.research.capture(account, "status", {}, {"amount": 100}, 1)
    archive = MemoryArchive()
    def concurrent_put(key, body):
        env.store.preferences(account["id"], False)
        env.research.cancel_pending(account["participant"])
        archive.objects[key] = body
    archive.put = concurrent_put
    env.research.flush(archive)
    assert archive.objects == {}
    with env.research.db() as db:
        assert db.execute("SELECT COUNT(*) FROM samples WHERE status='archived'").fetchone()[0] == 0


def test_backup_authenticated_and_encrypted_owner_binding(environment):
    from openinvest.hosted.backup import seal, unseal
    env = environment
    blob = seal(env.settings, "ledger", "owner-a", b"PRIVATE_BALANCE_CANARY")
    assert b"PRIVATE_BALANCE_CANARY" not in blob
    assert unseal(env.settings, blob, kind="ledger", owner="owner-a") == b"PRIVATE_BALANCE_CANARY"
    with pytest.raises(ValueError):
        unseal(env.settings, blob, kind="ledger", owner="owner-b")
    assert env.client.post("/operator/backup", json={}).status_code == 401


def test_delete_during_upload_retains_compensating_tombstone(environment):
    env = environment
    account = env.accounts[0][0]
    env.research.capture(account, "status", {}, {"amount": 12.5}, 2)
    archive = MemoryArchive()
    def put(key, body):
        assert env.research.delete_unused(account["participant"]) == 1
        archive.objects[key] = body
    archive.put = put
    env.research.flush(archive)
    assert not archive.objects
    env.research.flush(archive)
    with env.research.db() as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0


def test_archive_outage_preserves_queue_and_retries(environment):
    env = environment
    env.research.capture(env.accounts[0][0], "status", {}, {"amount": 77}, 1)
    archive = MemoryArchive()
    original = archive.put
    archive.put = lambda *_: (_ for _ in ()).throw(OSError("SENSITIVE_EXCEPTION_CANARY"))
    env.research.flush(archive)
    with env.research.db() as db:
        assert db.execute("SELECT status FROM samples").fetchone()[0] == "pending"
    archive.put = original
    env.research.flush(archive)
    with env.research.db() as db:
        assert db.execute("SELECT status,payload FROM samples").fetchone()[0] == "archived"
    assert len(archive.objects) == 1


def test_research_completeness_versions_retries_and_atomic_export(environment, tmp_path):
    env = environment
    account, token = env.accounts[0]
    roles = ("MACRO", "QUANT_R1", "RISK_R1", "QUANT_R2", "RISK_R2", "CIO")
    transcript = "\n".join(f"=== {role} ===\nEvidence" for role in roles)
    for text in ("VERDICT: HOLD", transcript):
        env.research.capture(account, "submit_committee", {"transcript": text, "analysis_id": "a"*32}, {"verdict": {"_original_alloc": 88.75}}, 1)
    with env.research.db() as db:
        rows = list(db.execute("SELECT id,payload FROM samples ORDER BY created"))
    assert [env.research.unseal(r["payload"])["sample_type"] for r in rows] == ["tool_call", "decision"]
    assert env.research.unseal(rows[1]["payload"])["result"]["verdict"]["_original_alloc"] == 88.75
    env.research.review(rows[0]["id"], "accepted", source_validity="valid", allowed_uses=["agent_training"])
    # Publishing must never overwrite an existing dataset or leave a partial export.
    target = tmp_path/"protected.jsonl"
    target.write_text("EXISTING")
    with pytest.raises(FileExistsError):
        env.research.export(target)
    assert target.read_text() == "EXISTING"
    initialize = env.client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'Test Client','version':'1.2'}}}, headers={'Authorization':'Bearer '+token})
    assert initialize.status_code == 200
    rpc(env, token)
    rpc(env, token)
    with env.research.db() as db:
        latest = env.research.unseal(db.execute("SELECT payload FROM samples ORDER BY created DESC LIMIT 1").fetchone()[0])
    assert latest["retry"] is True
    assert latest["client"] == {"client_name":"Test Client", "client_version":"1.2"}


def test_ledger_reset_requires_setup_and_connection_revoke(environment):
    env = environment
    account, token = env.accounts[0]
    headers = browser(env, account)
    env.store.connect_oauth(account["id"], "client-a", authorize=True)
    assert env.client.get('/api/me').json()['connections'][0]['client'] == 'client-a'
    assert env.client.delete('/api/me/connections/client-a', headers=headers).status_code == 200
    assert not env.store.connect_oauth(account['id'],'client-a')
    assert env.client.delete('/api/me/ledger',headers=headers).status_code == 200
    assert rpc(env,token).status_code == 409
    assert not env.client.get('/api/me').json()['initialized']


def test_consent_and_oauth_can_finish_before_agent_ledger_setup(environment):
    env = environment
    account = env.store.identity('google-oauth2|new-uninitialized-synthetic')
    # An Auth0 handoff is required to bind the authorized client to this account.
    ticket = jwt.encode({'sub': account['subject'], 'client_id': 'new-synthetic-client',
                         'aud': env.settings.origin, 'exp': int(time.time())+300,
                         'jti': 'owned-one-use-ticket'}, env.settings.action_secret, algorithm='HS256')
    handoff = env.client.get('/auth/action', params={'state':'synthetic-state','session_token':ticket}, follow_redirects=False)
    assert handoff.status_code == 303
    me = env.client.get('/api/me').json()
    headers = {'Origin':env.settings.origin,'X-CSRF-Token':me['csrf']}
    assert me['provider'] == 'google' and me['account_id'] == account['id']
    assert not me['initialized'] and not me['accepted']
    assert env.client.post('/api/me/credentials', headers=headers).status_code == 409
    consent = {'policy_version':env.settings.policy_version,'research_enabled':False}
    assert env.client.post('/api/me/consent',json=consent).status_code == 403
    assert env.client.post('/api/me/consent',json=consent,headers=headers).status_code == 200
    assert not env.store.account(account['id'])['initialized']
    authorized = env.client.post('/api/me/authorize', headers=headers)
    assert authorized.status_code == 200
    assert env.store.connect_oauth(account['id'],'new-synthetic-client')
    pat = env.client.post('/api/me/credentials',headers=headers).json()['token']
    assert rpc(env,pat,'status',{'unused':False}).status_code == 409
    assert rpc(env,pat,'doctor',{'unused':False}).status_code == 200
    assert env.client.get('/api/me/ledger/settings').json() == {'initialized':False}
    assert env.client.patch('/api/me/research',json={'enabled':True},headers=headers).status_code == 200
    assert env.client.patch('/api/me/research',json={'enabled':False},headers=headers).status_code == 200
    assert env.client.delete('/api/me/credentials/'+env.store.credentials(account['id'])[0]['id'],headers=headers).status_code == 200
    assert rpc(env,pat,'doctor',{'unused':False}).status_code == 401


def test_full_restore_revokes_access_and_drops_untrained_candidates(environment, tmp_path):
    from openinvest.hosted.backup import backup_all, restore_database
    env = environment
    account = env.accounts[0][0]
    env.research.capture(account,'status',{}, {'amount':100},1)
    assert rpc(env, env.accounts[0][1]).status_code == 200
    archive = MemoryArchive()
    manifest = asyncio.run(backup_all(env.settings,env.store,env.research,env.pool,archive))
    recovered = replace(env.settings,root=tmp_path/'recovered')
    for name in ('control','research','receipts'):
        restore_database(recovered,archive,name,manifest['backup'])
    store = Store(recovered.root,recovered.encryption_key)
    restored = store.account(account['id'])
    assert restored['research'] == 0 and restored['accepted'] is None
    assert store.authenticate(env.accounts[0][1],'pat') is None
    receipt = store.reserve(account['id'], 'same-key', 'deposit', {'amount':120.75,'currency':'USD','idempotency_key':'same-key'})
    assert receipt['result']['content']
    with Research(recovered,store).db() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 0


def test_backup_gate_includes_requests_still_authenticating(environment):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    env = environment
    entered, release = threading.Event(), threading.Event()
    class SlowAuth:
        def bearer(self, _authorization):
            entered.set()
            assert release.wait(5)
            return env.accounts[0][0], {'invest:read'}
    app = create_app(env.settings, store=env.store, pool=env.pool, authenticator=SlowAuth(),
                     transport=httpx.MockTransport(env.pool.request))
    with TestClient(app,base_url=env.settings.origin) as client, ThreadPoolExecutor(1) as executor:
        task = executor.submit(client.post,'/mcp', headers={'Authorization':'Bearer '+env.accounts[0][1]}, json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'doctor','arguments':{}}})
        try:
            assert entered.wait(3)
            result = client.post('/operator/backup',headers={'Authorization':'Bearer '+env.settings.admin_token})
            assert result.status_code == 503
            assert result.json()['error'] == 'Capacity busy; retry later'
        finally:
            release.set()
        assert task.result().status_code == 200


def test_hosted_log_formatter_omits_request_and_traceback():
    import logging
    from openinvest.hosted.safelog import SafeFormatter
    record = logging.LogRecord("hosted",logging.ERROR,"source",1,"IDENTITY_CREDENTIAL_CANARY",(),None)
    assert SafeFormatter().format(record) == "ERROR:hosted"


def test_research_wrong_prediction_retained_and_revoked_consent_blocks_reuse(environment, tmp_path):
    env=environment
    account,token=env.accounts[0]
    assert rpc(env,token).status_code == 200
    with env.research.db() as db:
        sample=db.execute("SELECT * FROM samples").fetchone()
    env.research.review(sample["id"], "failure_example", source_validity="valid",
                        judgment_outcome="wrong", allowed_uses=["agent_training"])
    output=tmp_path/"negative-example.jsonl"
    manifest=env.research.export(output)
    assert json.loads(output.read_text())["qualification"]["judgment_outcome"] == "wrong"
    # Simulate opt-out before asynchronous deletion; current consent must win.
    with env.store.db() as db:
        db.execute("UPDATE accounts SET research=0,epoch=epoch+1 WHERE id=?", (account["id"],))
    with pytest.raises(ValueError,match="revoked|qualification"):
        env.research.mark_used(manifest["dataset_version"])
    second=tmp_path/"after-optout.jsonl"
    assert env.research.export(second)["samples"] == []


def test_research_export_requires_explicit_source_qualification(environment, tmp_path):
    env=environment
    _,token=env.accounts[0]
    rpc(env,token)
    with env.research.db() as db:
        sample=db.execute("SELECT id FROM samples").fetchone()[0]
    env.research.review(sample,"accepted")
    assert env.research.export(tmp_path/"unknown.jsonl")["samples"] == []
    env.research.review(sample,"accepted",source_validity="valid",allowed_uses=["agent_training"])
    output=tmp_path/"qualified.jsonl"
    manifest=env.research.export(output)
    output.write_text(output.read_text()+" ")
    with pytest.raises(ValueError,match="integrity"):
        env.research.mark_used(manifest["dataset_version"])


def test_re_review_invalidates_old_export_even_with_valid_negative_sample(environment,tmp_path):
    env=environment
    _,token=env.accounts[0];rpc(env,token)
    with env.research.db() as db:sid=db.execute('SELECT id FROM samples').fetchone()[0]
    env.research.review(sid,'accepted',{'rationale':'Original hypothesis'},source_validity='valid',judgment_outcome='correct',allowed_uses=['agent_training'])
    manifest=env.research.export(tmp_path/'old.jsonl')
    env.research.review(sid,'failure_example',{'rationale':'Corrected negative result'},source_validity='valid',judgment_outcome='wrong',allowed_uses=['agent_training'])
    with pytest.raises(ValueError,match='review changed'):env.research.mark_used(manifest['dataset_version'])
    new=env.research.export(tmp_path/'corrected.jsonl')
    env.research.mark_used(new['dataset_version'])
    assert json.loads((tmp_path/'corrected.jsonl').read_text())['qualification']['judgment_outcome']=='wrong'
