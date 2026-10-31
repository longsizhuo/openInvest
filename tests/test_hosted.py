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
    env.research.review(row["id"], "accepted", {"rationale": "Evidence supports waiting."})
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
    env.research.review(rows[0]["id"], "accepted")
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
    assert rpc(env,token).status_code == 403
    assert not env.client.get('/api/me').json()['initialized']


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
